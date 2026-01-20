import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config.helpers import PROJECT_NAME, get_open_status_sg
from app.db.messages_repo import (
    log_message,
    claim_inbound_message_id,
    increment_daily_usage,
)
from app.services.whatsapp_client import send_whatsapp_message
from app.services.dedup import seen_recent
from app.services import history as history_store
from app.services import kb_cache
from app.services.chroma_store import get_collection_for_default_project, retrieve_context_from_vectordb
from app.services.admin_kb import add_text_to_vectordb, delete_by_id, log_admin_action
import app.config.settings as settings


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
        # ADMIN COMMANDS
        # -------------------------
        if from_number in settings.ADMIN_NUMBERS:

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
                collection = get_collection_for_default_project()
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

        context, cache_hit = kb_cache.get_cached_context(
            from_number=from_number,
            question=user_text,
            retrieve_fn=retrieve_context_from_vectordb,
            k=5,
            force_refresh=disable_kb_cache,
            return_meta=True,
        )

        t_retrieval_ms = (time.perf_counter() - t_retrieval0) * 1000.0

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
        )

        reply_text = chat.choices[0].message.content.strip()
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

        send_whatsapp_message(meta_phone_number_id, from_number, reply_text)

    except Exception as e:
        print("Error handling webhook:", e)
