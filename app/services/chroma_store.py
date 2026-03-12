import json
import os
from typing import Any

import psycopg2
from pgvector.psycopg2 import register_vector

import app.config.settings as settings
from app.config.helpers import EMBED_MODEL

_collections = {}

_VECTOR_DIMS = int(os.getenv("VECTOR_DIMS", "3072"))

# -------------------------------------------------------------------
# KB registry (tell the LLM what collections exist + what to use them for)
# IMPORTANT: names must match your KB types.
# -------------------------------------------------------------------
KB_REGISTRY = {
    "kb_general": {
        "purpose": "General workshop information, FAQs, operating info, common service explanations.",
        "best_for": ["opening hours", "location basics", "general questions", "service explanations"],
    },
    "kb_menu": {
        "purpose": "Service menu, packages, promos, price lists (if present in KB).",
        "best_for": ["how much", "price", "pricing", "quote", "package", "promo", "rates"],
    },
    "kb_contact": {
        "purpose": "Official contact details and how to reach the team.",
        "best_for": ["contact", "phone", "whatsapp", "email", "address", "how to reach"],
    },
}


def _to_vector_literal(values: list[float]) -> str:
    # Pass query vectors as pgvector literals so Postgres does not infer numeric[].
    return "[" + ",".join(str(float(v)) for v in values) + "]"


def _vector_db_conn():
    database_url = os.getenv("VECTOR_DB_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("VECTOR_DB_URL (or fallback DATABASE_URL) is not set.")

    sslmode = os.getenv("VECTOR_DB_SSLMODE")
    if sslmode is None:
        sslmode = "disable" if ("localhost" in database_url or "127.0.0.1" in database_url) else "require"

    conn = psycopg2.connect(
        database_url,
        connect_timeout=5,
        sslmode=sslmode,
    )
    conn.autocommit = True
    return conn


def _ensure_store_ready() -> None:
    conn = _vector_db_conn()
    try:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id TEXT PRIMARY KEY,
                    kb_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    embedding vector({_VECTOR_DIMS}) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_type ON kb_chunks (kb_type);")
    finally:
        conn.close()


class PgVectorCollection:
    def __init__(self, name: str):
        self.name = name
        _ensure_store_ready()

    def add(self, ids: list[str], embeddings: list[list[float]], documents: list[str], metadatas: list[dict[str, Any]] | None = None) -> None:
        if not (len(ids) == len(embeddings) == len(documents)):
            raise ValueError("ids, embeddings and documents must have the same length")

        metas = metadatas or [{} for _ in ids]
        if len(metas) != len(ids):
            raise ValueError("metadatas must match ids length")

        conn = _vector_db_conn()
        try:
            register_vector(conn)
            with conn.cursor() as cur:
                for doc_id, emb, doc, meta in zip(ids, embeddings, documents, metas):
                    safe_meta = meta or {}
                    cur.execute(
                        """
                        INSERT INTO kb_chunks (id, kb_type, content, metadata, embedding)
                        VALUES (%s, %s, %s, %s::jsonb, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            kb_type = EXCLUDED.kb_type,
                            content = EXCLUDED.content,
                            metadata = EXCLUDED.metadata,
                            embedding = EXCLUDED.embedding
                        """,
                        (doc_id, self.name, doc, json.dumps(safe_meta), emb),
                    )
        finally:
            conn.close()

    def count(self) -> int:
        conn = _vector_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM kb_chunks WHERE kb_type = %s", (self.name,))
                return int(cur.fetchone()[0] or 0)
        finally:
            conn.close()

    def get(self, ids: list[str] | None = None, limit: int = 1000) -> dict[str, list[Any]]:
        conn = _vector_db_conn()
        try:
            with conn.cursor() as cur:
                if ids:
                    cur.execute(
                        """
                        SELECT id, content, metadata
                        FROM kb_chunks
                        WHERE kb_type = %s AND id = ANY(%s)
                        ORDER BY created_at DESC
                        """,
                        (self.name, ids),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, content, metadata
                        FROM kb_chunks
                        WHERE kb_type = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (self.name, limit),
                    )

                rows = cur.fetchall()
                out_ids = [r[0] for r in rows]
                out_docs = [r[1] for r in rows]
                out_metas = [r[2] or {} for r in rows]
                return {"ids": out_ids, "documents": out_docs, "metadatas": out_metas}
        finally:
            conn.close()

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        conn = _vector_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kb_chunks WHERE kb_type = %s AND id = ANY(%s)", (self.name, ids))
        finally:
            conn.close()

    def clear(self) -> None:
        conn = _vector_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kb_chunks WHERE kb_type = %s", (self.name,))
        finally:
            conn.close()

    def query(self, query_embeddings: list[list[float]], n_results: int = 5, include: list[str] | None = None) -> dict[str, list[list[Any]]]:
        if not query_embeddings:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        q_vec = _to_vector_literal(query_embeddings[0])
        conn = _vector_db_conn()
        try:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, content, metadata, (embedding <=> %s::vector) AS distance
                    FROM kb_chunks
                    WHERE kb_type = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (q_vec, self.name, q_vec, n_results),
                )
                rows = cur.fetchall()

            ids = [r[0] for r in rows]
            docs = [r[1] for r in rows]
            metas = [r[2] or {} for r in rows]
            dists = [float(r[3]) for r in rows]

            return {
                "ids": [ids],
                "documents": [docs],
                "metadatas": [metas],
                "distances": [dists],
            }
        finally:
            conn.close()


def get_kb_inventory_text() -> str:
    """
    Returns a short string describing the KB collections.
    Used for LLM routing. Keep it short to reduce tokens.
    """
    lines = []
    for name, info in KB_REGISTRY.items():
        purpose = info.get("purpose", "")
        best_for = ", ".join(info.get("best_for", [])[:8])
        lines.append(f"- {name}: {purpose} Best for: {best_for}")
    return "\n".join(lines)


def get_collection(name: str) -> PgVectorCollection:
    if name not in _collections:
        _collections[name] = PgVectorCollection(name)
        print(f"[INFO] Using pgvector collection '{name}'")
    return _collections[name]


def clear_collection(name: str) -> None:
    get_collection(name).clear()


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
    distances: lower is more similar.
    """
    return retrieve_hits(question, "kb_general", k)


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


def best_distance(dists: list[float]) -> float | None:
    if not dists:
        return None
    try:
        return float(min(dists))
    except Exception:
        return None


def retrieve_context(question: str, kb_type: str, k: int = 5) -> str:
    docs, metas, _ = retrieve_hits(question, kb_type, k)

    if not docs:
        return ""

    parts = []
    for doc, meta in zip(docs, metas):
        meta = meta or {}
        src = meta.get("source_file", "unknown")
        parts.append(f"Source: {src}\n{doc}")

    return "\n\n---\n\n".join(parts)
