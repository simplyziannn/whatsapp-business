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


def try_create_pending_booking(meta_phone_number_id: str, customer_number: str, user_text: str) -> tuple[bool, str]:
    """
    Returns (handled, reply_text).
    If handled=True, caller should send reply_text and return.
    """
    # Keep holds tidy
    bookings_repo.expire_old_holds()

    parsed = llm_parse_booking(user_text)

    if parsed.intent != "booking" or parsed.confidence < 0.55:
        return (False, "")

    if not parsed.service_key or parsed.service_key not in SERVICE_CATALOG:
        return (True, "Sure — what service do you need (car servicing / car wash / polishing) and what date & time?")

    label, dur_min = SERVICE_CATALOG[parsed.service_key]
    parsed.service_label = label

    if not parsed.start_local:
        return (True, f"Okay — what date and time would you like for {label}?")

    start_ts = _parse_dt_local(parsed.start_local)
    end_ts = start_ts + timedelta(minutes=dur_min)

    # Business hours gate (simple v1): Mon–Sat 09:00–18:00 start time, end must be <= 18:00
    if start_ts.weekday() == 6:
        return (True, "We’re closed on Sundays. Can you pick a Mon–Sat time between 9am–6pm?")
    if start_ts.hour < 9 or start_ts.hour >= 18 or end_ts.hour > 18 or (end_ts.hour == 18 and end_ts.minute > 0):
        return (True, "Our booking hours are Mon–Sat, 9am–6pm. Can you choose a time within this window?")

    if not bookings_repo.is_window_available(start_ts, end_ts):
        return (True, "That slot is not available. Can you suggest another time (or a range like ‘Tuesday afternoon’)?")

    # Create hold + pending request
    hold_id = bookings_repo.create_hold(
        customer_number=customer_number,
        service_key=parsed.service_key,
        start_ts=start_ts,
        end_ts=end_ts,
        hold_minutes=DEFAULT_HOLD_MINUTES,
    )

    req_id = bookings_repo.create_booking_request(
        meta_phone_number_id=meta_phone_number_id,
        customer_number=customer_number,
        service_key=parsed.service_key,
        service_label=label,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    bookings_repo.link_hold_to_request(hold_id, req_id)

    # Customer reply (pending admin)
    start_str = start_ts.strftime("%a %d %b %Y, %H:%M")
    end_str = end_ts.strftime("%H:%M")
    return (True, f"Slot looks available: {start_str}–{end_str} for {label}. Pending admin confirmation — I’ll update you shortly. (Ref #{req_id})")
