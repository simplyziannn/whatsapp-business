from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse

router = APIRouter()

# -----------------------------
# Frontend redirect
# -----------------------------
@router.get("/")
async def root():
    return RedirectResponse(url="/frontend/index.html")


# -----------------------------
# Debug endpoints
# -----------------------------
@router.get("/debug/routes")
async def debug_routes(request: Request):
    # Use request.app instead of importing the app object
    return sorted([f"{r.methods} {r.path}" for r in request.app.router.routes])


@router.post("/debug/cache_test")
async def debug_cache_test(payload: dict):
    from app import main as m  # local import avoids circular import

    from_number = str(payload.get("from_number", "6599999999"))
    text = str(payload.get("text", "")).strip()
    disable_cache = bool(payload.get("disable_cache", False))

    if not text:
        return {"ok": False, "error": "text is required"}

    t_total0 = m.time.perf_counter()
    t_retrieval0 = m.time.perf_counter()

    context, cache_hit = m.get_cached_context(
        from_number=from_number,
        question=text,
        k=5,
        force_refresh=disable_cache,
        return_meta=True,
    )

    t_retrieval_ms = (m.time.perf_counter() - t_retrieval0) * 1000.0
    t_total_ms = (m.time.perf_counter() - t_total0) * 1000.0

    return {
        "ok": True,
        "from_number": from_number,
        "disable_cache": disable_cache,
        "cache_hit": cache_hit,
        "context_len": len(context or ""),
        "t_retrieval_ms": round(t_retrieval_ms, 2),
        "t_total_ms": round(t_total_ms, 2),
    }


# -----------------------------
# WhatsApp webhook endpoints
# -----------------------------
@router.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    from app import main as m

    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == m.settings.VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)

    raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/webhook/whatsapp")
async def webhook(request: Request):
    from app import main as m
    # Reuse your existing logic by calling a function in main (we’ll create it in step 2)
    return await m.handle_whatsapp_webhook(request)


# -----------------------------
# Admin endpoints
# -----------------------------
@router.get("/admin/cache_status")
async def admin_cache_status(request: Request):
    from app import main as m

    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

    with m.cache_lock:
        keys = list(m.conversation_contexts.keys())
        details = {k: {"version": v["version"], "ts": v["ts"]} for k, v in m.conversation_contexts.items()}

    return {"kb_version": m.kb_version, "keys": keys, "details": details}


@router.get("/admin/config")
async def admin_config(request: Request):
    from app import main as m

    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

    return {
        "admin_numbers": sorted(list(m.settings.ADMIN_NUMBERS)),
        "project_name": m.PROJECT_NAME,
        "phone_number_id": m.settings.PHONE_NUMBER_ID,
    }


@router.post("/admin/clear_history")
async def admin_clear_history(request: Request):
    from app import main as m

    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    phone = body.get("phone")
    if not phone:
        raise HTTPException(status_code=400, detail="Missing phone")

    m.clear_conversation(phone)
    return {"ok": True, "cleared": phone}
