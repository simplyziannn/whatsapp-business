from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from openai import OpenAI
from chromadb.config import Settings

from datetime import datetime
import json, uuid, os, requests, chromadb, threading , time

from helpers import get_project_paths, EMBED_MODEL, COLLECTION_NAME, PROJECT_NAME

load_dotenv()

PROMPTS_PATH = os.getenv("PROMPTS_PATH", "prompts.json")
with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
    PROMPTS = json.load(f)

ADMIN_NUMBERS = {
    num.strip()
    for num in os.getenv("ADMIN_NUMBERS", "").split(",")
    if num.strip()
}
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "whatsapp_verify_123")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
ADMIN_LOG_FILE = os.getenv("ADMIN_LOG_FILE", "admin_actions.log")

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-5.1")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI()

# -----------------------------
# KB cache
# -----------------------------
kb_version = 0
# cache key format: "{from_number}|k={k}" -> {"context": str, "version": int, "ts": float}
conversation_contexts: dict = {}
cache_lock = threading.Lock()
CACHE_MAX_AGE = int(os.getenv("KB_CACHE_MAX_AGE", str(60 * 60)))  # seconds


def bump_kb_version():
    """Bump KB version and clear in-memory cache so contexts are refreshed."""
    global kb_version
    with cache_lock:
        kb_version += 1
        conversation_contexts.clear()


def _context_cache_key(from_number: str, k: int) -> str:
    return f"{from_number}|k={k}"


def get_cached_context(from_number: str, question: str, k: int = 5, force_refresh: bool = False) -> str:
    """Return cached context for this user+k if still valid; otherwise fetch and cache.

    - Caches separately per value of `k` (number of results requested).
    - Use `force_refresh=True` to bypass the cache and re-query the vectordb.
    """
    key = _context_cache_key(from_number, k)
    now = time.time()

    with cache_lock:
        entry = conversation_contexts.get(key)
        if (
            not force_refresh
            and entry
            and entry.get("version") == kb_version
            and (now - entry.get("ts", 0)) < CACHE_MAX_AGE
        ):
            return entry.get("context", "")

    # Cache miss / stale: fetch and store
    context = retrieve_context_from_vectordb(question, k=k)
    with cache_lock:
        conversation_contexts[key] = {"context": context, "version": kb_version, "ts": now}

    return context


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
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "12"))  # total messages (user+assistant), keep it small
HISTORY_MAX_AGE = int(os.getenv("HISTORY_MAX_AGE", str(24 * 3600)))  # seconds; default 24 hours


def _is_history_stale(from_number: str) -> bool:
    last = conversation_last_activity.get(from_number)
    if last is None:
        return False
    return (time.time() - last) > HISTORY_MAX_AGE


def touch_conversation(from_number: str):
    conversation_last_activity[from_number] = time.time()


def clear_conversation(from_number: str):
    conversation_history.pop(from_number, None)
    conversation_last_activity.pop(from_number, None)



