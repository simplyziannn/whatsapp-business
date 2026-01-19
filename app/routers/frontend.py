from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse

router = APIRouter()

# -----------------------------
# Frontend redirect
# -----------------------------
@router.get("/")
async def root():
    return RedirectResponse(url="/frontend/index.html")

