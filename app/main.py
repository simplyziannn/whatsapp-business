from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse, RedirectResponse
from dotenv import load_dotenv
load_dotenv()
from openai import OpenAI
import chromadb
from chromadb.config import Settings
from fastapi.staticfiles import StaticFiles
from datetime import datetime
import json, uuid, os, requests, threading , time
from app.config.helpers import get_project_paths, EMBED_MODEL, COLLECTION_NAME, PROJECT_NAME, get_open_status_sg
import app.config.settings as settings
from app.routers.frontend import router as frontend_router
import psycopg2 #postgres
from contextlib import asynccontextmanager
from app.db.messages_repo import db_init, log_message, claim_inbound_message_id
from app.routers.admin_api import router as admin_api_router

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
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
else:
    print(f"[WARN] frontend folder not found at: {FRONTEND_DIR} (skipping /frontend mount)")

PERF_LOG_FILE = os.getenv("PERF_LOG_FILE", "perf.log")
ADMIN_LOG_FILE = os.getenv("ADMIN_LOG_FILE", "app/admin_actions.log")
DISABLE_KB_CACHE = os.getenv("DISABLE_KB_CACHE", "0") == "1"
app.include_router(frontend_router)
app.include_router(admin_api_router)


# -----------------------------
# ACK deduplication for inbound messages
# -----------------------------
processed_inbound_ids = {}
processed_lock = threading.Lock()
PROCESSED_TTL = 24 * 3600  # 24h


def _seen_recent(msg_id: str) -> bool:
    now = time.time()
    with processed_lock:
        # cleanup
        old_keys = [k for k, ts in processed_inbound_ids.items() if (now - ts) > PROCESSED_TTL]
        for k in old_keys:
            processed_inbound_ids.pop(k, None)

        if msg_id in processed_inbound_ids:
            return True

        processed_inbound_ids[msg_id] = now
        return False


# -----------------------------
# postgres configs
# -----------------------------


def kb_init_if_empty():
    txt_folder, persist_dir = get_project_paths(PROJECT_NAME)

    print("[KB_INIT] PROJECT_NAME:", PROJECT_NAME)
    print("[KB_INIT] txt_folder:", txt_folder)
    print("[KB_INIT] txt_folder files:", os.listdir(txt_folder) if os.path.exists(txt_folder) else "MISSING")
    print("[KB_INIT] persist_dir:", persist_dir)
    print("[KB_INIT] persist_dir files:", os.listdir(persist_dir) if os.path.exists(persist_dir) else "MISSING")

    client = chromadb.PersistentClient(path=persist_dir, settings=Settings(allow_reset=False))
    cols = client.list_collections()

    if not cols:
        print("[KB_INIT] No Chroma collections found. Rebuilding from txt...")
        from app.config.vectorize_txt import convert_txt_folder_to_vector_db
        convert_txt_folder_to_vector_db(txt_folder, persist_dir)

        # re-check after rebuild
        cols = client.list_collections()
        print("[KB_INIT] Collections after rebuild:", [c.name for c in cols])
        print("[KB_INIT] Rebuild complete.")
    else:
        print("[KB_INIT] Chroma collections exist:", [c.name for c in cols])


# -----------------------------
# KB cache
# -----------------------------
kb_version = 0
# cache key format: "{from_number}|k={k}" -> {"context": str, "version": int, "ts": float}
conversation_contexts: dict = {}
cache_lock = threading.Lock()
#CACHE_MAX_AGE = int(os.getenv("KB_CACHE_MAX_AGE", str(60 * 60)))  # seconds


def bump_kb_version():
    """Bump KB version and clear in-memory cache so contexts are refreshed."""
    global kb_version
    with cache_lock:
        kb_version += 1
        conversation_contexts.clear()


def _context_cache_key(from_number: str, k: int) -> str:
    return f"{from_number}|k={k}"


def get_cached_context(
    from_number: str,
    question: str,
    k: int = 5,
    force_refresh: bool = False,
    return_meta: bool = False,
):
    """Return cached context for this user+k if still valid; otherwise fetch and cache.
    If return_meta=True, returns (context, cache_hit: bool).
    """
    key = _context_cache_key(from_number, k)
    now = time.time()

    with cache_lock:
        entry = conversation_contexts.get(key)
        if (
            not force_refresh
            and entry
            and entry.get("version") == kb_version
            and (now - entry.get("ts", 0)) < settings.CACHE_MAX_AGE
        ):
            ctx = entry.get("context", "")
            return (ctx, True) if return_meta else ctx

    # Cache miss / stale: fetch and store
    context = retrieve_context_from_vectordb(question, k=k)
    with cache_lock:
        conversation_contexts[key] = {"context": context, "version": kb_version, "ts": now}

    return (context, False) if return_meta else context



