import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db, ratelimit, staff
from .config import get_settings

APPLE_ISSUER = "https://appleid.apple.com"
_apple_keys = jwt.PyJWKClient("https://appleid.apple.com/auth/keys", cache_keys=True)


def issue_session_token(user_id) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user_id), "iat": now, "exp": now + timedelta(days=settings.session_days)}
    return jwt.encode(claims, settings.session_secret, algorithm="HS256")


def verify_apple_identity_token(identity_token: str) -> str:
    """Returns Apple's stable user id ("sub") if the token is genuine and was issued for this app."""
    signing_key = _apple_keys.get_signing_key_from_jwt(identity_token)
    claims = jwt.decode(
        identity_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=get_settings().apple_bundle_id,
        issuer=APPLE_ISSUER,
    )
    return claims["sub"]


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in again", headers={"WWW-Authenticate": "Bearer"})


def current_user(authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorized()
    try:
        claims = jwt.decode(
            authorization.removeprefix("Bearer "), get_settings().session_secret, algorithms=["HS256"]
        )
    except jwt.InvalidTokenError:
        raise _unauthorized() from None
    with db.transaction() as conn:
        user = conn.execute(
            "select id, handle, created_at from users where id = %s and deleted_at is null",
            (claims["sub"],),
        ).fetchone()
    if user is None:
        raise _unauthorized()
    return user


CurrentUser = Annotated[dict, Depends(current_user)]

_basic = HTTPBasic(realm="Paylee admin", auto_error=False)


async def require_admin(request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)]) -> dict:
    client = request.client.host if request.client else "unknown"
    if ratelimit.admin_failures.is_blocked(client):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many wrong passwords. Try again later.")
    actor = staff.session_actor(request)
    if actor is None and get_settings().environment != "production" and credentials:
        expected = get_settings().admin_password.encode()
        if credentials.username == "admin" and secrets.compare_digest(credentials.password.encode(), expected):
            actor = {"id": None, "username": "development admin", "roles": []}
    if actor is None:
        ratelimit.admin_failures.hit(client)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Sign in at /admin/login", headers={"WWW-Authenticate": "Basic"}
        )
    request.state.staff = actor
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        token = request.headers.get("x-csrf-token")
        if not token and request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            token = (await request.form()).get("csrf", "")
        if not token or not hmac.compare_digest(str(token), admin_csrf_token(actor["id"])):
            raise HTTPException(403, "Stale form, reload the operations page")
    return actor


# Browsers resend Basic auth automatically, so admin forms also carry a token another site can't know.
def admin_csrf_token(actor_id=None) -> str:
    scope = f"staff-forms:{actor_id}".encode() if actor_id else b"admin-forms"
    return hmac.new(get_settings().session_secret.encode(), scope, hashlib.sha256).hexdigest()


def check_admin_csrf(token: str) -> None:
    if not hmac.compare_digest(token, admin_csrf_token()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Stale form, reload the admin page")
