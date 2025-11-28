import os
import requests
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "whatsapp_verify_123")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")

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



@app.get("/webhook/whatsapp")
async def verify(request: Request):
    """Verification endpoint used once when you configure the webhook in Meta."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        # Meta expects the raw challenge string as plain text
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

        # --- Call OpenAI ---
        chat = client.chat.completions.create(
            model="gpt-5.1",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a friendly WhatsApp business assistant. "
                        "Answer briefly in 1–3 sentences."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
        )

        reply_text = chat.choices[0].message.content.strip()

        # --- Send reply back to WhatsApp user ---
        send_whatsapp_message(from_number, reply_text)

    except Exception as e:
        print("Error handling webhook:", e)

    return {"status": "ok"}

#uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# in second terminal : ngrok http 8000