def clear_cached_context(from_number: str | None = None, k: int | None = None):
    """Clear cached contexts selectively.

    - If both `from_number` and `k` are provided, clear only that specific entry.
    - If `from_number` is provided and `k` is None, clear all entries for that phone number.
    - If `from_number` is None, clear the entire cache.
    """
    with cache_lock:
        if from_number is None:
            conversation_contexts.clear()
            return
        if k is not None:
            key = _context_cache_key(from_number, k)
            conversation_contexts.pop(key, None)
            return
        # remove all entries matching this from_number
        keys_to_remove = [kk for kk in conversation_contexts.keys() if kk.startswith(f"{from_number}|k=")]
        for kk in keys_to_remove:
            conversation_contexts.pop(kk, None)


# -----------------------------
# Conversation history (in-memory) with TTL
# -----------------------------
# { "whatsapp_number": [ {"role": "user"/"assistant", "content": "..."} , ... ] }
conversation_history = {}
conversation_last_activity: dict = {}  # phone_number -> last_activity_ts
#MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "12"))  # total messages (user+assistant), keep it small
#HISTORY_MAX_AGE = int(os.getenv("HISTORY_MAX_AGE", str(24 * 3600)))  # seconds; default 24 hours


def _is_history_stale(from_number: str) -> bool:
    last = conversation_last_activity.get(from_number)
    if last is None:
        return False
    return (time.time() - last) > settings.HISTORY_MAX_AGE


def touch_conversation(from_number: str):
    conversation_last_activity[from_number] = time.time()


def clear_conversation(from_number: str):
    conversation_history.pop(from_number, None)
    conversation_last_activity.pop(from_number, None)



def send_whatsapp_message(phone_number_id: str, to: str, text: str):
    url = f"https://graph.facebook.com/v24.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    resp = requests.post(url, headers=headers, json=data, timeout=10)
    print("WhatsApp send status:", resp.status_code, resp.text)


# -------------------------------------------------------------------
# Vector DB access for the single default project
# -------------------------------------------------------------------

_collection = None


def get_collection_for_default_project():
    """Lazy-load a Chroma collection for the default project."""
    global _collection

    if _collection is not None:
        return _collection

    _, db_path = get_project_paths(PROJECT_NAME)

    chroma_client = chromadb.PersistentClient(
        path=db_path,
        settings=Settings(allow_reset=False),
    )
    _collection = chroma_client.get_or_create_collection(name=COLLECTION_NAME)
    print(f"[INFO] Using collection '{COLLECTION_NAME}' at {db_path}")

    return _collection

def retrieve_context_from_vectordb(question: str, k: int = 5) -> str:
    """
    Given a user question, query the default project's vector DB
    and return a formatted context string.
    If anything fails, returns an empty string (so we can gracefully fallback).
    """
    try:
        collection = get_collection_for_default_project()

        emb_resp = settings.client.embeddings.create(
            model=EMBED_MODEL,
            input=[question],
        )
        q_vec = emb_resp.data[0].embedding

        results = collection.query(
            query_embeddings=[q_vec],
            n_results=k,
        )

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]

        if not docs:
            return ""

        context_parts = []
        for doc, meta in zip(docs, metas):
            src = meta.get("source_file") or meta.get("url") or "unknown source"
            context_parts.append(f"Source: {src}\n{doc}")

        return "\n\n---\n\n".join(context_parts)

    except Exception as e:
        print("[WARN] retrieve_context_from_vectordb failed:", e)
        return ""


# -----------------------------------------------------
# Admin CRUD operations for vector DB
# -----------------------------------------------------

def add_text_to_vectordb(text: str, source: str = "admin"):
    """Embed text and store it as a new document in the vectordb."""
    collection = get_collection_for_default_project()

    emb = settings.client.embeddings.create(
        model=EMBED_MODEL,
        input=[text]
    ).data[0].embedding

    doc_id = f"admin_{uuid.uuid4().hex}"

    collection.add(
        ids=[doc_id],
        embeddings=[emb],
        documents=[text],
        metadatas=[{"source_file": source}]
    )
    # Invalidate KB cache so future queries refresh context
    try:
        bump_kb_version()
    except Exception:
        pass
    return doc_id

