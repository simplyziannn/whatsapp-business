# helpers.py
import os
from pathlib import Path

# -----------------------
# Central configuration
# -----------------------

# Default embedding model (can override via .env)
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")

# Default collection name in Chroma
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "txt_collection")

# Default project name used by the bot and vectorizer
PROJECT_NAME = os.getenv("PROJECT_NAME", "AutoSpritze")

# Base folder where all projects live
PROJECTS_BASE = Path(os.getenv("PROJECTS_BASE", "Knowledge_Base"))


# -----------------------
# Path helpers
# -----------------------

def ensure_base_dir() -> Path:
    """
    Make sure the base 'projects' folder exists and return it.
    """
    PROJECTS_BASE.mkdir(exist_ok=True)
    return PROJECTS_BASE


def get_project_paths(project_name: str | None = None) -> tuple[str, str]:
    """
    Return (txt_folder, db_path) for the given project.

    If project_name is None, uses the default PROJECT_NAME.
    It also ensures the txt/ and vectordb/ folders exist.
    """
    if project_name is None:
        project_name = PROJECT_NAME

    base = ensure_base_dir()
    project_dir = base / project_name
    txt_dir = project_dir / "txt"
    db_dir = project_dir / "vectordb"

    txt_dir.mkdir(parents=True, exist_ok=True)
    db_dir.mkdir(parents=True, exist_ok=True)

    return str(txt_dir), str(db_dir)


# -----------------------
# Text chunking
# -----------------------

def chunk_text(text: str, max_chars: int = 800, overlap: int = 150) -> list[str]:
    """
    Simple overlapping character-based chunker.

    Example with max_chars=800, overlap=150:
      chunk 0: text[0:800]
      chunk 1: text[650:1450]
      chunk 2: text[1300:2100]
      ...
    """
    chunks: list[str] = []
    start = 0

    while start < len(text):
        end = start + max_chars
        chunk = text[start:end]
        chunks.append(chunk)
        start = end - overlap  # step with overlap

    return chunks

# -----------------------
# datetime helpers
# -----------------------
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SG_TZ = ZoneInfo("Asia/Singapore")

# Maintain yearly, format YYYY-MM-DD. Can start empty.
PUBLIC_HOLIDAYS_SG = {
    # "2026-01-01",
}

def _is_public_holiday_sg(dt: datetime) -> bool:
    return dt.date().isoformat() in PUBLIC_HOLIDAYS_SG

def _next_opening_datetime_sg(now: datetime) -> datetime:
    candidate = now.replace(hour=9, minute=0, second=0, microsecond=0)

    # if it's already past (or equal) today's 9am, move to next day
    if now >= candidate:
        candidate = candidate + timedelta(days=1)

    # find next non-Sunday and non-PH
    while candidate.weekday() == 6 or _is_public_holiday_sg(candidate):  # Sunday=6
        candidate = candidate + timedelta(days=1)

    return candidate

def get_open_status_sg() -> dict:
    now = datetime.now(SG_TZ)

    # Sunday closed
    if now.weekday() == 6:
        nxt = _next_opening_datetime_sg(now)
        return {
            "timezone": "Asia/Singapore",
            "now_iso": now.isoformat(),
            "open": False,
            "reason": "Closed on Sundays",
            "opens_at_iso": nxt.isoformat(),
        }

    # Public holiday closed
    if _is_public_holiday_sg(now):
        nxt = _next_opening_datetime_sg(now)
        return {
            "timezone": "Asia/Singapore",
            "now_iso": now.isoformat(),
            "open": False,
            "reason": "Closed on Public Holidays",
            "opens_at_iso": nxt.isoformat(),
        }

    open_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_dt = now.replace(hour=18, minute=0, second=0, microsecond=0)

    if open_dt <= now < close_dt:
        return {
            "timezone": "Asia/Singapore",
            "now_iso": now.isoformat(),
            "open": True,
            "closes_at_iso": close_dt.isoformat(),
        }

    nxt = _next_opening_datetime_sg(now)
    return {
        "timezone": "Asia/Singapore",
        "now_iso": now.isoformat(),
        "open": False,
        "reason": "Before opening hours" if now < open_dt else "After closing hours",
        "opens_at_iso": nxt.isoformat(),
    }


if __name__ == "__main__":
    # simple path testing debugging 
    returned_tuple = get_project_paths(PROJECT_NAME)
    print ("Project paths:", returned_tuple)