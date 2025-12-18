#!/usr/bin/env python3
"""Simple integration test runner for this project.

Features:
- Calls `scripts/chroma_cli.py` (via the same Python interpreter) to test add/list/query/delete.
- Posts simulated webhook payloads to the running FastAPI server to test webhook handling.
- Tests KB cache behavior via `/admin/cache_status` (requires header X-TEST-ADMIN: 1)

Usage:
  python test.py [--server http://127.0.0.1:8000] [--collection AutoSpritze] [--project AutoSpritze]

Notes:
- Run this while the server is running (uvicorn main:app --reload) in the same environment.
- Ensure the server's ADMIN_NUMBERS includes the admin number you pass if you want to test admin webhook commands.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import traceback
from typing import Tuple

import requests

SCRIPT_DIR = os.path.dirname(__file__)
CHROMA_CLI = os.path.join(SCRIPT_DIR, "scripts", "chroma_cli.py")

# Detect whether chromadb and the helper script are available. If not, skip helper-based tests.
def check_chromadb_available() -> bool:
    try:
        import chromadb  # type: ignore
        return True
    except Exception:
        return False

CHROMADB_AVAILABLE = check_chromadb_available()
CHROMA_CLI_EXISTS = os.path.exists(CHROMA_CLI)
if not (CHROMADB_AVAILABLE and CHROMA_CLI_EXISTS):
    print("[INFO] chromadb or chroma_cli not available; helper add/query/delete tests will be skipped")

DEFAULT_SERVER = "http://127.0.0.1:8000"


def run_chroma_cli(args) -> dict:
    cmd = [sys.executable, CHROMA_CLI, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = proc.stdout.strip() or proc.stderr.strip()
    try:
        return json.loads(out)
    except Exception as e:
        return {"ok": False, "error": f"Invalid JSON from helper: {e}; raw: {out}", "raw": out}


def ensure_db_params(project: str = None, collection: str = None) -> Tuple[str, str]:
    from app.config.helpers import get_project_paths, COLLECTION_NAME, PROJECT_NAME
    if project and collection:
        txt_folder, db_path = get_project_paths(project)
        return db_path, collection
    txt_folder, db_path = get_project_paths(PROJECT_NAME)
    return db_path, COLLECTION_NAME


def test_helper_add_query_delete(db_path: str, collection: str) -> bool:
    print("\n== Helper add/query/delete test ==")
    if not (CHROMADB_AVAILABLE and CHROMA_CLI_EXISTS):
        print("[SKIP] chromadb or chroma_cli missing; skipping helper add/query/delete test")
        return True

    test_text = f"TEST_ENTRY_{uuid.uuid4().hex[:8]}: The quick brown fox jumps over the lazy dog"

    print("Adding document via helper...")
    resp = run_chroma_cli(["add", "--db-path", db_path, "--collection", collection, "--text", test_text, "--source", "test_runner"]) 
    print(resp)
    if not resp.get("ok"):
        print("Add failed:", resp.get("error"))
        return False
    doc_id = resp["data"].get("doc_id")
    if not doc_id:
        print("No doc_id returned")
        return False

    print("Listing to confirm presence...")
    resp = run_chroma_cli(["list", "--db-path", db_path, "--collection", collection])
    if not resp.get("ok"):
        print("List failed:", resp.get("error"))
        return False
    ids = resp["data"].get("ids", [])
    if doc_id not in ids:
        print("Added doc_id not found in listing", ids[:10])
        return False

    print("Querying for relevance...")
    resp = run_chroma_cli(["query", "--db-path", db_path, "--collection", collection, "--question", "quick brown fox", "--k", "3"])
    if not resp.get("ok"):
        print("Query failed:", resp.get("error"))
        return False

    print("Deleting the document via helper...")
    resp = run_chroma_cli(["delete", "--db-path", db_path, "--collection", collection, "--id", doc_id])
    if not resp.get("ok"):
        print("Delete failed:", resp.get("error"))
        return False
    if not resp["data"].get("deleted"):
        print("Delete reported no deletion:", resp)
        return False

    print("Confirm deletion via listing...")
    resp = run_chroma_cli(["list", "--db-path", db_path, "--collection", collection])
    if not resp.get("ok"):
        print("List after delete failed:", resp.get("error"))
        return False
    ids = resp["data"].get("ids", [])
    if doc_id in ids:
        print("Deleted doc_id still present")
        return False

    print("Helper add/query/delete test passed")
    return True


def post_webhook(server: str, from_number: str, body_text: str, msg_type: str = "text", return_reply: bool = False, phone_number_id: str | None = None) -> dict:
    url = server.rstrip("/") + "/webhook/whatsapp"
    if msg_type == "text":
        msg = {"type": "text", "from": from_number, "text": {"body": body_text}}
    else:
        msg = {"type": msg_type, "from": from_number}

    # Resolve phone_number_id: explicit param > env PHONE_NUMBER_ID > env META_PHONE_NUMBER_ID > fallback 'test-phone'
    phone_id = phone_number_id or os.getenv("PHONE_NUMBER_ID") or os.getenv("META_PHONE_NUMBER_ID") or "test-phone"

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_id},
                            "messages": [msg],
                        }
                    }
                ]
            }
        ]
    }
    headers = {"Content-Type": "application/json"}
    if return_reply:
        headers["X-TEST-RETURN-REPLY"] = "1"

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        try:
            parsed = r.json()
        except Exception:
            parsed = None
        result = {"ok": True, "status_code": r.status_code, "json": parsed, "text": r.text}
        # If the server returned a reply (test mode), expose it as `reply` at top-level for convenience
        if parsed and isinstance(parsed, dict) and parsed.get("reply"):
            result["reply"] = parsed.get("reply")
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def test_webhook_user_query(server: str, user_number: str) -> bool:
    print("\n== Webhook user query test ==")
    res = post_webhook(server, user_number, "Hello, can you help me with a question?", return_reply=True)
    print(res)
    if not res.get("ok"):
        print("Webhook request failed:", res.get("error"))
        return False
    if res.get("reply"):
        print("Received assistant reply:\n", res["reply"])
        return True
    print("Webhook did not return a reply; raw:", res)
    return False


def test_admin_via_webhook(server: str, admin_number: str, db_path: str, collection: str) -> bool:
    print("\n== Webhook admin add/list/delete test ==")
    # /add via webhook
    add_text = f"ADMIN_TEST_{uuid.uuid4().hex[:8]}: admin added content"
    res = post_webhook(server, admin_number, "/add " + add_text, return_reply=True)
    print(res)
    if not res.get("ok"):
        print("Admin webhook /add failed to reach server:", res.get("error"))
        return False
    if res.get("json") and res["json"].get("status") == "admin_add_done":
        print("Server reported admin_add_done;")
        if not (CHROMADB_AVAILABLE and CHROMA_CLI_EXISTS):
            print("[INFO] chromadb/chroma_cli not available; skipping helper verification for admin add")
            return True
        print("verify via helper...")
        # Wait briefly for helper persistence
        time.sleep(1)
        resp = run_chroma_cli(["list", "--db-path", db_path, "--collection", collection])
        print(resp)
        if not resp.get("ok"):
            print("Helper list failed:", resp.get("error"))
            return False
        print("Admin webhook add appears to have completed (inspect DB listing above)")
        return True
    else:
        print("Server did not accept admin add (likely admin number not configured). Response:", res)
        return False


def test_cache_behavior(server: str, user_number: str, db_path: str, collection: str) -> bool:
    print("\n== Cache behavior test ==")
    # initial cache snapshot
    try:
        r = requests.get(server.rstrip('/') + '/admin/cache_status', headers={'X-TEST-ADMIN': '1'}, timeout=5)
        before = r.json()
    except Exception as e:
        print("Failed to get cache status:", e)
        return False

    print("Cache before:", before)

    # make a user request (should populate cache)
    resp = post_webhook(server, user_number, "Tell me something about the product.", return_reply=True)
    print("User webhook resp:", resp)
    time.sleep(0.5)

    try:
        r = requests.get(server.rstrip('/') + '/admin/cache_status', headers={'X-TEST-ADMIN': '1'}, timeout=5)
        after = r.json()
    except Exception as e:
        print("Failed to get cache status after request:", e)
        return False

    print("Cache after:", after)

    key = f"{user_number}|k=5"
    if key not in after.get('keys', []):
        print("Expected cache key not found after first query:", key)
        return False

    # second user request should hit cache (no change in keys)
    resp2 = post_webhook(server, user_number, "Another quick question.", return_reply=True)
    print("Second user webhook resp:", resp2)
    time.sleep(0.2)

    try:
        r = requests.get(server.rstrip('/') + '/admin/cache_status', headers={'X-TEST-ADMIN': '1'}, timeout=5)
        after2 = r.json()
    except Exception as e:
        print("Failed to get cache status after second request:", e)
        return False

    print("Cache after second request:", after2)

    if key not in after2.get('keys', []):
        print("Expected cache key missing after second request")
        return False

    print("Cache behavior test passed")
    return True


def test_admin_config_endpoint(server: str, admin_number: str) -> bool:
    print("\n== Admin config endpoint test ==")
    headers = {"X-TEST-ADMIN": "1"}
    try:
        r = requests.get(server.rstrip('/') + '/admin/config', headers=headers, timeout=5)
        j = r.json()
        print('Config:', j)
        admins = j.get('admin_numbers', [])
        if admin_number not in admins:
            print(f"Provided admin_number {admin_number} not present in server ADMIN_NUMBERS: {admins}")
            return False
        print('Admin number configured correctly on server')
        return True
    except Exception as e:
        print('Failed to get admin config:', e)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--project", default=None)
    ap.add_argument("--collection", default=None)
    ap.add_argument("--user-number", default="user_test_1")
    ap.add_argument("--admin-number", default=None, help="Set to a number present in ADMIN_NUMBERS or leave blank to use the first ADMIN_NUMBERS entry if available")
    ap.add_argument("--phone-number-id", default=None, help="Phone number id to place in metadata.phone_number_id")
    ap.add_argument("--skip-webhook", action="store_true")
    args = ap.parse_args()

    # If admin-number not provided, try to take from environment ADMIN_NUMBERS (first entry)
    if not args.admin_number:
        env_admins = os.getenv("ADMIN_NUMBERS", "")
        if env_admins:
            first = [x.strip() for x in env_admins.split(",") if x.strip()]
            if first:
                args.admin_number = first[0]
                print(f"[INFO] Using admin-number from ADMIN_NUMBERS: {args.admin_number}")
            else:
                args.admin_number = "admin_test_1"
        else:
            args.admin_number = "admin_test_1"

    # If phone_number_id provided, export it to env so helper/test code can pick it up
    if args.phone_number_id:
        os.environ["PHONE_NUMBER_ID"] = args.phone_number_id

    try:
        db_path, collection = ensure_db_params(args.project, args.collection)
    except Exception as e:
        print("Failed to determine DB path/collection:", e)
        sys.exit(2)

    ok = True
    try:
        ok &= test_helper_add_query_delete(db_path, collection)
    except Exception:
        print("Exception during helper test:\n", traceback.format_exc())
        ok = False

    if not args.skip_webhook:
        try:
            ok &= test_webhook_user_query(args.server, args.user_number)
        except Exception:
            print("Exception during webhook user test:\n", traceback.format_exc())
            ok = False

        try:
            ok &= test_admin_via_webhook(args.server, args.admin_number, db_path, collection)
        except Exception:
            print("Exception during webhook admin test:\n", traceback.format_exc())
            ok = False

        try:
            ok &= test_cache_behavior(args.server, args.user_number, db_path, collection)
        except Exception:
            print("Exception during cache behavior test:\n", traceback.format_exc())
            ok = False

    try:
        ok &= test_admin_config_endpoint(args.server, args.admin_number)
    except Exception:
        print("Exception during admin config test:\n", traceback.format_exc())
        ok = False
    main()