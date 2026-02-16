from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.db import tenants_repo
from app.services.auth import require_user, get_current_user


router = APIRouter(prefix="/api", tags=["auth-api"])


@router.post("/auth/login")
def auth_login(payload: dict):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")

    user = tenants_repo.verify_user_credentials(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token, expires_ts = tenants_repo.create_session(user_id=user["id"], hours=12)
    return {
        "ok": True,
        "token": token,
        "expires_ts": expires_ts,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "company_id": user["company_id"],
            "company_name": user["company_name"],
        },
    }


@router.post("/auth/logout")
def auth_logout(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": True}
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token:
            tenants_repo.delete_session(token)
    return {"ok": True}


@router.get("/auth/me")
def auth_me(request: Request):
    user = require_user(request)
    return {"ok": True, "user": user}


@router.post("/signup_requests")
def create_signup_request(payload: dict):
    full_name = (payload.get("full_name") or "").strip()
    work_email = (payload.get("work_email") or "").strip()
    company_name = (payload.get("company_name") or "").strip()
    whatsapp_number = (payload.get("whatsapp_number") or "").strip() or None
    automation_needs = (payload.get("automation_needs") or "").strip() or None

    if not full_name or not work_email or not company_name:
        raise HTTPException(status_code=400, detail="full_name, work_email, and company_name are required")

    req_id = tenants_repo.create_signup_request(
        full_name=full_name,
        work_email=work_email,
        company_name=company_name,
        whatsapp_number=whatsapp_number,
        automation_needs=automation_needs,
    )
    return {"ok": True, "id": req_id}


@router.get("/admin/signup_requests")
def admin_list_signup_requests(request: Request, status: str = "all", limit: int = 100):
    require_user(request, roles=["platform_admin"])
    allowed = {"all", "new", "contacted", "approved", "rejected"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use one of: {sorted(allowed)}")
    limit = max(1, min(limit, 300))
    return {"items": tenants_repo.list_signup_requests(status=status, limit=limit)}


@router.post("/admin/signup_requests/{request_id}/status")
def admin_update_signup_request(request: Request, request_id: int, payload: dict):
    require_user(request, roles=["platform_admin"])
    status = (payload.get("status") or "").strip()
    note = (payload.get("note") or "").strip() or None
    allowed = {"new", "contacted", "approved", "rejected"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use one of: {sorted(allowed)}")
    ok = tenants_repo.set_signup_request_status(request_id=request_id, status=status, note=note)
    if not ok:
        raise HTTPException(status_code=404, detail="signup request not found")
    return {"ok": True}


@router.post("/admin/accounts")
def admin_create_account(request: Request, payload: dict):
    require_user(request, roles=["platform_admin"])
    company_name = (payload.get("company_name") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    whatsapp_phone_number_id = (payload.get("whatsapp_phone_number_id") or "").strip() or None

    if not company_name or not username or not password:
        raise HTTPException(status_code=400, detail="company_name, username, and password are required")

    try:
        created = tenants_repo.create_company_with_user(
            company_name=company_name,
            username=username,
            password=password,
            whatsapp_phone_number_id=whatsapp_phone_number_id,
        )
        return {"ok": True, **created}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/admin/companies")
def admin_list_companies(request: Request, limit: int = 200):
    require_user(request, roles=["platform_admin"])
    limit = max(1, min(limit, 500))
    return {"items": tenants_repo.list_companies(limit=limit)}


@router.delete("/admin/companies/{company_id}")
def admin_delete_company(request: Request, company_id: int):
    require_user(request, roles=["platform_admin"])
    outcome = tenants_repo.delete_company(company_id)
    if not outcome.get("ok"):
        reason = outcome.get("reason")
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="Company not found")
        if reason == "protected_default":
            raise HTTPException(status_code=400, detail="Default company cannot be deleted")
        raise HTTPException(status_code=400, detail=f"Delete failed: {reason}")
    return outcome
