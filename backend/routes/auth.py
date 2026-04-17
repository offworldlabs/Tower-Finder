"""OAuth2 authentication routes — Google & GitHub SSO."""

import logging
import os
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from core.auth import _ANONYMOUS_USER, AUTH_ENABLED, create_token, get_current_user, get_or_create_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")


def _fix_scheme(url: str) -> str:
    """Ensure HTTPS when behind a reverse proxy."""
    if os.getenv("FORCE_HTTPS", "true").lower() == "true":
        return url.replace("http://", "https://", 1)
    return url


# ── Google OAuth ──────────────────────────────────────────────────────────────

@router.get("/login/google")
async def login_google(request: Request, redirect: str = "/"):
    callback = _fix_scheme(str(request.url_for("callback_google")))
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": callback,
        "response_type": "code",
        "scope": "openid email profile",
        "state": redirect,
        "prompt": "select_account",
    }
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


@router.get("/callback/google", name="callback_google")
async def callback_google(request: Request, code: str = "", state: str = "/"):
    callback = _fix_scheme(str(request.url_for("callback_google")))
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "redirect_uri": callback,
                "grant_type": "authorization_code",
            },
        )
        if tok.status_code != 200:
            logger.error("Google token exchange failed: %s", tok.text)
            return RedirectResponse("/login?error=google_token_failed")
        tokens = tok.json()

        info = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        userinfo = info.json()

    user = get_or_create_user(
        email=userinfo["email"],
        name=userinfo.get("name", ""),
        avatar=userinfo.get("picture", ""),
        provider="google",
    )
    token = create_token(user)
    # Validate redirect to prevent open-redirect attacks
    safe_redirect = state if state and state.startswith("/") and not state.startswith("//") else "/"
    response = RedirectResponse(safe_redirect)
    response.set_cookie(
        "auth_token", token,
        httponly=True, secure=True, samesite="lax",
        max_age=86400 * 7, path="/",
    )
    return response


# ── GitHub OAuth ──────────────────────────────────────────────────────────────

@router.get("/login/github")
async def login_github(request: Request, redirect: str = "/"):
    callback = _fix_scheme(str(request.url_for("callback_github")))
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": callback,
        "scope": "read:user user:email",
        "state": redirect,
    }
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{urlencode(params)}")


@router.get("/callback/github", name="callback_github")
async def callback_github(request: Request, code: str = "", state: str = "/"):
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        if tok.status_code != 200:
            logger.error("GitHub token exchange failed: %s", tok.text)
            return RedirectResponse("/login?error=github_token_failed")
        tokens = tok.json()
        access_token = tokens.get("access_token", "")

        # Get user profile
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        profile = user_resp.json()

        # Get primary email (may be private)
        email = profile.get("email")
        if not email:
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            for e in emails_resp.json():
                if e.get("primary"):
                    email = e["email"]
                    break

    if not email:
        return RedirectResponse("/login?error=no_email")

    user = get_or_create_user(
        email=email,
        name=profile.get("name") or profile.get("login", ""),
        avatar=profile.get("avatar_url", ""),
        provider="github",
    )
    token = create_token(user)
    # Validate redirect to prevent open-redirect attacks
    safe_redirect = state if state and state.startswith("/") and not state.startswith("//") else "/"
    response = RedirectResponse(safe_redirect)
    response.set_cookie(
        "auth_token", token,
        httponly=True, secure=True, samesite="lax",
        max_age=86400 * 7, path="/",
    )
    return response


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
