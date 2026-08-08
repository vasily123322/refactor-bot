from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


_ENCRYPTED_PREFIX = "enc:v1:"
_KEY_ENV = "DB_SECRET_KEY"
_KEY_MIN_LENGTH = 32
_KEY_DOMAIN = b"refactor-bot:db-secret:v1\0"


class SecretKeyNotConfigured(RuntimeError):
    pass


class SecretDecryptionError(RuntimeError):
    pass


def is_encrypted_secret(value: str | None) -> bool:
    return bool(value and value.startswith(_ENCRYPTED_PREFIX))


def database_secret_key_available() -> bool:
    value = (os.getenv(_KEY_ENV) or "").strip()
    return len(value) >= _KEY_MIN_LENGTH


def _fernet() -> Fernet:
    raw = (os.getenv(_KEY_ENV) or "").strip()
    if len(raw) < _KEY_MIN_LENGTH:
        raise SecretKeyNotConfigured(
            f"{_KEY_ENV} must be configured with at least {_KEY_MIN_LENGTH} characters"
        )
    digest = hashlib.sha256(_KEY_DOMAIN + raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    if not value:
        return value
    if is_encrypted_secret(value):
        return value
    encrypted = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return _ENCRYPTED_PREFIX + encrypted


def decrypt_secret(value: str) -> str:
    if not value or not is_encrypted_secret(value):
        # Backward compatibility for rows created before encryption-at-rest.
        return value
    token = value[len(_ENCRYPTED_PREFIX) :]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except SecretKeyNotConfigured:
        raise
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise SecretDecryptionError("database secret cannot be decrypted") from exc
