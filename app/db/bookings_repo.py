from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo
from app.db.conn import db_conn

SG_TZ = ZoneInfo("Asia/Singapore")

def db_init_bookings():
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            # booking_context: stores partial booking info across messages (service and/or datetime)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS booking_context (
                    customer_number TEXT PRIMARY KEY,
                    updated_ts TIMESTAMPTZ NOT NULL,
                    expires_ts TIMESTAMPTZ NOT NULL,
                    pending_service_key TEXT,
                    pending_service_label TEXT,
                    pending_start_local TEXT
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_context_expires ON booking_context (expires_ts);")

            # booking_requests: pending -> approved/rejected
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS booking_requests (
                    id SERIAL PRIMARY KEY,
                    created_ts TIMESTAMPTZ NOT NULL,
                    meta_phone_number_id TEXT NOT NULL,
                    customer_number TEXT NOT NULL,
                    service_key TEXT NOT NULL,
                    service_label TEXT NOT NULL,
                    start_ts TIMESTAMPTZ NOT NULL,
                    end_ts TIMESTAMPTZ NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired')),
                    admin_number TEXT,
                    admin_decision_ts TIMESTAMPTZ,
                    admin_note TEXT
                );
                """
            )

            # Holds to prevent double booking while waiting for admin
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS booking_holds (
                    id SERIAL PRIMARY KEY,
                    created_ts TIMESTAMPTZ NOT NULL,
                    expires_ts TIMESTAMPTZ NOT NULL,
                    customer_number TEXT NOT NULL,
                    service_key TEXT NOT NULL,
                    start_ts TIMESTAMPTZ NOT NULL,
                    end_ts TIMESTAMPTZ NOT NULL,
                    request_id INTEGER,
                    status TEXT NOT NULL CHECK (status IN ('active','released','expired'))
                );
                """
            )

            # Drafts: proposed slots awaiting customer confirmation
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS booking_drafts (
                    id SERIAL PRIMARY KEY,
                    created_ts TIMESTAMPTZ NOT NULL,
                    expires_ts TIMESTAMPTZ NOT NULL,
                    meta_phone_number_id TEXT NOT NULL,
                    customer_number TEXT NOT NULL,
                    service_key TEXT NOT NULL,
                    service_label TEXT NOT NULL,
                    start_ts TIMESTAMPTZ NOT NULL,
                    end_ts TIMESTAMPTZ NOT NULL,
                    hold_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('proposed','confirmed','cancelled','expired'))
                );
                """
            )

            # Indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_drafts_customer ON booking_drafts (customer_number, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_holds_window ON booking_holds (start_ts, end_ts);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_holds_expires ON booking_holds (expires_ts);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_requests_status ON booking_requests (status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_requests_window ON booking_requests (start_ts, end_ts);")

        conn.commit()
    finally:
        conn.close()



def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return (a_start < b_end) and (b_start < a_end)


def expire_old_holds(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(tz=SG_TZ)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE booking_holds
                SET status = 'expired'
                WHERE status = 'active' AND expires_ts <= %s
                """,
                (now,),
            )
            return cur.rowcount
    finally:
        conn.close()

def is_window_available(start_ts: datetime, end_ts: datetime, ignore_hold_id: int | None = None) -> bool:
    """
    Available if no approved booking overlaps AND no active hold overlaps.
    ignore_hold_id: used when confirming a draft, so we don't block on our own hold.
    """
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            # block overlaps with approved requests
            cur.execute(
                """
                SELECT COUNT(*)
                FROM booking_requests
                WHERE status = 'approved'
                  AND start_ts < %s
                  AND end_ts > %s
                """,
                (end_ts, start_ts),
            )
            approved_cnt = int(cur.fetchone()[0] or 0)
            if approved_cnt > 0:
                return False

            # block overlaps with active holds (optionally ignore one hold)
            if ignore_hold_id is None:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM booking_holds
                    WHERE status = 'active'
                      AND start_ts < %s
                      AND end_ts > %s
                    """,
                    (end_ts, start_ts),
                )
            else:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM booking_holds
                    WHERE status = 'active'
                      AND id <> %s
                      AND start_ts < %s
                      AND end_ts > %s
                    """,
                    (ignore_hold_id, end_ts, start_ts),
                )

            hold_cnt = int(cur.fetchone()[0] or 0)
            return hold_cnt == 0
    finally:
        conn.close()


def create_hold(
    customer_number: str,
    service_key: str,
    start_ts: datetime,
    end_ts: datetime,
    hold_minutes: int = 10,
) -> int:
    now = datetime.now(tz=SG_TZ)
    expires = now + timedelta(minutes=hold_minutes)

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO booking_holds
                    (created_ts, expires_ts, customer_number, service_key, start_ts, end_ts, status)
                VALUES
                    (%s, %s, %s, %s, %s, %s, 'active')
                RETURNING id
                """,
                (now, expires, customer_number, service_key, start_ts, end_ts),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def release_hold(hold_id: int) -> None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE booking_holds
                SET status = 'released'
                WHERE id = %s
                """,
                (hold_id,),
            )
    finally:
        conn.close()


