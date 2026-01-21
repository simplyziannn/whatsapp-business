import requests
import app.config.settings as settings


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

def send_whatsapp_buttons(phone_number_id: str, to: str, body_text: str, buttons: list[dict]) -> bool:
    """
    buttons: [{"id": "BOOK_CONFIRM:123", "title": "Confirm"}, {"id": "BOOK_CANCEL:123", "title": "Cancel"}]
    """
    url = f"https://graph.facebook.com/v24.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in buttons]
            },
        },
    }
    resp = requests.post(url, headers=headers, json=data, timeout=10)
    print("WhatsApp send status:", resp.status_code, resp.text)
    return 200 <= resp.status_code < 300

