import json
import os
import time
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from datetime import timezone
from app.config.helpers import PROJECT_NAME, get_open_status_sg
from app.db.messages_repo import (
    log_message,
    claim_inbound_message_id,
    increment_daily_usage,
)
from app.services.whatsapp_client import send_whatsapp_message, send_whatsapp_buttons
from app.services.dedup import seen_recent
from app.services import history as history_store
from app.services import kb_cache
from app.services.chroma_store import retrieve_context
from app.services.admin_kb import add_text_to_vectordb, delete_by_id, log_admin_action
import app.config.settings as settings
from app.services.booking_engine import try_create_pending_booking
from app.db import bookings_repo

SG_TZ = ZoneInfo("Asia/Singapore")


def classify_kb(text: str) -> str:
    t = (text or "").lower()

    if any(x in t for x in ["menu", "price", "service", "package", "promo"]):
        return "kb_menu"

    if any(x in t for x in ["contact", "phone", "email", "address", "location"]):
        return "kb_contact"

    return "kb_general"


def _wants_contact(text: str) -> bool:
    t = (text or "").lower()
    keywords = [
        "contact", "phone", "whatsapp", "email", "call", "number",
        "address", "where are you located", "location", "how to reach"
    ]
    return any(k in t for k in keywords)

def _contact_for_brand(text: str) -> str | None:
    t = (text or "").lower()

    # If they ask "who should I contact" + mention brands, give the relevant line(s)
    mercedes = "mercedes" in t or "benz" in t or "c class" in t or "c-class" in t
    bmw = "bmw" in t
    volkswagen = "volkswagen" in t or "vw" in t
    audi = "audi" in t

    lines = []

    if mercedes or bmw:
        lines.append("WhatsApp Enquiry:\nFor Mercedes & BMW: Ah Heng (+65 9475 4266)")
    if mercedes or volkswagen or audi:
        lines.append("For Mercedes, Volkswagen & Audi: Dennis Ng (+65 9475 4255)")

    if lines:
        return "CONTACT DETAILS\n\n" + "\n".join(lines)

    return None