def create_booking_request(
    meta_phone_number_id: str,
    customer_number: str,
    service_key: str,
    service_label: str,
    start_ts: datetime,
    end_ts: datetime,
) -> int:
    now = datetime.now(tz=SG_TZ)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO booking_requests
                    (created_ts, meta_phone_number_id, customer_number, service_key, service_label, start_ts, end_ts, status)
                VALUES
                    (%s,%s,%s,%s,%s,%s,%s,'pending')
                RETURNING id
                """,
                (now, meta_phone_number_id, customer_number, service_key, service_label, start_ts, end_ts),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def link_hold_to_request(hold_id: int, request_id: int) -> None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE booking_holds
                SET request_id = %s
                WHERE id = %s
                """,
                (request_id, hold_id),
            )
    finally:
        conn.close()


def list_pending_requests(limit: int = 50) -> list[dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_ts, customer_number, service_label, start_ts, end_ts, status
                FROM booking_requests
                WHERE status = 'pending'
                ORDER BY created_ts DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "created_ts": r[1].isoformat(),
                    "customer_number": r[2],
                    "service_label": r[3],
                    "start_ts": r[4].isoformat(),
                    "end_ts": r[5].isoformat(),
                    "status": r[6],
                }
                for r in rows
            ]
    finally:
        conn.close()

def list_requests(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            if not status or status == "all":
                cur.execute(
                    """
                    SELECT id, created_ts, customer_number, service_label, start_ts, end_ts, status
                    FROM booking_requests
                    ORDER BY created_ts DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, created_ts, customer_number, service_label, start_ts, end_ts, status
                    FROM booking_requests
                    WHERE status = %s
                    ORDER BY created_ts DESC
                    LIMIT %s
                    """,
                    (status, limit),
                )

            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "created_ts": r[1].isoformat(),
                    "customer_number": r[2],
                    "service_label": r[3],
                    "start_ts": r[4].isoformat(),
                    "end_ts": r[5].isoformat(),
                    "status": r[6],
                }
                for r in rows
            ]
    finally:
        conn.close()


def get_request(request_id: int) -> Optional[dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, meta_phone_number_id, customer_number, service_key, service_label,
                       start_ts, end_ts, status
                FROM booking_requests
                WHERE id = %s
                """,
                (request_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0],
                "meta_phone_number_id": r[1],
                "customer_number": r[2],
                "service_key": r[3],
                "service_label": r[4],
                "start_ts": r[5],
                "end_ts": r[6],
                "status": r[7],
            }
    finally:
        conn.close()


def decide_request(request_id: int, admin_number: str, decision: str, admin_note: str | None = None) -> bool:
    assert decision in ("approved", "rejected")

    now = datetime.now(tz=SG_TZ)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE booking_requests
                SET status = %s,
                    admin_number = %s,
                    admin_decision_ts = %s,
                    admin_note = %s
                WHERE id = %s AND status = 'pending'
                """,
                (decision, admin_number, now, admin_note, request_id),
            )
            return cur.rowcount == 1
    finally:
        conn.close()


