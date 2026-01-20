import time
from fastapi import APIRouter
from app.services import kb_cache
from app.services.chroma_store import retrieve_context_from_vectordb

router = APIRouter()

@router.post("/debug/cache_test")
async def debug_cache_test(payload: dict):
    from_number = str(payload.get("from_number", "6599999999"))
    text = str(payload.get("text", "")).strip()
    disable_cache = bool(payload.get("disable_cache", False))

    if not text:
        return {"ok": False, "error": "text is required"}

    t_total0 = time.perf_counter()
    t_retrieval0 = time.perf_counter()

    context, cache_hit = kb_cache.get_cached_context(
        from_number=from_number,
        question=text,
        retrieve_fn=retrieve_context_from_vectordb,
        k=5,
        force_refresh=disable_cache,
        return_meta=True,
    )

    t_retrieval_ms = (time.perf_counter() - t_retrieval0) * 1000.0
    t_total_ms = (time.perf_counter() - t_total0) * 1000.0

    return {
        "ok": True,
        "from_number": from_number,
        "disable_cache": disable_cache,
        "cache_hit": cache_hit,
        "context_len": len(context or ""),
        "t_retrieval_ms": round(t_retrieval_ms, 2),
        "t_total_ms": round(t_total_ms, 2),
    }
