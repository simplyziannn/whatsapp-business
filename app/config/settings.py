#loading of environment variables
from dotenv import load_dotenv
import os,json
from openai import OpenAI


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

CACHE_MAX_AGE = int(os.getenv("KB_CACHE_MAX_AGE", str(60 * 60)))  # seconds
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "12"))  # total messages (user+assistant), keep it small
HISTORY_MAX_AGE = int(os.getenv("HISTORY_MAX_AGE", str(24 * 3600)))  # seconds; default 24 hours

# -------------------------
# RATE LIMITING
# -------------------------
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "1") == "1"
RATE_LIMIT_MAX_PER_DAY = int(os.getenv("RATE_LIMIT_MAX_PER_DAY", "20"))
RATE_LIMIT_TZ = os.getenv("RATE_LIMIT_TZ", "Asia/Singapore")

RATE_LIMIT_BLOCK_MESSAGE = os.getenv(
    "RATE_LIMIT_BLOCK_MESSAGE",
    "You’ve reached today’s message limit. Please contact the company for further assistance."
)