def find_hold_by_request(request_id: int) -> Optional[int]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM booking_holds
                WHERE request_id = %s
                ORDER BY created_ts DESC
                LIMIT 1
                """,
                (request_id,),
            )
            r = cur.fetchone()
            return int(r[0]) if r else None
    finally:
        conn.close()


def expire_old_drafts(now: datetime | None = None) -> int:
    now = now or datetime.now(tz=SG_TZ)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            # Find drafts that are expiring now
            cur.execute(
                """
                SELECT hold_id
                FROM booking_drafts
                WHERE status = 'proposed' AND expires_ts <= %s
                """,
                (now,),
            )
            hold_ids = [r[0] for r in cur.fetchall()]

            # Expire drafts
            cur.execute(
                """
                UPDATE booking_drafts
                SET status = 'expired'
                WHERE status = 'proposed' AND expires_ts <= %s
                """,
                (now,),
            )
            expired_cnt = cur.rowcount

            # Release holds immediately
            if hold_ids:
                cur.execute(
                    """
                    UPDATE booking_holds
                    SET status = 'released'
                    WHERE id = ANY(%s) AND status = 'active'
                    """,
                    (hold_ids,),
                )

            return expired_cnt
    finally:
        conn.close()



def create_draft(
    meta_phone_number_id: str,
    customer_number: str,
    service_key: str,
    service_label: str,
    start_ts,
    end_ts,
    hold_id: int,
    hold_minutes: int,
) -> int:
    now = datetime.now(tz=SG_TZ)
    expires = now + timedelta(minutes=hold_minutes)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            # Enforce only 1 active draft per customer:
            # cancel existing proposed drafts and release their holds
            cur.execute(
                """
                SELECT id, hold_id
                FROM booking_drafts
                WHERE customer_number = %s AND status = 'proposed'
                """,
                (customer_number,),
            )
            old_rows = cur.fetchall()
            if old_rows:
                old_hold_ids = [r[1] for r in old_rows]

                cur.execute(
                    """
                    UPDATE booking_drafts
                    SET status = 'cancelled'
                    WHERE customer_number = %s AND status = 'proposed'
                    """,
                    (customer_number,),
                )

                cur.execute(
                    """
                    UPDATE booking_holds
                    SET status = 'released'
                    WHERE id = ANY(%s) AND status = 'active'
                    """,
                    (old_hold_ids,),
                )

            cur.execute(
                """
                INSERT INTO booking_drafts
                    (created_ts, expires_ts, meta_phone_number_id, customer_number,
                     service_key, service_label, start_ts, end_ts, hold_id, status)
                VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,'proposed')
                RETURNING id
                """,
                (now, expires, meta_phone_number_id, customer_number, service_key, service_label, start_ts, end_ts, hold_id),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()

def get_draft_by_id(draft_id: int) -> Optional[dict[str, Any]]:
    expire_old_drafts()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, meta_phone_number_id, customer_number, service_key, service_label,
                       start_ts, end_ts, hold_id, expires_ts
                FROM booking_drafts
                WHERE id = %s
                """,
                (draft_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0],
                "status": r[1],
                "meta_phone_number_id": r[2],
                "customer_number": r[3],
                "service_key": r[4],
                "service_label": r[5],
                "start_ts": r[6],
                "end_ts": r[7],
                "hold_id": r[8],
                "expires_ts": r[9],
            }
    finally:
        conn.close()

def get_draft_by_id(draft_id: int):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, meta_phone_number_id, customer_number, service_key, service_label,
                       start_ts, end_ts, hold_id, status, expires_ts
                FROM booking_drafts
                WHERE id = %s
                """,
                (draft_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "meta_phone_number_id": row[1],
                "customer_number": row[2],
                "service_key": row[3],
                "service_label": row[4],
                "start_ts": row[5],
                "end_ts": row[6],
                "hold_id": row[7],
                "status": row[8],
                "expires_ts": row[9],
            }
    finally:
        conn.close()


def get_active_draft(customer_number: str):
    expire_old_drafts()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, meta_phone_number_id, service_key, service_label, start_ts, end_ts, hold_id
                FROM booking_drafts
                WHERE customer_number = %s AND status = 'proposed'
                ORDER BY created_ts DESC
                LIMIT 1
                """,
                (customer_number,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0],
                "meta_phone_number_id": r[1],
                "service_key": r[2],
                "service_label": r[3],
                "start_ts": r[4],
                "end_ts": r[5],
                "hold_id": r[6],
            }
    finally:
        conn.close()


def mark_draft(customer_number: str, draft_id: int, status: str) -> None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE booking_drafts
                SET status = %s
                WHERE id = %s AND customer_number = %s
                """,
                (status, draft_id, customer_number),
            )
    finally:
        conn.close()

def upsert_booking_context(
    customer_number: str,
    pending_service_key: str | None = None,
    pending_service_label: str | None = None,
    pending_start_local: str | None = None,
    ttl_minutes: int = 30,
) -> None:
    now = datetime.now(tz=SG_TZ)
    expires = now + timedelta(minutes=ttl_minutes)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO booking_context (customer_number, updated_ts, expires_ts, pending_service_key, pending_service_label, pending_start_local)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (customer_number)
                DO UPDATE SET
                    updated_ts = EXCLUDED.updated_ts,
                    expires_ts = EXCLUDED.expires_ts,
                    pending_service_key = COALESCE(EXCLUDED.pending_service_key, booking_context.pending_service_key),
                    pending_service_label = COALESCE(EXCLUDED.pending_service_label, booking_context.pending_service_label),
                    pending_start_local = COALESCE(EXCLUDED.pending_start_local, booking_context.pending_start_local)
                """,
                (customer_number, now, expires, pending_service_key, pending_service_label, pending_start_local),
            )
    finally:
        conn.close()


def get_booking_context(customer_number: str):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pending_service_key, pending_service_label, pending_start_local, expires_ts
                FROM booking_context
                WHERE customer_number = %s
                """,
                (customer_number,),
            )
            row = cur.fetchone()
            if not row:
                return None
            # auto-expire
            expires_ts = row[3]
            if expires_ts and expires_ts <= datetime.now(tz=SG_TZ):
                clear_booking_context(customer_number)
                return None
            return {
                "pending_service_key": row[0],
                "pending_service_label": row[1],
                "pending_start_local": row[2],
            }
    finally:
        conn.close()


def clear_booking_context(customer_number: str) -> None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM booking_context WHERE customer_number = %s", (customer_number,))
    finally:
        conn.close()
