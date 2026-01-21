from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import app.config.settings as settings
from app.db import bookings_repo

SG_TZ = ZoneInfo("Asia/Singapore")

def _to_sg(dt: datetime) -> datetime:
    # DB may return UTC tz-aware datetimes; always display in SGT.
    if dt is None:
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SG_TZ)



def _fmt_window(start_ts: datetime, end_ts: datetime) -> str:
    s = _to_sg(start_ts)
    e = _to_sg(end_ts)
    return f"{s.strftime('%a %d %b %Y, %H:%M')}–{e.strftime('%H:%M')}"

def _suggest_alternative_slots(
    service_key: str,
    requested_start: datetime,
    max_suggestions: int = 3,
    step_minutes: int = 30,
    search_days: int = 7,
) -> list[tuple[datetime, datetime]]:
    label, dur_min = SERVICE_CATALOG[service_key]
    suggestions: list[tuple[datetime, datetime]] = []

    # Start searching from the next step boundary to avoid re-checking the same taken slot
    cur = requested_start + timedelta(minutes=step_minutes)

    for _ in range(int((search_days * 24 * 60) / step_minutes)):
        # Skip Sundays (weekday() == 6)
        if cur.weekday() != 6:
            end = cur + timedelta(minutes=dur_min)

            # Business hours: Mon–Sat 9:00–18:00, end must be <= 18:00
            if cur.hour >= 9 and cur.hour < 18:
                if end.hour < 18 or (end.hour == 18 and end.minute == 0):
                    if bookings_repo.is_window_available(cur, end):
                        suggestions.append((cur, end))
                        if len(suggestions) >= max_suggestions:
                            break

        cur = cur + timedelta(minutes=step_minutes)

    return suggestions


def _fmt_suggestions(service_key: str, requested_start: datetime) -> str:
    alts = _suggest_alternative_slots(service_key, requested_start)
    if not alts:
        return (
            "That time is no longer available.\n\n"
            "Could you share a preferred time range (e.g. “Tuesday afternoon”)?"
        )

    lines = [
        "That time is no longer available, but I can offer these alternatives:",
        "",
    ]
    for s, e in alts:
        lines.append(f"• {_fmt_window(s, e)}")
    lines += [
        "",
        "Reply with one of the options above, or tell me another time you prefer.",
    ]
    return "\n".join(lines)


@dataclass
class BookingParse:
    intent: str  # "booking" or "other"
    service_key: str | None
    service_label: str | None
    # ISO-ish fields in SGT
    start_local: str | None  # "2026-01-21 14:00"
    confidence: float

#configure here for services:time
SERVICE_CATALOG = {
    # key: (label, duration_minutes)
    "car_servicing": ("Car servicing", 120),
    "car_wash": ("Car wash", 60),
    "polish": ("Polishing", 240),
}

DEFAULT_HOLD_MINUTES = int(getattr(settings, "BOOKING_HOLD_MINUTES", 10))


def _now_sg() -> datetime:
    return datetime.now(tz=SG_TZ)


def _parse_dt_local(s: str) -> datetime:
    # expects "YYYY-MM-DD HH:MM" in SGT
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SG_TZ)


def llm_parse_booking(user_text: str) -> BookingParse:
    """
    Parse booking intent + service + datetime in SGT.
    We make the model output strict JSON.
    """
    now = _now_sg()
    system = (
        "You extract booking info from a WhatsApp message for an auto service shop.\n"
        "Return ONLY JSON. No markdown.\n"
        "Timezone is Asia/Singapore.\n"
        f"Today is {now.strftime('%Y-%m-%d')}.\n"
        "If user is asking to book/come/appointment/reserve, intent='booking', else intent='other'.\n"
        "service_key must be one of: car_servicing, car_wash, polish, or null if unknown.\n"
        "start_local must be 'YYYY-MM-DD HH:MM' in 24h time, or null if missing/unclear.\n"
        "confidence is 0 to 1.\n"
    )
    user = f"Message: {user_text}"

    resp = settings.client.chat.completions.create(
        model=settings.CHAT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )

    obj = json.loads(resp.choices[0].message.content or "{}")
    return BookingParse(
        intent=obj.get("intent", "other"),
        service_key=obj.get("service_key"),
        service_label=None,
        start_local=obj.get("start_local"),
        confidence=float(obj.get("confidence", 0.0) or 0.0),
    )

def _is_confirmation(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"yes", "y", "confirm", "confirmed", "proceed", "ok", "okay"} or t.startswith("yes ")


