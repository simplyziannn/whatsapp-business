import threading
import time

processed_inbound_ids = {}
processed_lock = threading.Lock()
PROCESSED_TTL = 24 * 3600  # 24h


def seen_recent(msg_id: str) -> bool:
    """
    Returns True if msg_id was seen recently (within TTL), else records it and returns False.
    Also performs TTL cleanup.
    """
    now = time.time()
    with processed_lock:
        old_keys = [k for k, ts in processed_inbound_ids.items() if (now - ts) > PROCESSED_TTL]
        for k in old_keys:
            processed_inbound_ids.pop(k, None)

        if msg_id in processed_inbound_ids:
            return True

        processed_inbound_ids[msg_id] = now
        return False
