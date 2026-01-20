import os
from app.config.helpers import get_project_paths, PROJECT_NAME


def kb_init_if_empty():
    txt_folder, persist_dir = get_project_paths(PROJECT_NAME)

    print("[KB_INIT] PROJECT_NAME:", PROJECT_NAME)
    print("[KB_INIT] txt_folder:", txt_folder)
    print("[KB_INIT] txt_folder files:", os.listdir(txt_folder) if os.path.exists(txt_folder) else "MISSING")
    print("[KB_INIT] persist_dir:", persist_dir)
    print("[KB_INIT] persist_dir files:", os.listdir(persist_dir) if os.path.exists(persist_dir) else "MISSING")

    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(path=persist_dir, settings=Settings(allow_reset=False))
    cols = client.list_collections()

    if not cols:
        print("[KB_INIT] No Chroma collections found. Rebuilding from txt...")
        from app.config.vectorize_txt import convert_txt_folder_to_vector_db
        convert_txt_folder_to_vector_db(txt_folder, persist_dir)

        cols = client.list_collections()
        print("[KB_INIT] Collections after rebuild:", [c.name for c in cols])
        print("[KB_INIT] Rebuild complete.")
    else:
        print("[KB_INIT] Chroma collections exist:", [c.name for c in cols])