def _is_cancellation(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"no", "n", "cancel", "stop", "dont", "don't"} or t.startswith("cancel")


def try_create_pending_booking(meta_phone_number_id: str, customer_number: str, user_text: str):
    """
    New flow:
    - Stage 2: if user confirms and a draft exists -> create booking_request -> notify admin
    - Stage 1: otherwise if intent booking -> propose slot and ask to proceed (no admin ping)
    Returns: (handled: bool, reply_text: str, request_id: int|None, admin_payload: dict|None)
    """
    bookings_repo.expire_old_holds()
    bookings_repo.expire_old_drafts()

    # -------------------------
    # Stage 2: confirmation / cancellation
    # -------------------------

    # Button-based confirmation: "__BOOK_CONFIRM__ <draft_id>" / "__BOOK_CANCEL__ <draft_id>"
    if user_text.startswith("__BOOK_CONFIRM__ "):
        draft_id_str = user_text.split(" ", 1)[1].strip()
        if not draft_id_str.isdigit():
            return True, "That confirmation button is invalid. Please request the slot again.", None, None

        draft_id = int(draft_id_str)
        d = bookings_repo.get_draft_by_id(draft_id)
        if not d or d["customer_number"] != customer_number:
            return True, "That booking offer is no longer available. Please request the slot again.", None, None
        if d["status"] != "proposed":
            return True, "That booking offer has expired/cancelled. Please request the slot again.", None, None

        # Continue using `d` like your current `draft`
        draft = d
        # fall through by reusing existing confirmation logic:
        # (we’ll just treat it as confirmed)
        user_text = "yes"

    if user_text.startswith("__BOOK_CANCEL__ "):
        draft_id_str = user_text.split(" ", 1)[1].strip()
        if not draft_id_str.isdigit():
            return True, "Cancelled.", None, None

        draft_id = int(draft_id_str)
        d = bookings_repo.get_draft_by_id(draft_id)
        if d and d["customer_number"] == customer_number and d["status"] == "proposed":
            bookings_repo.release_hold(d["hold_id"])
            bookings_repo.mark_draft(customer_number, d["id"], "cancelled")
        
        bookings_repo.clear_booking_context(customer_number)
        return True, "Okay — cancelled. If you want another slot, tell me your preferred date/time.", None, None

    # If confirm button path set `draft` already, keep it.
    if "draft" not in locals():
        draft = bookings_repo.get_active_draft(customer_number)

    if draft and _is_confirmation(user_text):

        # Final safety: ensure the window is still available (prevents race conditions)
        bookings_repo.expire_old_holds()
        bookings_repo.expire_old_drafts()

        if not bookings_repo.is_window_available(
            draft["start_ts"],
            draft["end_ts"],
            ignore_hold_id=draft["hold_id"],
        ):
            bookings_repo.release_hold(draft["hold_id"])
            bookings_repo.mark_draft(customer_number, draft["id"], "expired")
            return True, "That slot was just taken. Please suggest another date/time and I’ll check again.", None, None


        # Create pending request now
        req_id, public_ref = bookings_repo.create_booking_request(
            meta_phone_number_id=draft["meta_phone_number_id"],
            customer_number=customer_number,
            service_key=draft["service_key"],
            service_label=draft["service_label"],
            start_ts=draft["start_ts"],
            end_ts=draft["end_ts"],
        )
        bookings_repo.link_hold_to_request(draft["hold_id"], req_id)
        bookings_repo.mark_draft(customer_number, draft["id"], "confirmed")
        bookings_repo.clear_booking_context(customer_number)

        start_ts = draft["start_ts"]
        end_ts = draft["end_ts"]
        label = draft["service_label"]

        customer_reply = (
            "Booking request sent for confirmation.\n\n"
            f"Service: {label}\n"
            f"Date & Time: {_fmt_window(start_ts, end_ts)}\n\n"
            f"Reference: #{public_ref}\n\n"
            "We’ll notify you once the admin confirms."
        )

        admin_payload = {
            "request_id": req_id,
            "public_ref": public_ref,
            "customer_number": customer_number,
            "service_label": label,
            "start_ts": start_ts,
            "end_ts": end_ts,
        }
        return True, customer_reply, req_id, admin_payload

    if draft and _is_cancellation(user_text):
        bookings_repo.release_hold(draft["hold_id"])
        bookings_repo.mark_draft(customer_number, draft["id"], "cancelled")
        bookings_repo.clear_booking_context(customer_number)

        return True, "Okay — cancelled. If you want another slot, tell me your preferred date/time.", None, None

    # If there is a draft and user sends something else, prompt them
    if draft and draft["status"] == "proposed":
        start_ts = draft["start_ts"]
        end_ts = draft["end_ts"]
        label = draft["service_label"]
        return (
            True,
            f"Slot looks available:\n{label}\n{_fmt_window(start_ts, end_ts)}\n\nTap Confirm to proceed or Cancel to stop.",
            None,
            None,
        )

    # -------------------------
    # Stage 1: propose
    # -------------------------
    parsed = llm_parse_booking(user_text)
    
    # Merge partial context from previous message(s)
    ctx = bookings_repo.get_booking_context(customer_number) or {}

    # If current message has no service but we remembered one, restore it
    if (not parsed.service_key) and ctx.get("pending_service_key"):
        parsed.service_key = ctx["pending_service_key"]
        parsed.service_label = ctx.get("pending_service_label")

    # If current message has no datetime but we remembered one, restore it
    if (not parsed.start_local) and ctx.get("pending_start_local"):
        parsed.start_local = ctx["pending_start_local"]

    if parsed.intent != "booking" or parsed.confidence < 0.55:
        t = (user_text or "").lower()
        booking_related = any(w in t for w in ["book", "booking", "slot", "appointment", "come", "available", "time", "date"])
        if not booking_related:
            bookings_repo.clear_booking_context(customer_number)
        return False, "", None, None

    if not parsed.service_key or parsed.service_key not in SERVICE_CATALOG:
        # If we already have a datetime, remember it and only ask for service
        if parsed.start_local:
            bookings_repo.upsert_booking_context(customer_number, pending_start_local=parsed.start_local)
            return True, "Sure — what service do you need (car servicing / car wash / polishing)?", None, None

        return True, "Sure — what service do you need (car servicing / car wash / polishing) and what date & time?", None, None

    label, dur_min = SERVICE_CATALOG[parsed.service_key]
    parsed.service_label = label

    if not parsed.start_local:
        # Remember service so the next message "tomorrow 10am" works without asking again
        bookings_repo.upsert_booking_context(
            customer_number,
            pending_service_key=parsed.service_key,
            pending_service_label=label,
        )
        return True, f"Okay — what date and time would you like for {label}?", None, None

    start_ts = _parse_dt_local(parsed.start_local)
    end_ts = start_ts + timedelta(minutes=dur_min)

    # Business hours checks (same as before)
    if start_ts.weekday() == 6:
        return True, "We’re closed on Sundays. Can you pick a Mon–Sat time between 9am–6pm?", None, None
    if start_ts.hour < 9 or start_ts.hour >= 18 or end_ts.hour > 18 or (end_ts.hour == 18 and end_ts.minute > 0):
        return True, "Our booking hours are Mon–Sat, 9am–6pm. Can you choose a time within this window?", None, None

    if not bookings_repo.is_window_available(start_ts, end_ts):
        bookings_repo.upsert_booking_context(customer_number, pending_start_local=None)
        return True, _fmt_suggestions(parsed.service_key, start_ts), None, None

    # If user previously had a proposed draft, expire it to avoid stacking holds during testing
    prev = bookings_repo.get_active_draft(customer_number)
    if prev:
        bookings_repo.release_hold(prev["hold_id"])
        bookings_repo.mark_draft(customer_number, prev["id"], "expired")

    # Create hold + draft (NO admin notify yet)
    hold_id = bookings_repo.create_hold(
        customer_number=customer_number,
        service_key=parsed.service_key,
        start_ts=start_ts,
        end_ts=end_ts,
        hold_minutes=DEFAULT_HOLD_MINUTES,
    )

    draft_id = bookings_repo.create_draft(
        meta_phone_number_id=meta_phone_number_id,
        customer_number=customer_number,
        service_key=parsed.service_key,
        service_label=label,
        start_ts=start_ts,
        end_ts=end_ts,
        hold_id=hold_id,
        hold_minutes=DEFAULT_HOLD_MINUTES,
    )
    # Clear any previous partial context once we have a concrete proposal
    bookings_repo.clear_booking_context(customer_number)


    return (
        True,
        f"Slot looks available:\n{label}\n{_fmt_window(start_ts, end_ts)}\n\nWould you like to proceed? Tap Confirm to proceed or Cancel to stop.",
        None,
        None,
    )
