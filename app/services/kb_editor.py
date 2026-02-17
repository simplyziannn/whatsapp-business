from __future__ import annotations

import os
from pathlib import Path

import app.config.settings as settings
from app.config.helpers import EMBED_MODEL, PROJECT_NAME, chunk_text, get_project_paths
from app.services import kb_cache
from app.services.chroma_store import get_collection

FOLDER_TO_COLLECTION = {
    "menu": "kb_menu",
    "contact": "kb_contact",
    "general": "kb_general",
}


def _parse_chunk_index(doc_id: str) -> int:
    marker = "_chunk_"
    if marker not in doc_id:
        return 10**9
    raw = doc_id.rsplit(marker, 1)[-1]
    try:
        return int(raw)
    except ValueError:
        return 10**9


def _meta_chunk_index(meta: dict, doc_id: str) -> int:
    raw = (meta or {}).get("chunk_index")
    if raw is None:
        return _parse_chunk_index(doc_id)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _parse_chunk_index(doc_id)


def _sort_doc_rows(ids: list[str], documents: list[str], metadatas: list[dict]) -> list[dict]:
    rows = []
    for i, doc_id in enumerate(ids):
        meta = metadatas[i] if i < len(metadatas) else {}
        rows.append(
            {
                "id": doc_id,
                "content": documents[i] if i < len(documents) else "",
                "metadata": meta or {},
                "source_file": str((meta or {}).get("source_file", "unknown")),
                "chunk_index": _meta_chunk_index(meta or {}, doc_id),
            }
        )

    rows.sort(key=lambda r: (r["source_file"], r["chunk_index"], r["id"]))
    return rows


def list_kb_folders() -> list[dict]:
    out = []
    for folder, collection_name in FOLDER_TO_COLLECTION.items():
        collection = get_collection(collection_name)
        count = collection.count()
        docs = collection.get(limit=5000)
        source_files = sorted(
            {
                str((m or {}).get("source_file", "unknown"))
                for m in (docs.get("metadatas") or [])
            }
        )
        out.append(
            {
                "folder": folder,
                "collection": collection_name,
                "chunk_count": count,
                "source_files": source_files,
                "has_content": count > 0,
            }
        )
    return out


def get_folder_content(folder: str) -> dict:
    if folder not in FOLDER_TO_COLLECTION:
        raise ValueError(f"Unknown folder '{folder}'")

    collection_name = FOLDER_TO_COLLECTION[folder]
    collection = get_collection(collection_name)
    docs = collection.get(limit=20000)
    rows = _sort_doc_rows(docs.get("ids", []), docs.get("documents", []), docs.get("metadatas", []))
    content = "\n".join(r["content"] for r in rows).strip()

    return {
        "folder": folder,
        "collection": collection_name,
        "chunk_count": len(rows),
        "source_files": sorted({r["source_file"] for r in rows}),
        "content": content,
    }


def _target_txt_path(folder: str) -> Path:
    txt_root, _ = get_project_paths(PROJECT_NAME)
    folder_dir = Path(txt_root) / folder
    folder_dir.mkdir(parents=True, exist_ok=True)
    txt_files = sorted([p for p in folder_dir.glob("*.txt") if p.is_file()])
    if txt_files:
        return txt_files[0]
    return folder_dir / f"{folder}.txt"


def save_folder_content(folder: str, content: str, source: str = "dashboard") -> dict:
    if folder not in FOLDER_TO_COLLECTION:
        raise ValueError(f"Unknown folder '{folder}'")

    collection_name = FOLDER_TO_COLLECTION[folder]
    collection = get_collection(collection_name)
    text = content or ""

    file_path = _target_txt_path(folder)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(text)

    collection.clear()

    if text.strip():
        chunks = chunk_text(text)
        emb_resp = settings.client.embeddings.create(
            model=EMBED_MODEL,
            input=chunks,
        )
        ids = []
        vecs = []
        docs = []
        metas = []
        for idx, emb in enumerate(emb_resp.data):
            ids.append(f"{collection_name}:{folder}_dashboard_chunk_{idx}")
            vecs.append(emb.embedding)
            docs.append(chunks[idx])
            metas.append(
                {
                    "source_file": os.path.basename(file_path),
                    "source": source,
                    "folder": folder,
                    "chunk_index": idx,
                }
            )
        collection.add(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)

    try:
        kb_cache.bump_kb_version()
    except Exception:
        pass

    return {
        "folder": folder,
        "collection": collection_name,
        "saved_text_path": str(file_path),
        "chunk_count": collection.count(),
    }
