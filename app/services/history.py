import time


conversation_history = {}
conversation_last_activity = {}  # phone_number -> last_activity_ts


def is_stale(from_number: str, max_age_seconds: int) -> bool:
    last = conversation_last_activity.get(from_number)
    if last is None:
        return False
    return (time.time() - last) > max_age_seconds


def touch(from_number: str):
    conversation_last_activity[from_number] = time.time()


def clear(from_number: str):
    conversation_history.pop(from_number, None)
    conversation_last_activity.pop(from_number, None)


def get_history(from_number: str):
    return conversation_history.get(from_number, [])


def set_history(from_number: str, history):
    conversation_history[from_number] = history
