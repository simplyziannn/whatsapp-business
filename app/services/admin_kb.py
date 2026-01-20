import json
import uuid
from datetime import datetime

from app.services.chroma_store import get_collection_for_default_project
from app.services import kb_cache
from app.config.helpers import EMBED_MODEL
import app.config.settings as settings


def add_text_to_vectordb(text: str, source: str = "admin"):
    """Embed text and store it as a new document in the vectordb."""
    collection = get_collection_for_default_project()

    emb = settings.client.embeddings.create(
        model=EMBED_MODEL,
        input=[text],
    ).data[0].embedding

    doc_id = f"admin_{uuid.uuid4().hex}"

    collection.add(
        ids=[doc_id],
        embeddings=[emb],
        documents=[text],
        metadatas=[{"source_file": source}],
    )

    # Invalidate KB cache so future queries refresh context
    try:
        kb_cache.bump_kb_version()
    except Exception:
        pass

    return doc_id


def delete_by_id(doc_id: str):
    """
    Delete a single document by its ID and return info about what was deleted.
    Returns dict {"doc_id": ..., "content": ..., "metadata": {...}} or None.
    """
    try:
        collection = get_collection_for_default_project()

        # Fetch BEFORE deleting
        result = collection.get(ids=[doc_id])
        docs = result.get("documents", [])
        metas = result.get("metadatas", [])

        if not docs:
            return None

        deleted_entry = {
            "doc_id": doc_id,
            "content": docs[0],
            "metadata": metas[0] if metas else {},
        }

        collection.delete(ids=[doc_id])

        # Invalidate KB cache
        try:
            kb_cache.bump_kb_version()
        except Exception:
            pass

        return deleted_entry

    except Exception as e:
        print("Delete-by-ID error:", e)
        return None


def log_admin_action(admin_log_file: str, admin_number: str, action: str, details: dict):
    """
    Append a pretty JSON block describing an admin action.
    """
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "admin_number": admin_number,
        "action": action,
        "entry_details": details,
    }
    try:
        with open(admin_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, indent=2) + "\n\n")
    except Exception as e:
        print("[WARN] Failed to write admin log:", e)
