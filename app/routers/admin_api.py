from fastapi import APIRouter, Request, HTTPException
from app.db.messages_repo import list_phone_numbers_scoped, fetch_messages
from app.config.vectorize_txt import convert_project_to_vector_db
from app.services.chroma_store import get_collection
from app.services.auth import require_user

router = APIRouter(prefix="/api", tags=["admin-api"])

from app.services.admin_kb import (
    add_text_to_vectordb,
)
@router.get("/numbers")
def api_numbers(request: Request, limit: int = 200, all_companies: int = 0, company_id: int | None = None):
    user = require_user(request, roles=["company_user", "platform_admin"])

    scope_company_id = None
    if user["role"] == "platform_admin":
        if company_id is not None:
            scope_company_id = company_id
        elif not all_companies:
            scope_company_id = user["company_id"]
    else:
        scope_company_id = user["company_id"]

    items = list_phone_numbers_scoped(limit=limit, company_id=scope_company_id)

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
    all_companies: int = 0,
    company_id: int | None = None,
):
    user = require_user(request, roles=["company_user", "platform_admin"])
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    scope_company_id = None
    if user["role"] == "platform_admin":
        if company_id is not None:
            scope_company_id = company_id
        elif not all_companies:
            scope_company_id = user["company_id"]
    else:
        scope_company_id = user["company_id"]
    return {"items": fetch_messages(company_id=scope_company_id, phone_number=phone_number, direction=direction, limit=limit, offset=offset)}


@router.get("/admin/kb/status")
def kb_status(request: Request):
    require_user(request, roles=["company_user", "platform_admin"])
    cols = ["kb_menu", "kb_contact", "kb_general"]
    return {
        "collections": [{"name": c, "count": get_collection(c).count()} for c in cols]
    }


@router.post("/admin/kb/add")
def kb_add(request: Request, payload: dict):
    require_user(request, roles=["company_user", "platform_admin"])
    text = payload.get("text")
    source = payload.get("source", "admin")
    kb_type = payload.get("kb_type", "kb_general")  # default

    if not text:
        raise HTTPException(status_code=400, detail="Text required")

    doc_id = add_text_to_vectordb(text=text, kb_type=kb_type, source=source)
    return {"ok": True, "id": doc_id}



@router.post("/admin/kb/rebuild")
def kb_rebuild(request: Request):
    require_user(request, roles=["platform_admin"])
    convert_project_to_vector_db()
    return {"ok": True}
