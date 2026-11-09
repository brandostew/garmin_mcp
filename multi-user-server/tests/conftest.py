import socket
import threading
import time

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from nutrition_server import db
from nutrition_server.security import hash_secret, new_user_key
from nutrition_server.server import build_app


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


@pytest.fixture
def server(database):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=port,
                            access_log=False, log_level="info")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    thread.join(timeout=5)


async def call(url, tool, args=None, key=None, key_in_link=False):
    """Call one tool. Returns (is_error, text)."""
    headers = {}
    if key and key_in_link:
        url = f"{url}?key={key}"
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args or {})
            text = result.content[0].text if result.content else ""
            return result.isError, text
