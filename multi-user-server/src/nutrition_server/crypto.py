"""Sealing saved logins so they are unreadable at rest.

The secret key comes from the ENCRYPTION_KEY environment variable (set in
Railway, never in code). Whoever holds that key can open the box, which is
why the brief asks Brandon to tell people plainly.
"""

import json
import os

from cryptography.fernet import Fernet


class EncryptionNotConfigured(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        raise EncryptionNotConfigured("ENCRYPTION_KEY is not set.")
    return Fernet(key.encode())


def new_encryption_key() -> str:
    """Generate a value for ENCRYPTION_KEY (run once, store in Railway)."""
    return Fernet.generate_key().decode()


def seal(data: dict) -> bytes:
    return _fernet().encrypt(json.dumps(data).encode())


def unseal(blob: bytes) -> dict:
    return json.loads(_fernet().decrypt(blob))
