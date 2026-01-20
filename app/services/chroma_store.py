import chromadb
from chromadb.config import Settings
import app.config.settings as settings
from app.config.helpers import get_project_paths, EMBED_MODEL, COLLECTION_NAME, PROJECT_NAME

_collection = None


def get_collection_for_default_project():
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
