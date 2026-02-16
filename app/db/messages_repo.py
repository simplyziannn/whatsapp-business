from datetime import datetime, timezone, date
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
                    meta_phone_number_id TEXT,
                    company_id INTEGER,
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
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS meta_phone_number_id TEXT;")
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS company_id INTEGER;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_company_ts ON messages(company_id, ts DESC);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_meta_phone_id ON messages(meta_phone_number_id);")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_inbound (
                    message_id TEXT PRIMARY KEY,
                    first_seen_ts TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_usage (
                    phone_number TEXT NOT NULL,
                    day DATE NOT NULL,
                    count INTEGER NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (phone_number, day)
                );
                """
            )


    finally:
        conn.close()

def log_message(
    phone_number: str,
    direction: str,
    text: str,
    meta_phone_number_id: str | None = None,
    company_id: int | None = None,
    cache_hit=None,
    context_len=None,
    t_retrieval_ms=None,
    t_total_ms=None,
):
    if company_id is None and meta_phone_number_id:
        from app.db import tenants_repo
        company_id = tenants_repo.resolve_company_id_by_phone_id(meta_phone_number_id)

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages
                (ts, meta_phone_number_id, company_id, phone_number, direction, text, cache_hit, context_len, t_retrieval_ms, t_total_ms)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    datetime.utcnow(),
                    meta_phone_number_id,
                    company_id,
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
    """
    Returns list of phone numbers with:
    - phone_number
    - msg_count
    - in_count
    - out_count
    - last_ts
    """
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                phone_number,
                COUNT(*) AS msg_count,
                SUM(CASE WHEN direction = 'in' THEN 1 ELSE 0 END)  AS in_count,
                SUM(CASE WHEN direction = 'out' THEN 1 ELSE 0 END) AS out_count,
                MAX(ts) AS last_ts
            FROM messages
            GROUP BY phone_number
            ORDER BY last_ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    items = []
    for r in rows:
        items.append(
            {
                "phone_number": r[0],
                "msg_count": int(r[1] or 0),
                "in_count": int(r[2] or 0),
                "out_count": int(r[3] or 0),
                "last_ts": r[4].isoformat() if r[4] else None,
            }
        )
    return items


def list_phone_numbers_scoped(limit: int = 200, company_id: int | None = None):
    conn = db_conn()
    with conn.cursor() as cur:
        if company_id is None:
            cur.execute(
                """
                SELECT
                    phone_number,
                    COUNT(*) AS msg_count,
                    SUM(CASE WHEN direction = 'in' THEN 1 ELSE 0 END)  AS in_count,
                    SUM(CASE WHEN direction = 'out' THEN 1 ELSE 0 END) AS out_count,
                    MAX(ts) AS last_ts
                FROM messages
                GROUP BY phone_number
                ORDER BY last_ts DESC
                LIMIT %s
                """,
                (limit,),
            )
        else:
            include_unassigned = False
            try:
                from app.db import tenants_repo

                include_unassigned = company_id == tenants_repo.get_default_company_id()
            except Exception:
                include_unassigned = False
            cur.execute(
                """
                SELECT
                    phone_number,
                    COUNT(*) AS msg_count,
                    SUM(CASE WHEN direction = 'in' THEN 1 ELSE 0 END)  AS in_count,
                    SUM(CASE WHEN direction = 'out' THEN 1 ELSE 0 END) AS out_count,
                    MAX(ts) AS last_ts
                FROM messages
                WHERE (company_id = %s OR (%s AND company_id IS NULL))
                GROUP BY phone_number
                ORDER BY last_ts DESC
                LIMIT %s
                """,
                (company_id, include_unassigned, limit),
            )
        rows = cur.fetchall()
    return [
        {
            "phone_number": r[0],
            "msg_count": int(r[1] or 0),
            "in_count": int(r[2] or 0),
            "out_count": int(r[3] or 0),
            "last_ts": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


def fetch_messages(
    company_id: int | None = None,
    phone_number: str | None = None,
    direction: str | None = None,   # 'in' or 'out'
    limit: int = 100,
    offset: int = 0,
):
    where = []
    params = []

    if company_id is not None:
        include_unassigned = False
        try:
            from app.db import tenants_repo

            include_unassigned = company_id == tenants_repo.get_default_company_id()
        except Exception:
            include_unassigned = False

        where.append("(company_id = %s OR (%s AND company_id IS NULL))")
        params.extend([company_id, include_unassigned])

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


def claim_inbound_message_id(message_id: str) -> bool:
    """
    Idempotency guard.
    Returns True if this message_id is new and successfully claimed.
    Returns False if we've already seen it before.
    """
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO processed_inbound (message_id, first_seen_ts)
                VALUES (%s, %s)
                ON CONFLICT (message_id) DO NOTHING
                """,
                (message_id, datetime.utcnow()),
            )
            return cur.rowcount == 1
    finally:
        conn.close()

def increment_daily_usage(phone_number: str, day: date) -> int:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO daily_usage (phone_number, day, count, updated_at)
                VALUES (%s, %s, 1, %s)
                ON CONFLICT (phone_number, day)
                DO UPDATE SET
                    count = daily_usage.count + 1,
                    updated_at = EXCLUDED.updated_at
                RETURNING count
                """,
                (phone_number, day, datetime.utcnow()),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()
