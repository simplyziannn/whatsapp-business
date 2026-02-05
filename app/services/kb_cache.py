import threading
import time
import app.config.settings as settings

kb_version = 0
conversation_contexts: dict = {}
cache_lock = threading.Lock()


def bump_kb_version():
    """Bump KB version and clear in-memory cache so contexts are refreshed."""
    global kb_version
    with cache_lock:
        kb_version += 1
        conversation_contexts.clear()


def _context_cache_key(from_number: str, kb_type: str, k: int) -> str:
    return f"{from_number}|{kb_type}|k={k}"


def get_cached_context(
    from_number: str,
    question: str,
    kb_type: str,
    retrieve_fn,
    k: int = 5,
    force_refresh: bool = False,
    return_meta: bool = False,
):
    """
    Cached context per (user, kb_type, k).

    retrieve_fn(question, k) should return the context string.
    If return_meta=True, returns (context, cache_hit: bool).
    """
    key = _context_cache_key(from_number, kb_type, k)
    now = time.time()

    with cache_lock:
        entry = conversation_contexts.get(key)
        if (
            not force_refresh
            and entry
            and entry.get("version") == kb_version
            and (now - entry.get("ts", 0)) < settings.CACHE_MAX_AGE
        ):
            ctx = entry.get("context", "")
            return (ctx, True) if return_meta else ctx

    # Cache miss → retrieve
    context = retrieve_fn(question, k=k)

    with cache_lock:
        conversation_contexts[key] = {
            "context": context,
            "version": kb_version,
            "ts": now,
        }

    return (context, False) if return_meta else context


def clear_cached_context(
    from_number: str | None = None,
    kb_type: str | None = None,
    k: int | None = None,
):
    """
    Clear cached contexts selectively.

    - If from_number is None: clear entire cache.
    - If from_number is provided:
        - If k is None and kb_type is None: clear all entries for that phone number.
        - If k is not None and kb_type is None: clear all entries for that phone number at that k (all kb_types).
        - If kb_type is not None and k is None: clear all entries for that phone number for that kb_type (all k).
        - If kb_type is not None and k is not None: clear only that specific entry.
    """
    with cache_lock:
        if from_number is None:
            conversation_contexts.clear()
            return

        # Build removal list based on provided filters
        prefix = f"{from_number}|"
        keys = list(conversation_contexts.keys())

        if kb_type is None and k is None:
            # Remove all keys for this user
            keys_to_remove = [kk for kk in keys if kk.startswith(prefix)]
        elif kb_type is None and k is not None:
            # Remove all kb_types for this user at this k
            suffix = f"|k={k}"
            keys_to_remove = [kk for kk in keys if kk.startswith(prefix) and kk.endswith(suffix)]
        elif kb_type is not None and k is None:
            # Remove all k for this user and kb_type
            mid = f"{from_number}|{kb_type}|"
            keys_to_remove = [kk for kk in keys if kk.startswith(mid)]
        else:
            # Remove specific entry
            key = _context_cache_key(from_number, kb_type, k)
            keys_to_remove = [key]

        for kk in keys_to_remove:
            conversation_contexts.pop(kk, None)


def cache_status():
    """Used by /admin/cache_status endpoint."""
    with cache_lock:
        keys = list(conversation_contexts.keys())
        details = {k: {"version": v["version"], "ts": v["ts"]} for k, v in conversation_contexts.items()}
        return {"kb_version": kb_version, "keys": keys, "details": details}
