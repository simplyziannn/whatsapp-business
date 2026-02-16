from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .conn import db_conn


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return digest.hex()


def _make_password_record(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = _hash_password(password, salt)
    return f"pbkdf2_sha256${salt}${hashed}"


def _check_password(password: str, password_hash: str) -> bool:
    try:
        algo, salt, hashed = password_hash.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    test = _hash_password(password, salt)
    return hmac.compare_digest(test, hashed)


def db_init_tenants() -> None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    whatsapp_phone_number_id TEXT UNIQUE,
                    created_ts TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('platform_admin','company_user')),
                    company_id INTEGER REFERENCES companies(id),
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_ts TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_ts TIMESTAMPTZ NOT NULL,
                    expires_ts TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS signup_requests (
                    id SERIAL PRIMARY KEY,
                    created_ts TIMESTAMPTZ NOT NULL,
                    full_name TEXT NOT NULL,
                    work_email TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    whatsapp_number TEXT,
                    automation_needs TEXT,
                    status TEXT NOT NULL CHECK (status IN ('new','contacted','approved','rejected')) DEFAULT 'new',
                    note TEXT
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_company_id ON users(company_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions(expires_ts);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_signup_requests_status ON signup_requests(status, created_ts DESC);")

            # Seed default company
            default_company_name = os.getenv("DEFAULT_COMPANY_NAME", "AutoSpritze")
            default_phone_id = os.getenv("META_PHONE_NUMBER_ID")
            if not default_phone_id:
                try:
                    from app.config import settings as app_settings

                    default_phone_id = app_settings.PHONE_NUMBER_ID
                except Exception:
                    default_phone_id = None
            cur.execute(
                """
                INSERT INTO companies (name, whatsapp_phone_number_id, created_ts)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                SET whatsapp_phone_number_id = COALESCE(EXCLUDED.whatsapp_phone_number_id, companies.whatsapp_phone_number_id)
                RETURNING id
                """,
                (default_company_name, default_phone_id, _utcnow()),
            )
            company_id = int(cur.fetchone()[0])

            # Seed company user
            default_username = os.getenv("DEFAULT_COMPANY_USERNAME", "autospritze")
            default_password = os.getenv("DEFAULT_COMPANY_PASSWORD", os.getenv("ADMIN_DASH_TOKEN", "super-secret-string"))
            cur.execute("SELECT id, password_hash FROM users WHERE username = %s", (default_username,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, role, company_id, active, created_ts)
                    VALUES (%s, %s, 'company_user', %s, TRUE, %s)
                    """,
                    (default_username, _make_password_record(default_password), company_id, _utcnow()),
                )

            # Seed platform admin
            admin_username = os.getenv("PLATFORM_ADMIN_USERNAME", "platform-admin")
            admin_password = os.getenv("PLATFORM_ADMIN_PASSWORD", "super-secret-admin")
            cur.execute("SELECT id FROM users WHERE username = %s", (admin_username,))
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, role, company_id, active, created_ts)
                    VALUES (%s, %s, 'platform_admin', NULL, TRUE, %s)
                    """,
                    (admin_username, _make_password_record(admin_password), _utcnow()),
                )
    finally:
        conn.close()


def resolve_company_id_by_phone_id(whatsapp_phone_number_id: str | None) -> Optional[int]:
    if not whatsapp_phone_number_id:
        return None
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM companies WHERE whatsapp_phone_number_id = %s", (whatsapp_phone_number_id,))
            row = cur.fetchone()
            return int(row[0]) if row else None
    finally:
        conn.close()


def get_default_company_id() -> Optional[int]:
    default_company_name = os.getenv("DEFAULT_COMPANY_NAME", "AutoSpritze")
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM companies WHERE name = %s", (default_company_name,))
            row = cur.fetchone()
            return int(row[0]) if row else None
    finally:
        conn.close()


def verify_user_credentials(username: str, password: str) -> Optional[dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.password_hash, u.role, u.company_id, u.active, c.name
                FROM users u
                LEFT JOIN companies c ON c.id = u.company_id
                WHERE u.username = %s
                """,
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return None
            if not row[5]:
                return None
            if not _check_password(password, row[2]):
                return None
            return {
                "id": int(row[0]),
                "username": row[1],
                "role": row[3],
                "company_id": row[4],
                "company_name": row[6],
            }
    finally:
        conn.close()


def create_session(user_id: int, hours: int = 12) -> tuple[str, str]:
    token = secrets.token_urlsafe(40)
    now = _utcnow()
    expires = now + timedelta(hours=hours)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_sessions (token, user_id, created_ts, expires_ts)
                VALUES (%s, %s, %s, %s)
                """,
                (token, user_id, now, expires),
            )
    finally:
        conn.close()
    return token, expires.isoformat()


def get_session_user(token: str) -> Optional[dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.role, u.company_id, u.active, c.name, s.expires_ts
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                LEFT JOIN companies c ON c.id = u.company_id
                WHERE s.token = %s
                """,
                (token,),
            )
            row = cur.fetchone()
            if not row:
                return None
            expires_ts = row[6]
            if expires_ts <= _utcnow():
                return None
            if not row[4]:
                return None
            return {
                "id": int(row[0]),
                "username": row[1],
                "role": row[2],
                "company_id": row[3],
                "company_name": row[5],
                "expires_ts": expires_ts.isoformat(),
            }
    finally:
        conn.close()


def delete_session(token: str) -> None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_sessions WHERE token = %s", (token,))
    finally:
        conn.close()


def cleanup_expired_sessions() -> int:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_sessions WHERE expires_ts <= %s", (_utcnow(),))
            return cur.rowcount
    finally:
        conn.close()


def create_signup_request(
    full_name: str,
    work_email: str,
    company_name: str,
    whatsapp_number: str | None,
    automation_needs: str | None,
) -> int:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signup_requests
                (created_ts, full_name, work_email, company_name, whatsapp_number, automation_needs, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'new')
                RETURNING id
                """,
                (_utcnow(), full_name, work_email, company_name, whatsapp_number, automation_needs),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def list_signup_requests(status: str = "all", limit: int = 100) -> list[dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            if status == "all":
                cur.execute(
                    """
                    SELECT id, created_ts, full_name, work_email, company_name, whatsapp_number, automation_needs, status, note
                    FROM signup_requests
                    ORDER BY created_ts DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, created_ts, full_name, work_email, company_name, whatsapp_number, automation_needs, status, note
                    FROM signup_requests
                    WHERE status = %s
                    ORDER BY created_ts DESC
                    LIMIT %s
                    """,
                    (status, limit),
                )
            rows = cur.fetchall()
            return [
                {
                    "id": int(r[0]),
                    "created_ts": r[1].isoformat(),
                    "full_name": r[2],
                    "work_email": r[3],
                    "company_name": r[4],
                    "whatsapp_number": r[5],
                    "automation_needs": r[6],
                    "status": r[7],
                    "note": r[8],
                }
                for r in rows
            ]
    finally:
        conn.close()


def set_signup_request_status(request_id: int, status: str, note: str | None = None) -> bool:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE signup_requests
                SET status = %s, note = %s
                WHERE id = %s
                """,
                (status, note, request_id),
            )
            return cur.rowcount == 1
    finally:
        conn.close()


def create_company_with_user(
    company_name: str,
    username: str,
    password: str,
    whatsapp_phone_number_id: str | None = None,
) -> dict[str, Any]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies (name, whatsapp_phone_number_id, created_ts)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (company_name, whatsapp_phone_number_id, _utcnow()),
            )
            company_id = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO users (username, password_hash, role, company_id, active, created_ts)
                VALUES (%s, %s, 'company_user', %s, TRUE, %s)
                RETURNING id
                """,
                (username, _make_password_record(password), company_id, _utcnow()),
            )
            user_id = int(cur.fetchone()[0])
            return {"company_id": company_id, "user_id": user_id}
    finally:
        conn.close()


def list_companies(limit: int = 200) -> list[dict[str, Any]]:
    default_company_name = os.getenv("DEFAULT_COMPANY_NAME", "AutoSpritze")
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, whatsapp_phone_number_id, created_ts
                FROM companies
                ORDER BY created_ts DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": int(r[0]),
                    "name": r[1],
                    "whatsapp_phone_number_id": r[2],
                    "created_ts": r[3].isoformat(),
                    "is_default": r[1] == default_company_name,
                }
                for r in rows
            ]
    finally:
        conn.close()


def delete_company(company_id: int) -> dict[str, Any]:
    default_company_id = get_default_company_id()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM companies WHERE id = %s", (company_id,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "reason": "not_found"}

            if default_company_id is not None and int(row[0]) == int(default_company_id):
                return {"ok": False, "reason": "protected_default"}

            cur.execute("SELECT COUNT(*) FROM users WHERE company_id = %s", (company_id,))
            users_count = int(cur.fetchone()[0] or 0)

            # Keep historical records but detach ownership.
            cur.execute("UPDATE messages SET company_id = NULL WHERE company_id = %s", (company_id,))
            messages_detached = int(cur.rowcount or 0)
            cur.execute("UPDATE booking_requests SET company_id = NULL WHERE company_id = %s", (company_id,))
            bookings_detached = int(cur.rowcount or 0)

            # Remove user sessions and users for this tenant.
            cur.execute(
                """
                DELETE FROM user_sessions
                WHERE user_id IN (SELECT id FROM users WHERE company_id = %s)
                """,
                (company_id,),
            )
            sessions_deleted = int(cur.rowcount or 0)

            cur.execute("DELETE FROM users WHERE company_id = %s", (company_id,))
            users_deleted = int(cur.rowcount or 0)

            cur.execute("DELETE FROM companies WHERE id = %s", (company_id,))
            company_deleted = int(cur.rowcount or 0)

            if company_deleted != 1:
                return {"ok": False, "reason": "delete_failed"}

            return {
                "ok": True,
                "company_id": company_id,
                "users_count_before": users_count,
                "users_deleted": users_deleted,
                "sessions_deleted": sessions_deleted,
                "messages_detached": messages_detached,
                "bookings_detached": bookings_detached,
            }
    finally:
        conn.close()


def backfill_tenant_links() -> None:
    """
    Attach legacy rows to companies.
    1) by matching meta_phone_number_id <-> companies.whatsapp_phone_number_id
    2) fallback remaining NULL company rows to default company
    """
    default_company_id = get_default_company_id()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            # messages by phone id match
            cur.execute(
                """
                UPDATE messages m
                SET company_id = c.id
                FROM companies c
                WHERE m.company_id IS NULL
                  AND m.meta_phone_number_id IS NOT NULL
                  AND c.whatsapp_phone_number_id = m.meta_phone_number_id
                """
            )
            # bookings by phone id match
            cur.execute(
                """
                UPDATE booking_requests br
                SET company_id = c.id
                FROM companies c
                WHERE br.company_id IS NULL
                  AND br.meta_phone_number_id IS NOT NULL
                  AND c.whatsapp_phone_number_id = br.meta_phone_number_id
                """
            )
            if default_company_id is not None:
                cur.execute("UPDATE messages SET company_id = %s WHERE company_id IS NULL", (default_company_id,))
                cur.execute("UPDATE booking_requests SET company_id = %s WHERE company_id IS NULL", (default_company_id,))
    finally:
        conn.close()
