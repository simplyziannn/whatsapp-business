from fastapi import FastAPI
from fastapi import Request
from fastapi import BackgroundTasks
from fastapi import HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
load_dotenv()
from fastapi.staticfiles import StaticFiles
import os
import app.config.settings as settings
from app.routers.frontend import router as frontend_router
from contextlib import asynccontextmanager
from app.db.messages_repo import db_init
from app.routers.admin_api import router as admin_api_router
from app.routers.debug_api import router as debug_router
from app.routers.admin_debug_api import router as admin_debug_router
from app.services.webhook_handler import process_webhook_payload
from app.services.kb_init import kb_init_if_empty

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

#postgres
@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    kb_init_if_empty()
    yield
    
app = FastAPI(lifespan=lifespan)
if os.path.isdir(FRONTEND_DIR):
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    print(f"[WARN] frontend folder not found at: {FRONTEND_DIR} (skipping /frontend mount)")

PERF_LOG_FILE = os.getenv("PERF_LOG_FILE", "perf.log")
ADMIN_LOG_FILE = os.getenv("ADMIN_LOG_FILE", "app/admin_actions.log")
DISABLE_KB_CACHE = os.getenv("DISABLE_KB_CACHE", "0") == "1"
app.include_router(frontend_router)
app.include_router(admin_api_router)
app.include_router(debug_router)
app.include_router(admin_debug_router)

# -------------------------------------------------------------------
# FastAPI endpoints
# -------------------------------------------------------------------

@app.get("/debug/routes")
async def debug_routes():
    out = []
    for r in app.router.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None) or str(r)

        if methods:
            out.append(f"{sorted(list(methods))} {path}")
        else:
            out.append(f"[NO_METHODS] {path}")

    return sorted(out)

@app.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)

    raise HTTPException(status_code=403, detail="Forbidden")

@app.post("/webhook/whatsapp")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives all incoming WhatsApp messages. ACK fast to stop Meta retries."""
    body = await request.json()
    print("Incoming webhook: keys=", list(body.keys()))

    background_tasks.add_task(process_webhook_payload, body, ADMIN_LOG_FILE, PERF_LOG_FILE, DISABLE_KB_CACHE)
    return {"status": "ok"}

# uvicorn app.main:app --app-dir . --host 0.0.0.0 --port 8000
# in second terminal: ngrok http 8000
# frontend -> http://127.0.0.1:8000/frontend/index.html
if __name__ == "__main__":
    print("TOKEN PREFIX:", (settings.ACCESS_TOKEN or "")[:12])
    print("PHONE_NUMBER_ID USED:", settings.PHONE_NUMBER_ID)
