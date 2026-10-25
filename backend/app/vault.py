"""Application encryption for account identifiers, OCR output, and staff MFA secrets."""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet

from .config import get_settings


def _cipher() -> Fernet:
    settings = get_settings()
    key = settings.data_encryption_key
    if not key:
        if settings.environment == "production":
            raise RuntimeError("Missing encryption key")
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.session_secret.encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _cipher().decrypt(value.encode()).decode()


def encrypt_bytes(value: bytes) -> bytes:
    return _cipher().encrypt(value)


def decrypt_bytes(value: bytes) -> bytes:
    return _cipher().decrypt(value)


def fingerprint(*parts: str) -> str:
    settings = get_settings()
    key = settings.account_fingerprint_key or settings.session_secret
    # Length-prefix avoids delimiter collisions. Case and punctuation normalization is biller-specific.
    payload = "".join(f"{len(p)}:{p}" for p in parts)
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
