import os
from fastapi import APIRouter, HTTPException, Request

from app.services import kb_cache
from app.services import history as history_store
from app.config.helpers import get_project_paths, PROJECT_NAME, COLLECTION_NAME, EMBED_MODEL
import app.config.settings as settings

router = APIRouter()

def _require_test_admin(request: Request):
    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

@router.get("/admin/cache_status")
async def admin_cache_status(request: Request):
    _require_test_admin(request)
    return kb_cache.cache_status()

@router.get("/admin/kb_status")
async def admin_kb_status(request: Request):
    _require_test_admin(request)
    _, db_path = get_project_paths(PROJECT_NAME)

    kb_txt = "Knowledge_Base/AutoSpritze/txt/AutoSpritze_Web.txt"
    kb_vec = "Knowledge_Base/AutoSpritze/vectordb"

    return {
        "cwd": os.getcwd(),
        "kb_txt_exists": os.path.exists(kb_txt),
        "kb_txt_size": os.path.getsize(kb_txt) if os.path.exists(kb_txt) else None,
        "kb_vec_exists": os.path.exists(kb_vec),
        "kb_vec_files": os.listdir(kb_vec) if os.path.exists(kb_vec) else [],
        "db_path": db_path,
        "db_path_exists": os.path.exists(db_path),
        "db_path_files": os.listdir(db_path) if os.path.exists(db_path) else [],
    }

@router.get("/admin/kb_debug_collection")
async def admin_kb_debug_collection(request: Request):
    _require_test_admin(request)

    import chromadb
    from chromadb.config import Settings

    _, persist_dir = get_project_paths(PROJECT_NAME)
    client = chromadb.PersistentClient(path=persist_dir, settings=Settings(allow_reset=False))

    cols = client.list_collections()
    names = [c.name for c in cols]
    out = {"persist_dir": persist_dir, "collections": names, "counts": {}}

    for name in names:
        col = client.get_collection(name=name)
        out["counts"][name] = col.count()

    return out

@router.get("/admin/config")
async def admin_config(request: Request):
    _require_test_admin(request)
    return {
        "admin_numbers": sorted(list(settings.ADMIN_NUMBERS)),
        "project_name": PROJECT_NAME,
        "phone_number_id": settings.PHONE_NUMBER_ID,
        "collection_name": COLLECTION_NAME,
        "embed_model": EMBED_MODEL,
    }

@router.post("/admin/clear_history")
async def admin_clear_history(request: Request):
    _require_test_admin(request)
    body = await request.json()
    phone = body.get("phone")
    if not phone:
        raise HTTPException(status_code=400, detail="Missing phone")

    history_store.clear(phone)
    return {"ok": True, "cleared": phone}
