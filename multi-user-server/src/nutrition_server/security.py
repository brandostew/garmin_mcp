"""Random keys and codes, and how they are stored (only ever as a hash)."""

import hashlib
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