def send_whatsapp_message(phone_number_id: str, to: str, text: str):
    url = f"https://graph.facebook.com/v24.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
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

        emb_resp = client.embeddings.create(
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

    emb = client.embeddings.create(
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

@app.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
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


@app.get("/admin/config")
async def admin_config(request: Request):
    """Return a small slice of runtime configuration for debugging.

    Requires header `X-TEST-ADMIN: 1`.
    """
    if request.headers.get("X-TEST-ADMIN") != "1":
        raise HTTPException(status_code=403, detail="Forbidden")

    # Don't expose secrets; return only non-sensitive runtime hints
    return {
        "admin_numbers": sorted(list(ADMIN_NUMBERS)),
        "project_name": PROJECT_NAME,
        "phone_number_id": PHONE_NUMBER_ID,
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


@app.post("/webhook/whatsapp")
async def webhook(request: Request):
    """Receives all incoming WhatsApp messages."""
    body = await request.json()
    print("Incoming payload:", body)

    try:
        entry = body["entry"][0]["changes"][0]["value"]
        meta_phone_number_id = entry["metadata"]["phone_number_id"]
        messages = entry.get("messages")
        if not messages:
            # delivery/read receipts etc; nothing to reply
            return {"status": "ignored"}

        msg = messages[0]
        msg_type = msg.get("type")
        from_number = msg["from"]

        if msg_type == "text":
            # Normal case: text message (emoji included)
            user_text = msg["text"]["body"]

        elif msg_type == "image":
            # We currently do NOT process images, even if they have captions.
            send_whatsapp_message(
                meta_phone_number_id,
                from_number,
                "I’ve received your image, but I can only understand text messages. "
                "Please type your question as a message."
            )

            return {"status": "image_not_supported"}

        else:
            # Other message types (audio, video, stickers, etc.) are not supported for now
            send_whatsapp_message(
                meta_phone_number_id,
                from_number,
                "I can only understand text messages at the moment. "
                "Please type your question as a message."
            )
            return {"status": "unsupported_type"}


        # --------------------------------------------------------
        # ADMIN COMMANDS
        # --------------------------------------------------------
        if from_number in ADMIN_NUMBERS:

            # Add new knowledge
            if user_text.startswith("/add "):
                content = user_text[5:].strip()
                doc_id = add_text_to_vectordb(content, source="admin")

                # log it
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

                send_whatsapp_message(from_number, f"Added entry with ID: {doc_id}")
                return {"status": "admin_add_done"}

            # Delete by source
            if user_text.startswith("/del "):
                doc_id = user_text[5:].strip()

                collection = get_collection_for_default_project()
                existing = collection.get().get("ids", [])

                # Reject invalid IDs
                if doc_id not in existing:
                    send_whatsapp_message(from_number, f"No exact ID '{doc_id}' found. Nothing deleted.")
                    return {"status": "admin_delete_invalid"}

                # If valid, delete and get deleted content
                deleted_entry = delete_by_id(doc_id)

                if deleted_entry is None:
                    send_whatsapp_message(from_number, f"Failed to delete '{doc_id}'.")
                    return {"status": "admin_delete_failed"}

                # Log full deleted content
                log_admin_action(
                    from_number,
                    "DELETE_ENTRY",
                    {
                        "deleted_doc_id": deleted_entry["doc_id"],
                        "deleted_content": deleted_entry["content"],
                        "deleted_metadata": deleted_entry.get("metadata", {}),
                    },
                )

                send_whatsapp_message(from_number, f"Deleted entry with ID '{doc_id}'.")
                return {"status": "admin_delete_done"}



            # List database contents
            if user_text.startswith("/list"):
                collection = get_collection_for_default_project()
                # 'ids' are always returned; no need for include=
                results = collection.get()

                docs = results.get("documents", [])
                metas = results.get("metadatas", [])
                ids = results.get("ids", [])

                if not docs:
                    send_whatsapp_message(from_number, "Database is empty.")
                    return {"status": "admin_list_empty"}

                message_lines = []
                for doc_id, doc_text, meta in zip(ids, docs, metas):
                    preview = doc_text[:200].replace("\n", " ")
                    message_lines.append(f"{doc_id}: {preview}...")

                listing = "\n".join(message_lines)
                send_whatsapp_message(from_number, listing)

                return {"status": "admin_list_done"}


        # --------------------------------------------------------
        # 1) Try to retrieve context from the project's vector DB
        # --------------------------------------------------------
        context = retrieve_context_from_vectordb(user_text, k=5)

        if context:
            system_prompt = PROMPTS["with_context"]["system"].format(
                project_name=PROJECT_NAME
            )
            user_prompt = PROMPTS["with_context"]["user"].format(
                context=context,
                question=user_text,
            )

        else:
            system_prompt = PROMPTS["no_context"]["system"]
            user_prompt = PROMPTS["no_context"]["user"].format(
                question=user_text
            )

        # --------------------------------------------------------
        # 2) Build messages with conversation history (respect TTL)
        # --------------------------------------------------------
        # If the user's history is stale, clear it first
        if _is_history_stale(from_number):
            clear_conversation(from_number)

        # Get existing history for this user (if any)
        history = conversation_history.get(from_number, [])

        # We store RAW user_text + assistant replies in history,
        # but for THIS turn we still send the templated `user_prompt`
        # that includes context, instructions, etc.
        messages_for_model = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_prompt},
        ]

        chat = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages_for_model,
        )

        reply_text = chat.choices[0].message.content.strip()

        # --------------------------------------------------------
        # 3) Update history for this user
        # --------------------------------------------------------
        # For history, we only keep what the *human* actually typed
        # plus what the bot replied, not the long templated prompt.
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": reply_text})

        # trim to last N messages to keep token usage under control
        if len(history) > MAX_HISTORY_MESSAGES:
            history = history[-MAX_HISTORY_MESSAGES:]

        conversation_history[from_number] = history
        # update last activity timestamp
        touch_conversation(from_number)

        # 4) Send reply back to WhatsApp user
        send_whatsapp_message(meta_phone_number_id, from_number, reply_text)


    except Exception as e:
        print("Error handling webhook:", e)

    return {"status": "ok"}

# uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# in second terminal: ngrok http 8000

if __name__ == "__main__":
    print("TOKEN PREFIX:", (ACCESS_TOKEN or "")[:12])
    print("PHONE_NUMBER_ID USED:", PHONE_NUMBER_ID)

