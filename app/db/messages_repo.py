from datetime import datetime
from .conn import db_conn

def db_init():
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL,
                    phone_number TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('in','out')),
                    text TEXT NOT NULL,
                    cache_hit BOOLEAN,
                    context_len INTEGER,
                    t_retrieval_ms REAL,
                    t_total_ms REAL
                );
                """
            )
    finally:
        conn.close()

def log_message(
    phone_number: str,
    direction: str,
    text: str,
    cache_hit=None,
    context_len=None,
    t_retrieval_ms=None,
    t_total_ms=None,
):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages
                (ts, phone_number, direction, text, cache_hit, context_len, t_retrieval_ms, t_total_ms)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    datetime.utcnow(),
                    phone_number,
                    direction,
                    text,
                    cache_hit,
                    context_len,
                    t_retrieval_ms,
                    t_total_ms,
                ),
            )
    finally:
        conn.close()

def list_phone_numbers(limit: int = 200):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT phone_number, COUNT(*) AS msg_count, MAX(ts) AS last_ts
                FROM messages
                GROUP BY phone_number
                ORDER BY last_ts DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [
                {"phone_number": r[0], "msg_count": int(r[1]), "last_ts": r[2].isoformat()}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()

def fetch_messages(
    phone_number: str | None = None,
    direction: str | None = None,   # 'in' or 'out'
    limit: int = 100,
    offset: int = 0,
):
    where = []
    params = []

    if phone_number:
        where.append("phone_number = %s")
        params.append(phone_number)

    if direction in ("in", "out"):
        where.append("direction = %s")
        params.append(direction)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.extend([limit, offset])

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, ts, phone_number, direction, text, cache_hit, context_len, t_retrieval_ms, t_total_ms
                FROM messages
                {where_sql}
                ORDER BY ts DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()

            return [
                {
                    "id": r[0],
                    "ts": r[1].isoformat(),
                    "phone_number": r[2],
                    "direction": r[3],
                    "text": r[4],
                    "cache_hit": r[5],
                    "context_len": r[6],
                    "t_retrieval_ms": r[7],
                    "t_total_ms": r[8],
                }
                for r in rows
            ]
    finally:
        conn.close()