def delete_by_id(doc_id: str):
    """
    Delete a single document by its ID and return info about what was deleted.
    Returns:
      dict with {"doc_id": ..., "content": ..., "metadata": {...}}
      or None if nothing was found.
    """
    try:
        collection = get_collection_for_default_project()

        # Fetch the document BEFORE deleting
        result = collection.get(ids=[doc_id])
        docs = result.get("documents", [])
        metas = result.get("metadatas", [])

        if not docs:
            return None  # nothing to delete

        deleted_entry = {
            "doc_id": doc_id,
            "content": docs[0],
            "metadata": metas[0] if metas else {},
        }

        # Now actually delete
        collection.delete(ids=[doc_id])
        # Invalidate KB cache so future queries refresh context
        try:
            bump_kb_version()
        except Exception:
            pass

        return deleted_entry

    except Exception as e:
        print("Delete-by-ID error:", e)
        return None


# -------------------------------------------------------------------
# Admin action logging
# -------------------------------------------------------------------

def log_admin_action(admin_number: str, action: str, details: dict):
    """
    Append a pretty JSON block describing an admin action.
    Example:
    {
      "timestamp": "...",
      "admin_number": "6594...",
      "action": "ADD_ENTRY",
      "entry_details": { ... }
    }
    """
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "admin_number": admin_number,
        "action": action,
        "entry_details": details,
    }
    try:
        with open(ADMIN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, indent=2) + "\n\n")
    except Exception as e:
        print("[WARN] Failed to write admin log:", e)


# -------------------------------------------------------------------
# FastAPI endpoints
# -------------------------------------------------------------------

from fastapi import HTTPException
from fastapi.responses import PlainTextResponse


@app.get("/debug/routes")
async def debug_routes():
    return sorted([f"{r.methods} {r.path}" for r in app.router.routes])

@app.post("/debug/cache_test")
async def debug_cache_test(payload: dict):
    """
    Debug endpoint to test KB cache without WhatsApp/Meta and without OpenAI.
    """
    from_number = str(payload.get("from_number", "6599999999"))
    text = str(payload.get("text", "")).strip()
    disable_cache = bool(payload.get("disable_cache", False))

    if not text:
        return {"ok": False, "error": "text is required"}

    t_total0 = time.perf_counter()
    t_retrieval0 = time.perf_counter()

    context, cache_hit = get_cached_context(
        from_number=from_number,
        question=text,
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

@app.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge, status_code=200)

    raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/admin/cache_status")
async def admin_cache_status(request: Request):
    """Return basic cache status for testing/debugging.

    Requires header `X-TEST-ADMIN: 1` to avoid accidental exposure.
    """
    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

    with cache_lock:
        keys = list(conversation_contexts.keys())
        details = {k: {"version": v["version"], "ts": v["ts"]} for k, v in conversation_contexts.items()}

    return {"kb_version": kb_version, "keys": keys, "details": details}


@app.get("/admin/kb_status")
async def admin_kb_status(request: Request):
    _, db_path = get_project_paths(PROJECT_NAME)

    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

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

@app.get("/admin/kb_debug_collection")
async def admin_kb_debug_collection(request: Request):
    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

    import chromadb

    _, persist_dir = get_project_paths(PROJECT_NAME)


    client = chromadb.PersistentClient(path=persist_dir, settings=Settings(allow_reset=False))

    # list all collections so we stop guessing names/case
    cols = client.list_collections()
    names = [c.name for c in cols]

    out = {"persist_dir": persist_dir, "collections": names, "counts": {}}

    for name in names:
        col = client.get_collection(name=name)
        out["counts"][name] = col.count()

    return out

@app.get("/admin/config")
async def admin_config(request: Request):
    """Return a small slice of runtime configuration for debugging.

    Requires header `X-TEST-ADMIN: 1`.
    """
    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

    # Don't expose secrets; return only non-sensitive runtime hints
    return {
        "admin_numbers": sorted(list(settings.ADMIN_NUMBERS)),
        "project_name": PROJECT_NAME,
        "phone_number_id": settings.PHONE_NUMBER_ID,
        "collection_name": COLLECTION_NAME,
        "embed_model": EMBED_MODEL,
    }


@app.post("/admin/clear_history")
async def admin_clear_history(request: Request):
    """Clear a specific user's history. Requires header `X-TEST-ADMIN: 1` and JSON body {"phone": "..."}.
    """
    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    phone = body.get("phone")
    if not phone:
        raise HTTPException(status_code=400, detail="Missing phone")

    clear_conversation(phone)
    return {"ok": True, "cleared": phone}