_PHONE_RE = re.compile(r"(\+?\d[\d\s\-]{6,}\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

def _contains_contact_details(text: str) -> bool:
    if not text:
        return False
    return bool(_PHONE_RE.search(text) or _EMAIL_RE.search(text))

_PRICE_INTENT_RE = re.compile(
    r"\b(price|pricing|quote|quotation|cost|how much|estimate|estimated|rates?|charges?)\b", re.I
)
_PROMO_INTENT_RE = re.compile(
    r"\b(promo|promotion|discount|deal|package|bundle|offer|special)\b", re.I
)

# “Explicit pricing present in context” signals
_CONTEXT_HAS_PRICE_RE = re.compile(
    r"(\bS\$|\$|SGD\b|\bfrom\s+\$|\bstarting\s+at\b|\b\d+\s*(?:sgd|S\$|\$))",
    re.I
)

def _is_pricing_or_promo_query(text: str) -> bool:
    t = text or ""
    return bool(_PRICE_INTENT_RE.search(t) or _PROMO_INTENT_RE.search(t))

def _context_has_explicit_pricing(context: str) -> bool:
    if not context:
        return False
    return bool(_CONTEXT_HAS_PRICE_RE.search(context))

def _pricing_safe_fallback() -> str:
    # Deterministic, no “not listed” claims.
    if settings.BUSINESS_CONTACT_ENABLED:
        official = settings.format_business_contact_block(mode="pricing")
        return (
            "To provide an accurate quote, please share your vehicle model and year.\n\n"
            + official
        )
    return "To provide an accurate quote, please share your vehicle model and year."


def _to_whatsapp_format(text: str) -> str:
    """
    Convert common Markdown emphasis to WhatsApp emphasis.
    WhatsApp: *bold* and _italic_. It does NOT support **bold**.
    """
    if not text:
        return text

    # Convert Markdown bold **x** -> WhatsApp bold *x*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Convert Markdown italic *x* -> WhatsApp italic _x_
    # (only when it's single-asterisk wrapped, not bullet points)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)", r"_\1_", text)

    # Remove code fences if any (WhatsApp shows them ugly)
    text = text.replace("```", "")

    return text


def _finalize_reply(reply_text: str) -> str:
    """
    Enforce that we never send invented contact details.
    Only override LLM-generated contact hallucinations.
    """
    if not reply_text:
        return reply_text

    # Do NOT override official contact blocks
    if reply_text.startswith("CONTACT DETAILS"):
        return reply_text

    if settings.BUSINESS_CONTACT_ENABLED and _contains_contact_details(reply_text):
        official = settings.format_business_contact_block(mode="pricing")
        return (
            "For an accurate quote, please contact our team and share your vehicle model and year:\n\n"
            + official
        )

    return reply_text


def _to_sg(dt: datetime) -> datetime:
    if dt is None:
        return dt
    # If DB gives naive dt, assume it's UTC (common psycopg / serialization edge case)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SG_TZ)


def _fmt_window(start_ts: datetime, end_ts: datetime) -> str:
    s = _to_sg(start_ts)
    e = _to_sg(end_ts)
    return f"{s.strftime('%a %d %b %Y, %H:%M')}–{e.strftime('%H:%M')}"

def _display_ref(req: dict) -> str:
    # Prefer public_ref (random), fallback to numeric id
    return str(req.get("public_ref") or req.get("id"))


def process_webhook_payload(body: dict, admin_log_file: str, perf_log_file: str, disable_kb_cache: bool):
    try:
        entry = body["entry"][0]["changes"][0]["value"]
        meta_phone_number_id = entry["metadata"]["phone_number_id"]
        messages = entry.get("messages")
        if not messages:
            return

        msg = messages[0]
        msg_id = msg.get("id")

        # Idempotency: DB first, then memory
        if msg_id:
            if not claim_inbound_message_id(msg_id):
                print("[DEDUP][DB] Duplicate inbound msg_id ignored:", msg_id)
                return
            if seen_recent(msg_id):
                print("[DEDUP][MEM] Duplicate inbound msg_id ignored:", msg_id)
                return

        msg_type = msg.get("type")
        from_number = msg["from"]
        user_text = ""

        if msg_type == "text":
            user_text = msg["text"]["body"]
            # -------------------------
            # RATE LIMIT (per number / per day)
            # -------------------------
            if (
                settings.RATE_LIMIT_ENABLED
                and from_number not in settings.ADMIN_NUMBERS
            ):
                now_sg = datetime.now(ZoneInfo(settings.RATE_LIMIT_TZ))
                today_sg = now_sg.date()

                new_count = increment_daily_usage(from_number, today_sg)

                if new_count > settings.RATE_LIMIT_MAX_PER_DAY:
                    send_whatsapp_message(
                        meta_phone_number_id,
                        from_number,
                        settings.RATE_LIMIT_BLOCK_MESSAGE,
                    )
                    return

            try:
                log_message(phone_number=from_number, direction="in", text=user_text)
            except Exception as e:
                print("[WARN] DB inbound log failed:", e)
        
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            button_reply = interactive.get("button_reply", {})
            btn_id = button_reply.get("id", "")

            # Convert button click into a synthetic text command for booking_engine
            # Example: "BOOK_CONFIRM:123" -> "__BOOK_CONFIRM__ 123"
            if btn_id.startswith("BOOK_CONFIRM:"):
                user_text = "__BOOK_CONFIRM__ " + btn_id.split(":", 1)[1]
            elif btn_id.startswith("BOOK_CANCEL:"):
                user_text = "__BOOK_CANCEL__ " + btn_id.split(":", 1)[1]
            else:
                user_text = btn_id  # fallback

            try:
                log_message(phone_number=from_number, direction="in", text=f"[button]{btn_id}")
            except Exception as e:
                print("[WARN] DB inbound log failed:", e)
        
        elif msg_type == "image":
            send_whatsapp_message(
                meta_phone_number_id,
                from_number,
                "I’ve received your image, but I can only understand text messages. "
                "Please type your question as a message.",
            )
            return
        else:
            send_whatsapp_message(
                meta_phone_number_id,
                from_number,
                "I can only understand text messages at the moment. "
                "Please type your question as a message.",
            )
            return

        # -------------------------
        # CONTACT INFO (authoritative constants; no LLM)
        # -------------------------
        if settings.BUSINESS_CONTACT_ENABLED and _wants_contact(user_text):
            # If user asked "who to contact for <brand>", give targeted contact lines
            targeted = _contact_for_brand(user_text)
            if targeted:
                reply_text = targeted
            else:
                # Otherwise show the full official block
                reply_text = settings.format_business_contact_block(mode="full")

            reply_text = _finalize_reply(reply_text)
            reply_text = _to_whatsapp_format(reply_text)

            try:
                log_message(phone_number=from_number, direction="out", text=reply_text)
            except Exception as e:
                print("[WARN] DB outbound log failed:", e)

            send_whatsapp_message(meta_phone_number_id, from_number, reply_text)
            return

        # -------------------------
        # ADMIN COMMANDS
        # -------------------------
        if from_number in settings.ADMIN_NUMBERS:

            # -------------------------
            # BOOKING ADMIN ACTIONS (WhatsApp)
            # -------------------------
            if user_text.startswith("/approve "):
                ref = user_text[len("/approve "):].strip()
                if not ref:
                    send_whatsapp_message(meta_phone_number_id, from_number, "Usage: /approve <ref>")
                    return

                req_id = bookings_repo.resolve_request_id(ref)
                if not req_id:
                    send_whatsapp_message(meta_phone_number_id, from_number, f"Ref #{ref} not found.")
                    return

                req = bookings_repo.get_request(req_id)

                if not req:
                    send_whatsapp_message(meta_phone_number_id, from_number, f"Ref #{req_id} not found.")
                    return

                ok = bookings_repo.decide_request(req_id, from_number, "approved", admin_note=None)
                if not ok:
                    send_whatsapp_message(meta_phone_number_id, from_number, f"Ref #{req_id} is not pending (already decided).")
                    return

                hold_id = bookings_repo.find_hold_by_request(req_id)
                if hold_id:
                    bookings_repo.release_hold(hold_id)

                start_ts = req["start_ts"]
                end_ts = req["end_ts"]
                label = req["service_label"]

                # Notify customer
                ref_out = _display_ref(req)

                customer_msg = (
                    "Confirmed ✅\n"
                    f"{label}\n"
                    f"{_fmt_window(start_ts, end_ts)}\n"
                    f"Ref #{ref_out}"
                )
                send_whatsapp_message(meta_phone_number_id, req["customer_number"], customer_msg)

                # Ack admin
                send_whatsapp_message(meta_phone_number_id, from_number, f"Approved Ref #{ref_out}. Customer notified.")
                return


            if user_text.startswith("/reject "):
                ref = user_text[len("/reject "):].strip()
                if not ref:
                    send_whatsapp_message(meta_phone_number_id, from_number, "Usage: /reject <ref>")
                    return

                req_id = bookings_repo.resolve_request_id(ref)
                if not req_id:
                    send_whatsapp_message(meta_phone_number_id, from_number, f"Ref #{ref} not found.")
                    return

                req = bookings_repo.get_request(req_id)
                if not req:
                    send_whatsapp_message(meta_phone_number_id, from_number, f"Ref #{ref} not found.")
                    return

                ok = bookings_repo.decide_request(req_id, from_number, "rejected", admin_note=None)
                if not ok:
                    send_whatsapp_message(meta_phone_number_id, from_number, f"Ref #{_display_ref(req)} is not pending (already decided).")
                    return

                hold_id = bookings_repo.find_hold_by_request(req_id)
                if hold_id:
                    bookings_repo.release_hold(hold_id)

                ref_out = _display_ref(req)

                # Notify customer
                customer_msg = (
                    "Sorry — that slot couldn’t be confirmed.\n"
                    "Please suggest another date/time and I’ll check availability.\n"
                    f"Ref #{ref_out}"
                )
                send_whatsapp_message(meta_phone_number_id, req["customer_number"], customer_msg)

                # Ack admin
                send_whatsapp_message(meta_phone_number_id, from_number, f"Rejected Ref #{ref_out}. Customer notified.")
                return


            if user_text.startswith("/add "):
                content = user_text[5:].strip()
                doc_id = add_text_to_vectordb(content, source="admin")

                log_admin_action(
                    admin_log_file,
                    from_number,
                    "ADD_ENTRY",
                    {
                        "doc_id": doc_id,
                        "source_tag": "admin",
                        "content": content,
                        "content_preview": content[:200],
                    },
                )

                try:
                    log_message(phone_number=from_number, direction="out", text=f"Added entry with ID: {doc_id}")
                except Exception as e:
                    print("[WARN] DB outbound log failed:", e)

                send_whatsapp_message(meta_phone_number_id, from_number, f"Added entry with ID: {doc_id}")
                return

            if user_text.startswith("/del "):
                doc_id = user_text[5:].strip()

                deleted_entry = delete_by_id(doc_id)
                if deleted_entry is None:
                    send_whatsapp_message(
                        meta_phone_number_id,
                        from_number,
                        f"No exact ID '{doc_id}' found. Nothing deleted.",
                    )
                    return

                log_admin_action(
                    admin_log_file,
                    from_number,
                    "DELETE_ENTRY",
                    {
                        "deleted_doc_id": deleted_entry["doc_id"],
                        "deleted_content": deleted_entry["content"],
                        "deleted_metadata": deleted_entry.get("metadata", {}),
                    },
                )

                try:
                    log_message(phone_number=from_number, direction="out", text=f"Deleted entry with ID '{doc_id}'.")
                except Exception as e:
                    print("[WARN] DB outbound log failed:", e)

                send_whatsapp_message(meta_phone_number_id, from_number, f"Deleted entry with ID '{doc_id}'.")
                return

            if user_text.startswith("/list"):
                from app.services.chroma_store import get_collection
                collection = get_collection("kb_general")

                results = collection.get()

                docs = results.get("documents", [])
                metas = results.get("metadatas", [])
                ids = results.get("ids", [])

                if not docs:
                    send_whatsapp_message(meta_phone_number_id, from_number, "Database is empty.")
                    return

                message_lines = []
                for doc_id, doc_text, meta in zip(ids, docs, metas):
                    preview = doc_text[:200].replace("\n", " ")
                    message_lines.append(f"{doc_id}: {preview}...")

                listing = "\n".join(message_lines)

                try:
                    log_message(phone_number=from_number, direction="out", text="Admin requested list of KB entries")
                except Exception as e:
                    print("[WARN] DB outbound log failed:", e)

                send_whatsapp_message(meta_phone_number_id, from_number, listing)
                return
        # -------------------------
        # BOOKING ROUTING (calendar/db)
        # -------------------------
        handled, booking_reply, request_id, admin_payload = try_create_pending_booking(
            meta_phone_number_id=meta_phone_number_id,
            customer_number=from_number,
            user_text=user_text,
        )
        if handled:

            # Notify admin ONLY if a pending request was created
            if admin_payload:
                ref_id = admin_payload.get("public_ref") or str(admin_payload["request_id"])
                label = admin_payload["service_label"]
                start_ts = admin_payload["start_ts"]
                end_ts = admin_payload["end_ts"]

                admin_msg = (
                    "🚗 New booking request (needs approval)\n\n"
                    f"Customer: {from_number}\n"
                    f"Service: {label}\n"
                    f"Time: {_fmt_window(start_ts, end_ts)}\n"
                    f"Ref #{ref_id}\n\n"
                    "Reply with:\n"
                    f"/approve {ref_id}\n"
                    f"/reject {ref_id}"
                )

                admin_msg = _to_whatsapp_format(admin_msg)
                for admin_num in settings.ADMIN_NUMBERS:
                    if admin_num == from_number:
                        continue
                    send_whatsapp_message(meta_phone_number_id, admin_num, admin_msg)
            # If this is a proposal (no admin ping yet), send interactive buttons
            if (not admin_payload) and (request_id is None) and booking_reply.startswith("Slot looks available:"):
                d = bookings_repo.get_active_draft(from_number)
                if d:
                    draft_id = d["id"]
                    ok = send_whatsapp_buttons(
                        meta_phone_number_id,
                        from_number,
                        booking_reply,
                        buttons=[
                            {"id": f"BOOK_CONFIRM:{draft_id}", "title": "Confirm"},
                            {"id": f"BOOK_CANCEL:{draft_id}", "title": "Cancel"},
                        ],
                    )
                    if not ok:
                        # Fallback: interactive failed, so send text instructions the user can reply with
                        fallback = booking_reply + "\n\nIf you can’t see buttons, reply YES to confirm or CANCEL to stop."
                        fallback = _to_whatsapp_format(fallback)
                        send_whatsapp_message(meta_phone_number_id, from_number, fallback)
                        try:
                            log_message(phone_number=from_number, direction="out", text="[fallback] " + fallback)
                        except Exception as e:
                            print("[WARN] DB outbound log failed:", e)
                        return

                    try:
                        log_message(phone_number=from_number, direction="out", text="[buttons] " + booking_reply)
                    except Exception as e:
                        print("[WARN] DB outbound log failed:", e)
                    return
                else:
                    print("[WARN] Proposal detected but no active draft found; falling back to text.")

                
            # Log normal text replies
            try:
                log_message(phone_number=from_number, direction="out", text=booking_reply)
            except Exception as e:
                print("[WARN] DB outbound log failed:", e)

            booking_reply = _to_whatsapp_format(booking_reply)
            send_whatsapp_message(meta_phone_number_id, from_number, booking_reply)
            return

        # -------------------------
        # TOOL ROUTING (open now)
        # -------------------------
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_open_status_sg",
                    "description": "Returns whether the business is open right now using Asia/Singapore time. Hours: Mon-Sat 9am-6pm. Closed Sundays and Public Holidays.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            }
        ]

        tool_router_system = (
            "You are routing user requests for a WhatsApp business assistant. "
            "If the user is asking whether the business is open now/currently/still open, "
            "call get_open_status_sg. "
            "Otherwise, do not call any tool."
        )

        router_resp = settings.client.chat.completions.create(
            model=settings.CHAT_MODEL,
            messages=[
                {"role": "system", "content": tool_router_system},
                {"role": "user", "content": user_text},
            ],
            tools=tools,
            tool_choice="auto",
        )

        msg0 = router_resp.choices[0].message

        if getattr(msg0, "tool_calls", None):
            tool_messages = []
            for tc in msg0.tool_calls:
                if tc.function.name == "get_open_status_sg":
                    result = get_open_status_sg()
                else:
                    result = {"error": "Unknown tool"}

                tool_messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)}
                )

            final_system = (
                "You are a WhatsApp business assistant. "
                "Use ONLY the tool result JSON to answer whether we are open right now. "
                "If open, include closing time. If closed, include next opening time. "
                "Be concise. Timezone is SGT."
            )

            final_resp = settings.client.chat.completions.create(
                model=settings.CHAT_MODEL,
                messages=[
                    {"role": "system", "content": final_system},
                    {"role": "user", "content": user_text},
                    msg0,
                    *tool_messages,
                ],
            )

            reply_text = final_resp.choices[0].message.content.strip()
            reply_text = _finalize_reply(reply_text)
            reply_text = _to_whatsapp_format(reply_text)

            try:
                log_message(phone_number=from_number, direction="out", text=reply_text)
            except Exception as e:
                print("[WARN] DB outbound log failed:", e)
            
            send_whatsapp_message(meta_phone_number_id, from_number, reply_text)
            return

        # -------------------------
        # RAG + HISTORY
        # -------------------------
        t_total0 = time.perf_counter()
        t_retrieval0 = time.perf_counter()

        kb_type = classify_kb(user_text)

        context, cache_hit = kb_cache.get_cached_context(
            from_number=from_number,
            question=user_text,
            kb_type=kb_type,
            retrieve_fn=lambda q, k: retrieve_context(q, kb_type, k),
            k=5,
            force_refresh=disable_kb_cache,
            return_meta=True,
        )


        t_retrieval_ms = (time.perf_counter() - t_retrieval0) * 1000.0

        if _is_pricing_or_promo_query(user_text) and (not _context_has_explicit_pricing(context)):
            reply_text = _pricing_safe_fallback()
            reply_text = _to_whatsapp_format(reply_text)

            try:
                log_message(phone_number=from_number, direction="out", text=reply_text, cache_hit=cache_hit, context_len=len(context or ""))
            except Exception as e:
                print("[WARN] DB outbound log failed:", e)

            send_whatsapp_message(meta_phone_number_id, from_number, reply_text)
            return


        if context:
            system_prompt = settings.PROMPTS["with_context"]["system"].format(project_name=PROJECT_NAME)
            user_prompt = settings.PROMPTS["with_context"]["user"].format(context=context, question=user_text)
        else:
            system_prompt = settings.PROMPTS["no_context"]["system"]
            user_prompt = settings.PROMPTS["no_context"]["user"].format(question=user_text)

        if history_store.is_stale(from_number, settings.HISTORY_MAX_AGE):
            history_store.clear(from_number)

        history = history_store.get_history(from_number)

        messages_for_model = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_prompt},
        ]

        chat = settings.client.chat.completions.create(
            model=settings.CHAT_MODEL,
            messages=messages_for_model,
            temperature=0,
        )

        reply_text = chat.choices[0].message.content.strip()

        # IMPORTANT:
        # Always sanitize contact details for pricing/promo queries (even if KB context exists).
        # For non-pricing queries, only sanitize when no KB context exists.
        if _is_pricing_or_promo_query(user_text) or (not context):
            reply_text = _finalize_reply(reply_text)


        t_total_ms = (time.perf_counter() - t_total0) * 1000.0

        perf_entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "from_number": from_number,
            "cache_disabled": disable_kb_cache,
            "cache_hit": cache_hit,
            "context_len": len(context or ""),
            "t_retrieval_ms": round(t_retrieval_ms, 2),
            "t_total_ms": round(t_total_ms, 2),
        }
        try:
            with open(perf_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(perf_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print("[WARN] Failed to write perf log:", e)

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": reply_text})

        if len(history) > settings.MAX_HISTORY_MESSAGES:
            history = history[-settings.MAX_HISTORY_MESSAGES:]

        history_store.set_history(from_number, history)
        history_store.touch(from_number)

        try:
            log_message(
                phone_number=from_number,
                direction="out",
                text=reply_text,
                cache_hit=cache_hit,
                context_len=len(context or ""),
                t_retrieval_ms=round(t_retrieval_ms, 2),
                t_total_ms=round(t_total_ms, 2),
            )
        except Exception as e:
            print("[WARN] DB outbound log failed:", e)
        reply_text = _to_whatsapp_format(reply_text)
        send_whatsapp_message(meta_phone_number_id, from_number, reply_text)

    except Exception as e:
        print("Error handling webhook:", e)
