import chromadb
from chromadb.config import Settings
import app.config.settings as settings
from app.config.helpers import get_project_paths, EMBED_MODEL, COLLECTION_NAME, PROJECT_NAME

_collection = None
_collections = {}

def get_collection_for_default_project():
    # Backwards-compatible shim for older imports (admin_api, admin_kb, etc.)
    return get_collection(COLLECTION_NAME)

def get_collection(name: str):
    if name in _collections:
        return _collections[name]

    _, db_path = get_project_paths(PROJECT_NAME)

    chroma_client = chromadb.PersistentClient(
        path=db_path,
        settings=Settings(allow_reset=False),
    )

    col = chroma_client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )
    _collections[name] = col
    print(f"[INFO] Using collection '{name}' at {db_path}")
    return col

def retrieve_hits(question: str, kb_type: str, k: int = 5):
    collection = get_collection(kb_type)

    emb_resp = settings.client.embeddings.create(
        model=EMBED_MODEL,
        input=[question],
    )
    q_vec = emb_resp.data[0].embedding

    results = collection.query(
        query_embeddings=[q_vec],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]
    return docs, metas, dists

def retrieve_hits_from_vectordb(question: str, k: int = 5):
    """
    Returns (docs, metas, distances) for downstream gating/inspection.
    distances: lower is more similar (depends on Chroma metric).
    """
    collection = get_collection("kb_general")

    emb_resp = settings.client.embeddings.create(
        model=EMBED_MODEL,
        input=[question],
    )
    q_vec = emb_resp.data[0].embedding

    results = collection.query(
        query_embeddings=[q_vec],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]
    return docs, metas, dists

def retrieve_context_from_vectordb(question: str, k: int = 5) -> str:
    """
    Backwards-compatible helper for routes expecting a single formatted context string.
    Pulls from the default 'kb_general' collection via retrieve_hits_from_vectordb().
    """
    docs, metas, _ = retrieve_hits_from_vectordb(question, k)

    if not docs:
        return ""

    parts = []
    for doc, meta in zip(docs, metas):
        meta = meta or {}
        src = meta.get("source_file", "unknown")
        parts.append(f"Source: {src}\n{doc}")

    return "\n\n---\n\n".join(parts)

def retrieve_context(question: str, kb_type: str, k: int = 5) -> str:
    docs, metas, _ = retrieve_hits(question, kb_type, k)

    if not docs:
        return ""

    parts = []
    for doc, meta in zip(docs, metas):
        src = meta.get("source_file", "unknown")
        parts.append(f"Source: {src}\n{doc}")

    return "\n\n---\n\n".join(parts)
