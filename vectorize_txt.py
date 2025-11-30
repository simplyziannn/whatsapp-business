# vectorize_txt.py
#
# Core logic for converting .txt files into vector embeddings (Chroma + OpenAI)
# and doing it in a project-aware way.

import os
import chromadb
from chromadb.config import Settings
from openai import OpenAI
from dotenv import load_dotenv

from helpers import (
    chunk_text,
    get_project_paths,
    EMBED_MODEL,
    COLLECTION_NAME,
    PROJECT_NAME,
)

load_dotenv()
client = OpenAI()


def convert_txt_folder_to_vector_db(txt_folder: str, db_path: str):
    """
    Converts ALL .txt files in a folder into vector embeddings
    and stores them in a Chroma vector database at db_path.
    """

    chroma_client = chromadb.PersistentClient(
        path=db_path,
        settings=Settings(allow_reset=True),
    )
    collection = chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    file_list = [f for f in os.listdir(txt_folder) if f.endswith(".txt")]
    if not file_list:
        print(f"[WARN] No .txt files found in {txt_folder}")
        return db_path

    for filename in file_list:
        filepath = os.path.join(txt_folder, filename)
        print(f"Processing {filepath}...")

        with open(filepath, "r", encoding="utf-8") as f:
            raw_text = f.read()

        # Chunking
        chunks = chunk_text(raw_text)
        if not chunks:
            continue

        # Embeddings (batch)
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
            chunk_id = f"{filename}_chunk_{i}"
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

    print("\n✅ DONE — Vector DB created at:", db_path)
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
    print(f"       db_path   : {db_path}")

    return convert_txt_folder_to_vector_db(txt_folder, db_path)


if __name__ == "__main__":
    # cli: python vectorize_txt.py [project_name]
    import sys

    proj = sys.argv[1] if len(sys.argv) > 1 else None
    convert_project_to_vector_db(proj)
