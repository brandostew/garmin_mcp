"""Random keys and codes, and how they are stored (only ever as a hash)."""

import hashlib
import hmac
import secrets


def new_user_key() -> str:
    """A long random key that identifies one person to the server."""
    return "nk_" + secrets.token_urlsafe(32)


def new_invite_code() -> str:
    return secrets.token_urlsafe(24)


def hash_secret(value: str) -> str:
    """SHA-256 is enough here: keys and invite codes are long and random,
    so they cannot be guessed. Passwords people choose get a slow hash later."""
    return hashlib.sha256(value.encode()).hexdigest()


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


# Passwords people choose for this service: slow, salted scrypt.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_hex, digest_hex = stored.split("$")
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)
