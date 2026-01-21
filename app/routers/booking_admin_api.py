import os
from fastapi import APIRouter, Request, HTTPException
from app.db import bookings_repo
from app.services.whatsapp_client import send_whatsapp_message

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


@router.post("/{request_id}/approve")
def approve(request: Request, request_id: int, admin_note: str | None = None):
    _require_admin(request)

    admin_number = request.headers.get("X-Admin-Actor", "admin")

    req = bookings_repo.get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    ok = bookings_repo.decide_request(request_id, admin_number, "approved", admin_note)
    if not ok:
        raise HTTPException(status_code=409, detail="Request already decided or not pending")

    # release hold (optional; approved booking itself blocks overlap)
    hold_id = bookings_repo.find_hold_by_request(request_id)
    if hold_id:
        bookings_repo.release_hold(hold_id)

    start_ts = req["start_ts"]
    end_ts = req["end_ts"]
    label = req["service_label"]

    msg = (
        f"Confirmed ✅\n"
        f"{label}\n"
        f"{start_ts.strftime('%a %d %b %Y, %H:%M')}–{end_ts.strftime('%H:%M')}\n"
        f"Ref #{request_id}"
    )
    send_whatsapp_message(req["meta_phone_number_id"], req["customer_number"], msg)
    return {"ok": True}


@router.post("/{request_id}/reject")
def reject(request: Request, request_id: int, admin_note: str | None = None):
    _require_admin(request)

    admin_number = request.headers.get("X-Admin-Actor", "admin")

    req = bookings_repo.get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    ok = bookings_repo.decide_request(request_id, admin_number, "rejected", admin_note)
    if not ok:
        raise HTTPException(status_code=409, detail="Request already decided or not pending")

    hold_id = bookings_repo.find_hold_by_request(request_id)
    if hold_id:
        bookings_repo.release_hold(hold_id)

    msg = (
        f"Sorry — that slot couldn’t be confirmed.\n"
        f"Please suggest another date/time and I’ll check availability.\n"
        f"(Ref #{request_id})"
    )
    send_whatsapp_message(req["meta_phone_number_id"], req["customer_number"], msg)
    return {"ok": True}
