from __future__ import annotations

import os
from typing import Optional

from fastapi import HTTPException, Request

from app.db import tenants_repo


def _token_from_auth_header(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    return token or None


def get_current_user(request: Request) -> Optional[dict]:
    token = _token_from_auth_header(request)
    if token:
        user = tenants_repo.get_session_user(token)
        if user:
            user["auth_mode"] = "session"
            return user

    # Backward-compatible legacy admin token header.
    legacy = request.headers.get("X-Admin-Token")
    legacy_expected = os.getenv("ADMIN_DASH_TOKEN")
    if legacy and legacy_expected and legacy == legacy_expected:
        return {
            "id": 0,
            "username": "legacy-admin",
            "role": "platform_admin",
            "company_id": None,
            "company_name": None,
            "auth_mode": "legacy",
        }
    return None


def require_user(request: Request, roles: list[str] | None = None) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if roles and user["role"] not in roles:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user

