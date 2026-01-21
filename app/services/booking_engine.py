from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import app.config.settings as settings
from app.db import bookings_repo

SG_TZ = ZoneInfo("Asia/Singapore")


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
    draft = bookings_repo.get_active_draft(customer_number)

    if draft and _is_confirmation(user_text):
        # Create pending request now
        req_id = bookings_repo.create_booking_request(
            meta_phone_number_id=draft["meta_phone_number_id"],
            customer_number=customer_number,
            service_key=draft["service_key"],
            service_label=draft["service_label"],
            start_ts=draft["start_ts"],
            end_ts=draft["end_ts"],
        )
        bookings_repo.link_hold_to_request(draft["hold_id"], req_id)
        bookings_repo.mark_draft(customer_number, draft["id"], "confirmed")

        start_ts = draft["start_ts"]
        end_ts = draft["end_ts"]
        label = draft["service_label"]

        customer_reply = (
            f"Got it — I’ve sent this to admin for confirmation:\n"
            f"{label}\n"
            f"{start_ts.strftime('%a %d %b %Y, %H:%M')}–{end_ts.strftime('%H:%M')}\n"
            f"(Ref #{req_id})"
        )

        admin_payload = {
            "request_id": req_id,
            "customer_number": customer_number,
            "service_label": label,
            "start_ts": start_ts,
            "end_ts": end_ts,
        }
        return True, customer_reply, req_id, admin_payload

    if draft and _is_cancellation(user_text):
        bookings_repo.release_hold(draft["hold_id"])
        bookings_repo.mark_draft(customer_number, draft["id"], "cancelled")
        return True, "Okay — cancelled. If you want another slot, tell me your preferred date/time.", None, None

    # If there is a draft and user sends something else, prompt them
    if draft:
        start_ts = draft["start_ts"]
        end_ts = draft["end_ts"]
        label = draft["service_label"]
        return (
            True,
            f"Slot looks available:\n{label}\n{start_ts.strftime('%a %d %b %Y, %H:%M')}–{end_ts.strftime('%H:%M')}\n\nReply YES to proceed, or CANCEL to stop.",
            None,
            None,
        )

    # -------------------------
    # Stage 1: propose
    # -------------------------
    parsed = llm_parse_booking(user_text)
    if parsed.intent != "booking" or parsed.confidence < 0.55:
        return False, "", None, None

    if not parsed.service_key or parsed.service_key not in SERVICE_CATALOG:
        return True, "Sure — what service do you need (car servicing / car wash / polishing) and what date & time?", None, None

    label, dur_min = SERVICE_CATALOG[parsed.service_key]
    parsed.service_label = label

    if not parsed.start_local:
        return True, f"Okay — what date and time would you like for {label}?", None, None

    start_ts = _parse_dt_local(parsed.start_local)
    end_ts = start_ts + timedelta(minutes=dur_min)

    # Business hours checks (same as before)
    if start_ts.weekday() == 6:
        return True, "We’re closed on Sundays. Can you pick a Mon–Sat time between 9am–6pm?", None, None
    if start_ts.hour < 9 or start_ts.hour >= 18 or end_ts.hour > 18 or (end_ts.hour == 18 and end_ts.minute > 0):
        return True, "Our booking hours are Mon–Sat, 9am–6pm. Can you choose a time within this window?", None, None

    if not bookings_repo.is_window_available(start_ts, end_ts):
        return True, "That slot is not available. Can you suggest another time (or a range like ‘Tuesday afternoon’)?", None, None

    # Create hold + draft (NO admin notify yet)
    hold_id = bookings_repo.create_hold(
        customer_number=customer_number,
        service_key=parsed.service_key,
        start_ts=start_ts,
        end_ts=end_ts,
        hold_minutes=DEFAULT_HOLD_MINUTES,
    )

    bookings_repo.create_draft(
        meta_phone_number_id=meta_phone_number_id,
        customer_number=customer_number,
        service_key=parsed.service_key,
        service_label=label,
        start_ts=start_ts,
        end_ts=end_ts,
        hold_id=hold_id,
        hold_minutes=DEFAULT_HOLD_MINUTES,
    )

    return (
        True,
        f"Slot looks available:\n{label}\n{start_ts.strftime('%a %d %b %Y, %H:%M')}–{end_ts.strftime('%H:%M')}\n\nWould you like to proceed? Reply YES to confirm, or CANCEL to stop.",
        None,
        None,
    )
