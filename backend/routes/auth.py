"""Authentication session routes — /me and /logout."""

import logging

from fastapi import APIRouter, Request, Response

from core.auth import _ANONYMOUS_USER, AUTH_ENABLED, get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Session endpoints ─────────────────────────────────────────────────────────

@router.get("/me")
async def me(request: Request):
    """Return current user info (or 401). When AUTH_ENABLED=False, returns anonymous admin."""
    if not AUTH_ENABLED:
        return {**_ANONYMOUS_USER, "auth_enabled": False}
    user = await get_current_user(request)
    return {**{k: v for k, v in user.items()}, "auth_enabled": True}


@router.post("/logout")
async def logout():
    response = Response(content='{"ok":true}', media_type="application/json")
    response.delete_cookie("auth_token", path="/")
    return response
