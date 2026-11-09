import pytest

from nutrition_server import db
from nutrition_server.security import hash_secret, new_user_key


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A fresh, empty database file for each test."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    yield


def make_user(name: str) -> tuple[int, str]:
    """Create a user directly (the website will do this later). Returns (id, key)."""
    key = new_user_key()
    with db.open_session() as s:
        user = db.User(display_name=name, key_hash=hash_secret(key))
        s.add(user)
        s.commit()
        return user.id, key
