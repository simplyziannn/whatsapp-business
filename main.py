import os
import requests
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from openai import OpenAI

import chromadb
from chromadb.config import Settings

from helpers import get_project_paths, EMBED_MODEL, COLLECTION_NAME, PROJECT_NAME

load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "whatsapp_verify_123")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-5.1")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI()

def send_whatsapp_message(to: str, text: str):
    """Send a WhatsApp text message via Cloud API."""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
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

# -------------------------------------------------------------------
# FastAPI endpoints
# -------------------------------------------------------------------

@app.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    """Meta webhook verification."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")

    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/webhook/whatsapp")
async def webhook(request: Request):
    """Receives all incoming WhatsApp messages."""
    body = await request.json()
    print("Incoming payload:", body)

    try:
        entry = body["entry"][0]["changes"][0]["value"]
        messages = entry.get("messages")
        if not messages:
            # delivery/read receipts etc; nothing to reply
            return {"status": "ignored"}

        msg = messages[0]
        if msg.get("type") != "text":
            return {"status": "ignored_non_text"}

        from_number = msg["from"]
        user_text = msg["text"]["body"]

        # --------------------------------------------------------
        # 1) Try to retrieve context from the project's vector DB
        # --------------------------------------------------------
        context = retrieve_context_from_vectordb(user_text, k=5)

        if context:
            system_prompt = (
                f"You are a friendly company representative for the company '{PROJECT_NAME}'. "
                "Answer questions strictly based on the provided context. "
                "If the answer is not found in the context, redirect the customer to contact a real human."
                "The query will be from a customer stand-point. You are the comapny's first contact, please reply professionally and not show that you are an AI."
            )
            user_prompt = (
                "Here is some context from the knowledge base:\n\n"
                f"{context}\n\n"
                "Customer question:\n"
                f"{user_text}"
                "You are to reply based on the above context only. Answer briefly in 1–3 sentences."
                "You are not to mention or show that you are an AI model."
                "You are to present the context given to you are your prior knowledge."
            )
        else:
            # Fallback: no context (DB empty / error). Normal chat mode.
            system_prompt = (
                "You are a friendly WhatsApp business assistant. "
                "Answer briefly in 1–3 sentences."
            )
            user_prompt = user_text

        # --------------------------------------------------------
        # 2) Call OpenAI with (system + user) including context
        # --------------------------------------------------------
        chat = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        reply_text = chat.choices[0].message.content.strip()

        # 3) Send reply back to WhatsApp user
        send_whatsapp_message(from_number, reply_text)

    except Exception as e:
        print("Error handling webhook:", e)

    return {"status": "ok"}

# uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# in second terminal: ngrok http 8000
