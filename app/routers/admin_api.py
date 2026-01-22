import os
from fastapi import APIRouter, Request, HTTPException
from app.db.messages_repo import list_phone_numbers, fetch_messages
from app.config.vectorize_txt import convert_project_to_vector_db

router = APIRouter(prefix="/api", tags=["admin-api"])

from app.services.admin_kb import (
    add_text_to_vectordb,
)
from app.services.chroma_store import get_collection_for_default_project


def _require_admin(request: Request):
    # Simple protection so random people can't read your logs.
    # Set ADMIN_DASH_TOKEN in Railway Variables.
    token = os.getenv("ADMIN_DASH_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="ADMIN_DASH_TOKEN not set")

    got = request.headers.get("X-Admin-Token")
    if got != token:
        raise HTTPException(status_code=403, detail="Forbidden")

@router.get("/numbers")
def api_numbers(request: Request, limit: int = 200):
    _require_admin(request)

    items = list_phone_numbers(limit=limit)

    totals = {
        "in_count": sum(int(i.get("in_count", 0) or 0) for i in items),
        "out_count": sum(int(i.get("out_count", 0) or 0) for i in items),
    }

    return {"items": items, "totals": totals}

@router.get("/messages")
def api_messages(
    request: Request,
    phone_number: str | None = None,
    direction: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    _require_admin(request)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return {"items": fetch_messages(phone_number=phone_number, direction=direction, limit=limit, offset=offset)}


@router.get("/admin/kb/status")
def kb_status(request: Request):
    _require_admin(request)
    col = get_collection_for_default_project()
    return {
        "collection": col.name,
        "count": col.count(),
    }


@router.post("/admin/kb/add")
def kb_add(request: Request, payload: dict):
    _require_admin(request)
    text = payload.get("text")
    source = payload.get("source", "admin")

    if not text:
        raise HTTPException(status_code=400, detail="Text required")

    doc_id = add_text_to_vectordb(text=text, source=source)
    return {"ok": True, "id": doc_id}


@router.post("/admin/kb/rebuild")
def kb_rebuild(request: Request):
    _require_admin(request)
    convert_project_to_vector_db()
    return {"ok": True}
