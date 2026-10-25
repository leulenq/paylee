"""Individual operations accounts, scrypt passwords, TOTP MFA, and revocable sessions."""

import argparse
import base64
import getpass
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException

from . import db, vault
from .config import get_settings

COOKIE = "paylee_staff"
ROLES = {"reviewer", "payments", "treasury", "administrator"}


def password_hash(password: str) -> str:
    if len(password) < 14:
        raise ValueError("Staff passwords must contain at least 14 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def totp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def authenticate(conn, username: str, password: str, code: str):
    row = conn.execute("select * from staff where username = %s for update", (username,)).fetchone()
    # Also do scrypt work for unknown usernames.
    encoded = row["password_hash"] if row else password_hash("invalid-account-password")
    valid = password_matches(password, encoded)
    if not row or not valid or row["disabled_at"]:
        raise HTTPException(401, "Invalid staff credentials")
    if row["totp_ciphertext"]:
        secret = vault.decrypt(row["totp_ciphertext"])
        counter = int(time.time()) // 30
        match = next((n for n in (counter - 1, counter, counter + 1)
                      if n > row["last_totp_counter"] and hmac.compare_digest(totp(secret, n), code)), None)
        if match is None:
            raise HTTPException(401, "Invalid staff credentials")
        conn.execute("update staff set last_totp_counter = %s where id = %s", (match, row["id"]))
    elif get_settings().environment == "production":
        raise HTTPException(401, "Staff MFA enrollment is required")
    return row


def issue_token(actor):
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": str(actor["id"]), "aud": "paylee-operations", "iat": now,
        "exp": now + timedelta(hours=get_settings().staff_session_hours),
        "version": actor["session_version"],
    }, get_settings().session_secret, algorithm="HS256")


def session_actor(request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        claims = jwt.decode(token, get_settings().session_secret, algorithms=["HS256"], audience="paylee-operations")
        with db.transaction() as conn:
            row = conn.execute("select * from staff where id = %s and disabled_at is null", (claims["sub"],)).fetchone()
        if row and row["session_version"] == claims["version"]:
            return row
    except (jwt.InvalidTokenError, ValueError, KeyError):
        pass
    return None


def require_role(actor, *roles):
    # Administrator is deliberately NOT a payment/reviewer/treasury superuser.
    if not actor or actor.get("id") is None or not set(actor["roles"]).intersection(roles):
        raise HTTPException(403, "An individual staff account with the required role is needed")


def main():
    parser = argparse.ArgumentParser(description="Provision an individual Paylee operator; no public enrollment")
    parser.add_argument("username")
    parser.add_argument("--roles", required=True, help="Comma-separated reviewer,payments,treasury,administrator")
    parser.add_argument("--revoke-sessions", action="store_true")
    args = parser.parse_args()
    roles = args.roles.split(",")
    if not set(roles) <= ROLES or not roles:
        parser.error("Invalid roles")
    settings = get_settings()
    db.open_pool(settings.database_url)
    try:
        with db.transaction() as conn:
            db.init_schema(conn)
            if args.revoke_sessions:
                conn.execute("update staff set session_version = session_version + 1 where username = %s", (args.username,))
                print("Sessions revoked.")
                return
            password = getpass.getpass("Staff password (14+ characters): ")
            secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
            row = conn.execute(
                "insert into staff (username, password_hash, roles, totp_ciphertext) values (%s, %s, %s, %s) returning id",
                (args.username, password_hash(password), roles, vault.encrypt(secret)),
            ).fetchone()
            conn.execute("insert into audit_events (actor_id, action, object_id) values (%s, 'staff.provisioned', %s)",
                         (row["id"], str(row["id"])))
        print(f"Enroll in an authenticator now. TOTP secret (shown once): {secret}")
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
