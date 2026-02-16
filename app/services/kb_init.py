import os
from app.config.helpers import get_project_paths, PROJECT_NAME
from app.services.chroma_store import get_collection


def kb_init_if_empty():
    txt_folder, persist_dir = get_project_paths(PROJECT_NAME)

    print("[KB_INIT] PROJECT_NAME:", PROJECT_NAME)
    print("[KB_INIT] txt_folder:", txt_folder)
    print("[KB_INIT] txt_folder files:", os.listdir(txt_folder) if os.path.exists(txt_folder) else "MISSING")
    print("[KB_INIT] persist_dir:", persist_dir)

    cols = ["kb_menu", "kb_contact", "kb_general"]
    counts = {name: get_collection(name).count() for name in cols}
    total = sum(counts.values())
    print("[KB_INIT] Current counts:", counts)

    if total == 0:
        print("[KB_INIT] No KB rows found. Rebuilding from txt...")
        from app.config.vectorize_txt import vectorize_kb_structure

        vectorize_kb_structure(txt_folder, persist_dir)
        counts_after = {name: get_collection(name).count() for name in cols}
        print("[KB_INIT] Counts after rebuild:", counts_after)
        print("[KB_INIT] Rebuild complete.")
    else:
        print("[KB_INIT] KB rows already exist; skipping rebuild.")
