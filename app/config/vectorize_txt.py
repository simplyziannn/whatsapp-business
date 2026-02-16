import os
from openai import OpenAI
from dotenv import load_dotenv

from app.config.helpers import (
    chunk_text,
    get_project_paths,
    EMBED_MODEL,
    PROJECT_NAME,
)
from app.services.chroma_store import get_collection, clear_collection

load_dotenv()
client = OpenAI()


def convert_txt_folder_to_vector_db(txt_folder: str, db_path: str, collection_name: str):
    """
    Converts ALL .txt files in a folder into vector embeddings
    and stores them in the Postgres pgvector KB table via collection adapter.

    db_path is kept for backwards compatibility in call sites/logging.
    """
    _ = db_path
    collection = get_collection(collection_name)

    file_list = [f for f in os.listdir(txt_folder) if f.endswith(".txt")]
    if not file_list:
        print(f"[WARN] No .txt files found in {txt_folder}")
        return db_path

    for filename in file_list:
        filepath = os.path.join(txt_folder, filename)
        print(f"Processing {filepath}...")

        with open(filepath, "r", encoding="utf-8") as f:
            raw_text = f.read()

        chunks = chunk_text(raw_text)
        if not chunks:
            continue

        resp = client.embeddings.create(
            model=EMBED_MODEL,
            input=chunks,
        )
        embeddings = resp.data

        ids = []
        vecs = []
        docs = []
        metas = []

        for i, emb in enumerate(embeddings):
            chunk_id = f"{collection_name}:{filename}_chunk_{i}"
            ids.append(chunk_id)
            vecs.append(emb.embedding)
            docs.append(chunks[i])
            metas.append({"source_file": filename})

        collection.add(
            ids=ids,
            embeddings=vecs,
            documents=docs,
            metadatas=metas,
        )

    print("\nDONE - KB vectors stored in Postgres for:", collection_name)
    return db_path


def convert_project_to_vector_db(project_name: str | None = None):
    """
    High-level helper: for a given project name, look up its txt & vectordb
    folders and run the conversion.

    If project_name is None, uses default PROJECT_NAME.
    """
    if project_name is None:
        project_name = PROJECT_NAME

    txt_folder, db_path = get_project_paths(project_name)
    print(f"[INFO] Vectorising project '{project_name}'")
    print(f"       txt_folder: {txt_folder}")
    print(f"       db_path   : {db_path} (unused by pgvector mode)")

    vectorize_kb_structure(txt_folder, db_path)
    return db_path


def vectorize_kb_structure(base_txt_dir: str, db_path: str):
    """
    Expected structure:
    txt/
      menu/
      contact/
      general/
    """
    mapping = {
        "menu": "kb_menu",
        "contact": "kb_contact",
        "general": "kb_general",
    }

    for folder, collection in mapping.items():
        path = os.path.join(base_txt_dir, folder)
        if not os.path.isdir(path):
            continue

        print(f"[KB] Resetting {collection}")
        clear_collection(collection)

        print(f"[KB] Vectorising {folder} -> {collection}")
        convert_txt_folder_to_vector_db(path, db_path, collection)


if __name__ == "__main__":
    import sys

    proj = sys.argv[1] if len(sys.argv) > 1 else None
    convert_project_to_vector_db(proj)
