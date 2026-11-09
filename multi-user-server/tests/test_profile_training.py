"""Step 3: profile and training-week tools, and that people stay apart."""

import asyncio
import json

import pytest
from sqlalchemy import select
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from nutrition_server import db

from .conftest import call, make_user

WEEK = "2026-11-09"
ALEX_WEEK = [{"day": "Monday", "name": "ALEX-long-run", "details": "18 km easy"}]


def data(text):
    return json.loads(text)


async def test_profile_round_trip_and_partial_update(server):
    _, key = make_user("Alex")
    assert data((await call(server, "get_profile", key=key))[1]) == {
        "likes": "", "dislikes": "", "dietary_notes": "", "goals": ""}
    await call(server, "update_profile", {"likes": "eggs", "goals": "cut to 85 kg"}, key=key)
    await call(server, "update_profile", {"dislikes": "olives"}, key=key)
    assert data((await call(server, "get_profile", key=key))[1]) == {
        "likes": "eggs", "dislikes": "olives", "dietary_notes": "", "goals": "cut to 85 kg"}


async def test_training_week_save_get_and_replace(server):
    _, key = make_user("Alex")
    empty = data((await call(server, "get_training_week", key=key))[1])
    assert empty["sessions"] == []
    is_error, text = await call(server, "save_training_week",
                                {"week_start": WEEK, "sessions": ALEX_WEEK}, key=key)
    assert not is_error and "1 session" in text
    await call(server, "save_training_week", {"week_start": "2026-11-02", "sessions": []}, key=key)
    # No date: latest week. With date: that week.
    assert data((await call(server, "get_training_week", key=key))[1])["sessions"] == ALEX_WEEK
    assert data((await call(server, "get_training_week", {"week_start": "2026-11-02"},
                            key=key))[1])["sessions"] == []
    # Saving the same week again replaces it.
    await call(server, "save_training_week", {"week_start": WEEK, "sessions": []}, key=key)
    assert data((await call(server, "get_training_week", {"week_start": WEEK}, key=key))[1])["sessions"] == []


async def test_bad_date_is_an_error_not_a_crash_and_is_audited(server):
    uid, key = make_user("Alex")
    is_error, _ = await call(server, "get_training_week", {"week_start": "next tuesday"}, key=key)
    assert is_error
    with db.open_session() as s:
        rows = s.scalars(select(db.AuditLog).where(db.AuditLog.user_id == uid)).all()
    assert [(r.action, r.ok) for r in rows] == [("get_training_week", False)]


async def _seed_alex(server):
    alex_id, alex_key = make_user("Alex")
    await call(server, "update_profile", {"likes": "ALEX-likes", "goals": "ALEX-goals"}, key=alex_key)
    await call(server, "save_training_week", {"week_start": WEEK, "sessions": ALEX_WEEK}, key=alex_key)
    return alex_id, alex_key


# Brief test 1, every tool: Bo never sees anything of Alex's.
async def test_every_tool_returns_only_the_callers_data(server):
    alex_id, alex_key = await _seed_alex(server)
    _, bo_key = make_user("Bo")
    async with streamablehttp_client(server, headers={"Authorization": f"Bearer {bo_key}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            sample_args = {"week_start": WEEK, "sessions": []}
            for tool in tools:
                props = tool.inputSchema.get("properties", {})
                required = tool.inputSchema.get("required", [])
                args = {p: sample_args[p] for p in required}
                if "week_start" in props:
                    args["week_start"] = WEEK
                result = await session.call_tool(tool.name, args)
                text = " ".join(c.text for c in result.content)
                assert "ALEX" not in text and "Alex" not in text, tool.name
    # And nothing Bo did changed Alex's data.
    profile = data((await call(server, "get_profile", key=alex_key))[1])
    assert profile["likes"] == "ALEX-likes"
    week = data((await call(server, "get_training_week", {"week_start": WEEK}, key=alex_key))[1])
    assert week["sessions"] == ALEX_WEEK


# Brief test 3 for the new tools: pointing at Alex by id/name does nothing.
@pytest.mark.parametrize("tool,args", [
    ("get_profile", {}),
    ("update_profile", {"likes": "BO-likes"}),
    ("get_training_week", {"week_start": WEEK}),
    ("save_training_week", {"week_start": WEEK, "sessions": []}),
])
async def test_identity_arguments_cannot_reach_another_person(server, tool, args):
    alex_id, alex_key = await _seed_alex(server)
    _, bo_key = make_user("Bo")
    for extra in ({"user_id": alex_id}, {"user": "Alex"}, {"email": "alex@example.com"}):
        _, text = await call(server, tool, {**args, **extra}, key=bo_key)
        assert "ALEX" not in text
    assert data((await call(server, "get_profile", key=alex_key))[1])["likes"] == "ALEX-likes"
    week = data((await call(server, "get_training_week", {"week_start": WEEK}, key=alex_key))[1])
    assert week["sessions"] == ALEX_WEEK


# Brief test 4 with real data: 50 interleaved reads and writes.
async def test_fifty_interleaved_profile_calls(server):
    _, alex_key = make_user("Alex")
    _, bo_key = make_user("Bo")
    await call(server, "update_profile", {"likes": "alex-food"}, key=alex_key)
    await call(server, "update_profile", {"likes": "bo-food"}, key=bo_key)
    callers = [("alex-food", alex_key), ("bo-food", bo_key)] * 25
    results = await asyncio.gather(*(call(server, "get_profile", key=k) for _, k in callers))
    for (expected, _), (is_error, text) in zip(callers, results):
        assert not is_error and data(text)["likes"] == expected
