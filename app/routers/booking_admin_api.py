import os
from fastapi import APIRouter, Request, HTTPException
from app.db import bookings_repo
from app.services.whatsapp_client import send_whatsapp_message


from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SG_TZ = ZoneInfo("Asia/Singapore")

def _to_sg(dt: datetime) -> datetime:
    if dt is None:
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SG_TZ)

def _fmt_window(start_ts: datetime, end_ts: datetime) -> str:
    s = _to_sg(start_ts)
    e = _to_sg(end_ts)
    return f"{s.strftime('%a %d %b %Y, %H:%M')}–{e.strftime('%H:%M')}"


router = APIRouter(prefix="/api/bookings", tags=["booking-admin"])


def _require_admin(request: Request):
    token = os.getenv("ADMIN_DASH_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="ADMIN_DASH_TOKEN not set")

    got = request.headers.get("X-Admin-Token")
    if got != token:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/pending")
def list_pending(request: Request, limit: int = 50):
    _require_admin(request)
    limit = max(1, min(limit, 200))
    return {"items": bookings_repo.list_pending_requests(limit=limit)}

@router.get("/requests")
def list_requests(request: Request, status: str = "all", limit: int = 50):
    _require_admin(request)
    limit = max(1, min(limit, 200))

    allowed = {"all", "pending", "approved", "rejected", "expired", "cancelled"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use one of: {sorted(allowed)}")

    return {"items": bookings_repo.list_requests(status=status, limit=limit)}

@router.post("/{ref}/approve")
def approve(request: Request, ref: str, admin_note: str | None = None):
    _require_admin(request)

    admin_number = request.headers.get("X-Admin-Actor", "admin")

    req_id = bookings_repo.resolve_request_id(ref)
    if not req_id:
        raise HTTPException(status_code=404, detail="Request not found")

    req = bookings_repo.get_request(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    ok = bookings_repo.decide_request(req_id, admin_number, "approved", admin_note)
    if not ok:
        raise HTTPException(status_code=409, detail="Request already decided or not pending")

    # release hold (optional; approved booking itself blocks overlap)
    hold_id = bookings_repo.find_hold_by_request(req_id)
    if hold_id:
        bookings_repo.release_hold(hold_id)

    start_ts = req["start_ts"]
    end_ts = req["end_ts"]
    label = req["service_label"]

    ref_out = req.get("public_ref") or str(req.get("id"))

    msg = (
        "Confirmed ✅\n"
        f"{label}\n"
        f"{_fmt_window(start_ts, end_ts)}\n"
        f"Ref #{ref_out}"
    )

    send_whatsapp_message(req["meta_phone_number_id"], req["customer_number"], msg)
    return {"ok": True}


@router.post("/{ref}/reject")
def reject(request: Request, ref: str, admin_note: str | None = None):
    _require_admin(request)

    admin_number = request.headers.get("X-Admin-Actor", "admin")

    req_id = bookings_repo.resolve_request_id(ref)
    if not req_id:
        raise HTTPException(status_code=404, detail="Request not found")

    req = bookings_repo.get_request(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    ok = bookings_repo.decide_request(req_id, admin_number, "rejected", admin_note)
    if not ok:
        raise HTTPException(status_code=409, detail="Request already decided or not pending")

    hold_id = bookings_repo.find_hold_by_request(req_id)
    if hold_id:
        bookings_repo.release_hold(hold_id)

    ref_out = req.get("public_ref") or str(req.get("id"))

    msg = (
        "Sorry — that slot couldn’t be confirmed.\n"
        "Please suggest another date/time and I’ll check availability.\n"
        f"(Ref #{ref_out})"
    )
    # IMPORTANT: do NOT include admin_note in customer message

    send_whatsapp_message(req["meta_phone_number_id"], req["customer_number"], msg)
    return {"ok": True}

@router.post("/{ref}/cancel")
def cancel(request: Request, ref: str, admin_note: str | None = None):
    _require_admin(request)

    admin_number = request.headers.get("X-Admin-Actor", "admin")

    req_id = bookings_repo.resolve_request_id(ref)
    if not req_id:
        raise HTTPException(status_code=404, detail="Request not found")

    req = bookings_repo.get_request(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    ok = bookings_repo.cancel_request(req_id, admin_number, admin_note)
    if not ok:
        raise HTTPException(status_code=409, detail="Only approved bookings can be cancelled")

    start_ts = req["start_ts"]
    end_ts = req["end_ts"]
    label = req["service_label"]
    ref_out = req.get("public_ref") or str(req.get("id"))

    msg = (
        "Booking cancelled ❌\n"
        f"{label}\n"
        f"{_fmt_window(start_ts, end_ts)}\n"
        f"Ref #{ref_out}"
    )
    # IMPORTANT: do NOT include admin_note in customer message

    send_whatsapp_message(req["meta_phone_number_id"], req["customer_number"], msg)
    return {"ok": True}