def process_webhook_payload(body: dict):
    try:
        entry = body["entry"][0]["changes"][0]["value"]
        meta_phone_number_id = entry["metadata"]["phone_number_id"]
        messages = entry.get("messages")
        if not messages:
            return  # receipts etc.

        msg = messages[0]
        msg_id = msg.get("id")  # WhatsApp unique id

        # Idempotency: DB first, then memory
        if msg_id:
            if not claim_inbound_message_id(msg_id):
                print("[DEDUP][DB] Duplicate inbound msg_id ignored:", msg_id)
                return
            if _seen_recent(msg_id):
                print("[DEDUP][MEM] Duplicate inbound msg_id ignored:", msg_id)
                return

        msg_type = msg.get("type")
        from_number = msg["from"]
        user_text = ""

        if msg_type == "text":
            user_text = msg["text"]["body"]
            try:
                log_message(phone_number=from_number, direction="in", text=user_text)
            except Exception as e:
                print("[WARN] DB inbound log failed:", e)

        elif msg_type == "image":
            send_whatsapp_message(
                meta_phone_number_id,
                from_number,
                "I’ve received your image, but I can only understand text messages. "
                "Please type your question as a message."
            )
            return

        else:
            send_whatsapp_message(
                meta_phone_number_id,
                from_number,
                "I can only understand text messages at the moment. "
                "Please type your question as a message."
            )
            return

        # --------------------------------------------------------
        # ADMIN COMMANDS
        # --------------------------------------------------------
        if from_number in settings.ADMIN_NUMBERS:

            if user_text.startswith("/add "):
                content = user_text[5:].strip()
                doc_id = add_text_to_vectordb(content, source="admin")

                log_admin_action(
                    from_number,
                    "ADD_ENTRY",
                    {
                        "doc_id": doc_id,
                        "source_tag": "admin",
                        "content": content,
                        "content_preview": content[:200],
                    },
                )

                try:
                    log_message(phone_number=from_number, direction="out", text=f"Added entry with ID: {doc_id}")
                except Exception as e:
                    print("[WARN] DB outbound log failed:", e)

                send_whatsapp_message(meta_phone_number_id, from_number, f"Added entry with ID: {doc_id}")
                return

            if user_text.startswith("/del "):
                doc_id = user_text[5:].strip()

                collection = get_collection_for_default_project()
                existing = collection.get().get("ids", [])

                if doc_id not in existing:
                    send_whatsapp_message(meta_phone_number_id, from_number, f"No exact ID '{doc_id}' found. Nothing deleted.")
                    return

                deleted_entry = delete_by_id(doc_id)

                if deleted_entry is None:
                    send_whatsapp_message(meta_phone_number_id, from_number, f"Failed to delete '{doc_id}'.")
                    return

                log_admin_action(
                    from_number,
                    "DELETE_ENTRY",
                    {
                        "deleted_doc_id": deleted_entry["doc_id"],
                        "deleted_content": deleted_entry["content"],
                        "deleted_metadata": deleted_entry.get("metadata", {}),
                    },
                )

                try:
                    log_message(
                        phone_number=from_number,
                        direction="out",
                        text=f"Deleted entry with ID '{doc_id}'.",
                    )
                except Exception as e:
                    print("[WARN] DB outbound log failed:", e)

                send_whatsapp_message(meta_phone_number_id, from_number, f"Deleted entry with ID '{doc_id}'.")
                return

            if user_text.startswith("/list"):
                collection = get_collection_for_default_project()
                results = collection.get()

                docs = results.get("documents", [])
                metas = results.get("metadatas", [])
                ids = results.get("ids", [])

                if not docs:
                    send_whatsapp_message(meta_phone_number_id, from_number, "Database is empty.")
                    return

                message_lines = []
                for doc_id, doc_text, meta in zip(ids, docs, metas):
                    preview = doc_text[:200].replace("\n", " ")
                    message_lines.append(f"{doc_id}: {preview}...")

                listing = "\n".join(message_lines)

                try:
                    log_message(
                        phone_number=from_number,
                        direction="out",
                        text="Admin requested list of KB entries",
                    )
                except Exception as e:
                    print("[WARN] DB outbound log failed:", e)

                send_whatsapp_message(meta_phone_number_id, from_number, listing)
                return

        # --------------------------------------------------------
        # TOOL-CALL ROUTING
        # --------------------------------------------------------
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_open_status_sg",
                    "description": "Returns whether the business is open right now using Asia/Singapore time. Hours: Mon-Sat 9am-6pm. Closed Sundays and Public Holidays.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            }
        ]

        tool_router_system = (
            "You are routing user requests for a WhatsApp business assistant. "
            "If the user is asking whether the business is open now/currently/still open, "
            "call get_open_status_sg. "
            "Otherwise, do not call any tool."
        )

        router_resp = settings.client.chat.completions.create(
            model=settings.CHAT_MODEL,
            messages=[
                {"role": "system", "content": tool_router_system},
                {"role": "user", "content": user_text},
            ],
            tools=tools,
            tool_choice="auto",
        )

        msg0 = router_resp.choices[0].message

        if getattr(msg0, "tool_calls", None):
            tool_messages = []
            for tc in msg0.tool_calls:
                if tc.function.name == "get_open_status_sg":
                    result = get_open_status_sg()
                else:
                    result = {"error": "Unknown tool"}

                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

            final_system = (
                "You are a WhatsApp business assistant. "
                "Use ONLY the tool result JSON to answer whether we are open right now. "
                "Do not ask the user to check the day/time themselves. "
                "If open, include closing time. If closed, include next opening time. "
                "Be concise. Timezone is SGT."
            )

            final_resp = settings.client.chat.completions.create(
                model=settings.CHAT_MODEL,
                messages=[
                    {"role": "system", "content": final_system},
                    {"role": "user", "content": user_text},
                    msg0,
                    *tool_messages,
                ],
            )

            reply_text = final_resp.choices[0].message.content.strip()

            try:
                log_message(phone_number=from_number, direction="out", text=reply_text)
            except Exception as e:
                print("[WARN] DB outbound log failed:", e)

            send_whatsapp_message(meta_phone_number_id, from_number, reply_text)
            return

        # --------------------------------------------------------
        # 1) Retrieve context (cached, unless disabled)
        # --------------------------------------------------------
        t_total0 = time.perf_counter()
        t_retrieval0 = time.perf_counter()

        context, cache_hit = get_cached_context(
            from_number=from_number,
            question=user_text,
            k=5,
            force_refresh=DISABLE_KB_CACHE,
            return_meta=True,
        )

        t_retrieval_ms = (time.perf_counter() - t_retrieval0) * 1000.0

        if context:
            system_prompt = settings.PROMPTS["with_context"]["system"].format(project_name=PROJECT_NAME)
            user_prompt = settings.PROMPTS["with_context"]["user"].format(context=context, question=user_text)
        else:
            system_prompt = settings.PROMPTS["no_context"]["system"]
            user_prompt = settings.PROMPTS["no_context"]["user"].format(question=user_text)

        # --------------------------------------------------------
        # 2) Build messages with conversation history (respect TTL)
        # --------------------------------------------------------
        if _is_history_stale(from_number):
            clear_conversation(from_number)

        history = conversation_history.get(from_number, [])

        messages_for_model = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_prompt},
        ]

        chat = settings.client.chat.completions.create(
            model=settings.CHAT_MODEL,
            messages=messages_for_model,
        )

        reply_text = chat.choices[0].message.content.strip()
        t_total_ms = (time.perf_counter() - t_total0) * 1000.0

        perf_entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "from_number": from_number,
            "cache_disabled": DISABLE_KB_CACHE,
            "cache_hit": cache_hit,
            "context_len": len(context or ""),
            "t_retrieval_ms": round(t_retrieval_ms, 2),
            "t_total_ms": round(t_total_ms, 2),
        }
        try:
            with open(PERF_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(perf_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print("[WARN] Failed to write perf log:", e)

        # --------------------------------------------------------
        # 3) Update history
        # --------------------------------------------------------
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": reply_text})

        if len(history) > settings.MAX_HISTORY_MESSAGES:
            history = history[-settings.MAX_HISTORY_MESSAGES:]

        conversation_history[from_number] = history
        touch_conversation(from_number)

        try:
            log_message(
                phone_number=from_number,
                direction="out",
                text=reply_text,
                cache_hit=cache_hit,
                context_len=len(context or ""),
                t_retrieval_ms=round(t_retrieval_ms, 2),
                t_total_ms=round(t_total_ms, 2),
            )
        except Exception as e:
            print("[WARN] DB outbound log failed:", e)

        send_whatsapp_message(meta_phone_number_id, from_number, reply_text)

    except Exception as e:
        print("Error handling webhook:", e)

@app.post("/webhook/whatsapp")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives all incoming WhatsApp messages. ACK fast to stop Meta retries."""
    body = await request.json()
    print("Incoming payload:", body)

    background_tasks.add_task(process_webhook_payload, body)
    return {"status": "ok"}

# uvicorn app.main:app --app-dir . --host 0.0.0.0 --port 8000
# in second terminal: ngrok http 8000
# frontend -> http://127.0.0.1:8000/frontend/index.html
if __name__ == "__main__":
    print("TOKEN PREFIX:", (settings.ACCESS_TOKEN or "")[:12])
    print("PHONE_NUMBER_ID USED:", settings.PHONE_NUMBER_ID)
