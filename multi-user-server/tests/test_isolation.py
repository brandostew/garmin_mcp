"""Step 2 checkpoint: the key check, whoami, and keeping two people apart.

A real copy of the server runs on this machine for each test, and a real
MCP client talks to it over HTTP, the same way Claude will.
"""

import asyncio
import logging
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from nutrition_server import admin, db
from nutrition_server.server import build_app

from .conftest import make_user


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


# 1. Each key returns only its own data.
async def test_each_key_sees_only_its_own_name(server):
    _, alex_key = make_user("Alex")
    _, bo_key = make_user("Bo")
    assert await call(server, "whoami", key=alex_key) == (False, "Alex")
    assert await call(server, "whoami", key=bo_key) == (False, "Bo")


async def test_key_inside_connector_link_works(server):
    _, key = make_user("Alex")
    assert await call(server, "whoami", key=key, key_in_link=True) == (False, "Alex")


# 2. Missing, wrong or disabled key: error, no data.
@pytest.mark.parametrize("case", ["missing", "wrong", "disabled", "rotated"])
async def test_bad_keys_get_nothing(server, case):
    uid, key = make_user("Alex")
    if case == "missing":
        key = None
    elif case == "wrong":
        key = "nk_" + "x" * 43
    elif case == "disabled":
        admin.disable_user(uid)
    elif case == "rotated":
        admin.rotate_key(uid)
    headers = {"Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}}}
    async with httpx.AsyncClient() as client:
        resp = await client.post(server, json=body, headers=headers)
    assert resp.status_code == 401
    assert "Alex" not in resp.text


# 3. Extra user id / email / name arguments cannot change who you are.
@pytest.mark.parametrize("extra", [
    {"user_id": 1}, {"user": "Bo"}, {"email": "bo@example.com"}, {"name": "Bo"},
])
async def test_extra_identity_arguments_are_ignored(server, extra):
    make_user("Bo")  # user 1
    _, alex_key = make_user("Alex")
    is_error, text = await call(server, "whoami", extra, key=alex_key)
    assert "Bo" not in text
    assert is_error or text == "Alex"


async def test_no_tool_accepts_identity_inputs(server):
    _, key = make_user("Alex")
    async with streamablehttp_client(server, headers={"Authorization": f"Bearer {key}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
    banned = {"user", "user_id", "userid", "email", "name", "username", "uid"}
    for tool in tools:
        params = set((tool.inputSchema or {}).get("properties", {}))
        assert not params & banned, f"{tool.name} takes an identity input: {params & banned}"


# 4. Concurrency: 50 interleaved calls, every reply matches its caller.
async def test_fifty_interleaved_calls(server):
    _, alex_key = make_user("Alex")
    _, bo_key = make_user("Bo")
    callers = [("Alex", alex_key), ("Bo", bo_key)] * 25
    results = await asyncio.gather(*(call(server, "whoami", key=k) for _, k in callers))
    for (expected, _), (is_error, text) in zip(callers, results):
        assert (is_error, text) == (False, expected)


# 6. Logs never contain keys.
async def test_logs_never_contain_keys(server, caplog):
    caplog.set_level(logging.DEBUG)
    _, key = make_user("Alex")
    await call(server, "whoami", key=key)
    await call(server, "whoami", key=key, key_in_link=True)
    # Only the server's own log lines count. The test client stands in for
    # Claude and naturally logs the link it connects to; that is not ours.
    client_side = ("httpx", "httpcore", "mcp.client")
    server_lines = [r.getMessage() for r in caplog.records
                    if not r.name.startswith(client_side)]
    assert server_lines
    assert not [line for line in server_lines if key in line]
    assert any("user=" in line for line in server_lines)  # audit line, no secrets


async def test_every_call_is_audited_by_user(server):
    uid, key = make_user("Alex")
    await call(server, "whoami", key=key)
    from sqlalchemy import select
    with db.open_session() as s:
        rows = s.scalars(select(db.AuditLog).where(db.AuditLog.user_id == uid)).all()
    assert [(r.action, r.ok) for r in rows] == [("whoami", True)]
