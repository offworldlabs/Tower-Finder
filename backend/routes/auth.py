"""OAuth2 authentication routes — Google & GitHub SSO.

The OAuth flow is implemented here (custom routes keep the URL paths stable
so the frontend needs no changes). JWT issuance and cookie management are
fully delegated to fastapi-users' JWTStrategy + CookieTransport.
"""

import hashlib
import hmac as _hmac
import logging
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from core import state
from core.auth import (
    consume_invite_for_email,
    create_claim_code,
    get_user_nodes,
    list_claim_codes,
    revoke_claim_code,
)
from core.users import (
    ANONYMOUS_USER,
    AUTH_ENABLED,
    JWT_LIFETIME_SECONDS,
    JWT_SECRET,
    get_current_user,
    get_jwt_strategy,
    get_or_create_oauth_user,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")


def _fix_scheme(url: str) -> str:
    if os.getenv("FORCE_HTTPS", "true").lower() == "true":
        return url.replace("http://", "https://", 1)
    return url


def _safe_redirect(state_param: str) -> str:
    """Validate the redirect target to prevent open-redirect attacks."""
    if state_param and state_param.startswith("/") and not state_param.startswith("//"):
        return state_param
    return "/"


def _make_oauth_state(redirect: str) -> str:
    """Return an HMAC-signed state token: {nonce}:{sig}:{redirect}."""
    nonce = secrets.token_urlsafe(16)
    msg = f"{nonce}:{redirect}".encode()
    sig = _hmac.new(JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"{nonce}:{sig}:{redirect}"


def _verify_oauth_state(state: str) -> str | None:
    """Verify HMAC-signed OAuth state. Returns safe redirect URL or None on failure."""
    parts = state.split(":", 2)
    if len(parts) != 3:
        return None
    nonce, sig, redirect = parts
    msg = f"{nonce}:{redirect}".encode()
    expected = _hmac.new(JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(expected, sig):
        return None
    return _safe_redirect(redirect)


async def _set_auth_cookie(response: Response, user) -> None:
    """Write the fastapi-users JWT into the auth_token cookie."""
    strategy = get_jwt_strategy()
    token = await strategy.write_token(user)
    response.set_cookie(
        "auth_token",
        token,
        max_age=JWT_LIFETIME_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


# ── Google OAuth ──────────────────────────────────────────────────────────────

@router.get("/login/google")
async def login_google(request: Request, redirect: str = "/"):
    callback = _fix_scheme(str(request.url_for("callback_google")))
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": callback,
        "response_type": "code",
        "scope": "openid email profile",
        "state": _make_oauth_state(redirect),
        "prompt": "select_account",
    }
    return RedirectResponse(
        f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    )


@router.get("/callback/google", name="callback_google")
async def callback_google(request: Request, code: str = "", state: str = ""):
    redirect_url = _verify_oauth_state(state)
    if redirect_url is None:
        return RedirectResponse("/login?error=invalid_state")
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

    email = userinfo.get("email")
    if not email:
        return RedirectResponse("/login?error=no_email")

    user = await get_or_create_oauth_user(
        email=email,
        name=userinfo.get("name", ""),
        avatar=userinfo.get("picture", ""),
        provider="google",
        consume_invite_fn=consume_invite_for_email,
    )
    response = RedirectResponse(redirect_url)
    await _set_auth_cookie(response, user)
    return response


# ── GitHub OAuth ──────────────────────────────────────────────────────────────

@router.get("/login/github")
async def login_github(request: Request, redirect: str = "/"):
    callback = _fix_scheme(str(request.url_for("callback_github")))
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": callback,
        "scope": "read:user user:email",
        "state": _make_oauth_state(redirect),
    }
    return RedirectResponse(
        f"https://github.com/login/oauth/authorize?{urlencode(params)}"
    )


@router.get("/callback/github", name="callback_github")
async def callback_github(request: Request, code: str = "", state: str = ""):
    redirect_url = _verify_oauth_state(state)
    if redirect_url is None:
        return RedirectResponse("/login?error=invalid_state")
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

        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        profile = user_resp.json()

        email = profile.get("email")
        if not email:
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if emails_resp.status_code == 200:
                for e in emails_resp.json():
                    if e.get("primary"):
                        email = e["email"]
                        break

    if not email:
        return RedirectResponse("/login?error=no_email")

    user = await get_or_create_oauth_user(
        email=email,
        name=profile.get("name") or profile.get("login", ""),
        avatar=profile.get("avatar_url", ""),
        provider="github",
        consume_invite_fn=consume_invite_for_email,
    )
    response = RedirectResponse(redirect_url)
    await _set_auth_cookie(response, user)
    return response


# ── Session endpoints ─────────────────────────────────────────────────────────

@router.get("/me")
async def me(request: Request):
    if not AUTH_ENABLED:
        return {**ANONYMOUS_USER, "auth_enabled": False}
    user_dict = await get_current_user(request)
    return {**user_dict, "auth_enabled": True}


@router.post("/logout")
async def logout():
    response = Response(content='{"ok":true}', media_type="application/json")
    response.delete_cookie("auth_token", path="/")
    return response


# ── Node ownership self-service ───────────────────────────────────────────────

@router.get("/me/nodes")
async def my_nodes(request: Request):
    user = await get_current_user(request)
    node_ids = get_user_nodes(user["id"])
    out = []
    with state.connected_nodes_lock:
        snapshot = {nid: dict(state.connected_nodes.get(nid, {})) for nid in node_ids}
    for nid in node_ids:
        info = snapshot.get(nid) or {}
        cfg = info.get("config", {}) or {}
        out.append({
            "node_id": nid,
            "name": cfg.get("name", nid),
            "status": info.get("status", "never_connected"),
            "last_heartbeat": info.get("last_heartbeat"),
            "is_synthetic": info.get("is_synthetic", False),
            "rx_lat": cfg.get("rx_lat"),
            "rx_lon": cfg.get("rx_lon"),
            "frequency": cfg.get("FC", cfg.get("frequency")),
        })
    return out


@router.get("/me/claim-codes")
async def my_claim_codes(request: Request):
    user = await get_current_user(request)
    codes = list_claim_codes(user["id"])
    codes.sort(key=lambda c: c.get("created_at", 0), reverse=True)
    return codes


@router.post("/me/claim-codes")
async def create_my_claim_code(request: Request):
    user = await get_current_user(request)
    try:
        return create_claim_code(user["id"])
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e


@router.delete("/me/claim-codes/{code}")
async def revoke_my_claim_code(code: str, request: Request):
    user = await get_current_user(request)
    if not revoke_claim_code(code, user["id"]):
        raise HTTPException(404, "Code not found, already used, or not yours")
    return {"ok": True}
