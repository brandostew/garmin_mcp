"""Step 4 (fake Cronometer): diary and targets tools keep people apart."""

import asyncio
import json
import logging

import pytest
from sqlalchemy import select

from nutrition_server import db, sources

from .conftest import call, make_user
from .fake_sources import FAKE_ACCOUNTS, FakeCronometer, add_fake_account

DAY = "2026-11-09"
ALEX_PASSWORD = "alex-cronometer-pw-123"


@pytest.fixture(autouse=True)
def fake_cronometer(monkeypatch):
    FAKE_ACCOUNTS.clear()
    FakeCronometer.logins = 0
    monkeypatch.setitem(sources.FACTORIES, "cronometer", FakeCronometer)


def connect(user_id, email, password):
    """What the onboarding website will do after a successful login."""
    with db.open_session() as s:
        db.save_credential(s, user_id, "cronometer", {"email": email, "password": password})


def two_people():
    add_fake_account("alex@example.com", ALEX_PASSWORD,
                     diary={DAY: ["ALEX-oatmeal"]}, targets={DAY: {"calories": 2925}})
    add_fake_account("bo@example.com", "bo-pw",
                     diary={DAY: ["BO-toast"]}, targets={DAY: {"calories": 1800}})
    alex_id, alex_key = make_user("Alex")
    bo_id, bo_key = make_user("Bo")
    connect(alex_id, "alex@example.com", ALEX_PASSWORD)
    connect(bo_id, "bo@example.com", "bo-pw")
    return (alex_id, alex_key), (bo_id, bo_key)


async def test_each_person_gets_their_own_diary_and_targets(server):
    (_, alex_key), (_, bo_key) = two_people()
    _, alex_diary = await call(server, "get_food_diary", {"day": DAY}, key=alex_key)
    _, bo_diary = await call(server, "get_food_diary", {"day": DAY}, key=bo_key)
    assert "ALEX-oatmeal" in alex_diary and "BO" not in alex_diary
    assert "BO-toast" in bo_diary and "ALEX" not in bo_diary
    _, alex_t = await call(server, "get_daily_targets", {"day": DAY}, key=alex_key)
    _, bo_t = await call(server, "get_daily_targets", {"day": DAY}, key=bo_key)
    assert json.loads(alex_t)["calories"] == 2925
    assert json.loads(bo_t)["calories"] == 1800


async def test_set_targets_changes_only_the_callers_account(server):
    (alex_id, alex_key), (_, bo_key) = two_people()
    is_error, text = await call(server, "set_daily_targets",
        {"day": DAY, "calories": 2500, "protein": 200, "carbs": 250, "fat": 70,
         "user_id": alex_id}, key=bo_key)
    assert not is_error and "2500 kcal" in text
    assert FAKE_ACCOUNTS["bo@example.com"]["targets"][DAY]["calories"] == 2500
    assert FAKE_ACCOUNTS["alex@example.com"]["targets"][DAY]["calories"] == 2925


async def test_not_connected_says_please_reconnect(server):
    _, key = make_user("Alex")
    for tool, args in [("get_food_diary", {}), ("get_daily_targets", {}),
                       ("set_daily_targets", {"day": DAY, "calories": 1, "protein": 1,
                                              "carbs": 1, "fat": 1})]:
        is_error, text = await call(server, tool, args, key=key)
        assert is_error
        assert "Please reconnect Cronometer at https://nutrition.example/connect" in text


async def test_refreshed_session_is_saved_back_sealed(server):
    (alex_id, alex_key), _ = two_people()
    await call(server, "get_food_diary", {"day": DAY}, key=alex_key)
    with db.open_session() as s:
        assert db.get_credential(s, alex_id, "cronometer")["mobile"] == {"token": "tok-1"}
    # Next call reuses the saved session: no second login.
    await call(server, "get_food_diary", {"day": DAY}, key=alex_key)
    assert FakeCronometer.logins == 1


async def test_saved_logins_are_unreadable_in_the_database(server):
    (alex_id, _), _ = two_people()
    with db.open_session() as s:
        raw = s.scalar(select(db.Credential.secret).where(db.Credential.user_id == alex_id))
    assert ALEX_PASSWORD.encode() not in raw
    assert b"alex@example.com" not in raw


async def test_no_tool_or_log_ever_shows_the_password(server, caplog):
    caplog.set_level(logging.DEBUG)
    (_, alex_key), _ = two_people()
    texts = []
    for tool, args in [("get_food_diary", {"day": DAY}), ("get_daily_targets", {"day": DAY}),
                       ("set_daily_targets", {"day": DAY, "calories": 1, "protein": 1,
                                              "carbs": 1, "fat": 1}),
                       ("get_profile", {}), ("whoami", {})]:
        texts.append((await call(server, tool, args, key=alex_key))[1])
    assert not [t for t in texts if ALEX_PASSWORD in t or "tok-" in t]
    assert ALEX_PASSWORD not in caplog.text
    assert "tok-" not in caplog.text


async def test_fifty_interleaved_diary_calls(server):
    (_, alex_key), (_, bo_key) = two_people()
    callers = [("ALEX-oatmeal", alex_key), ("BO-toast", bo_key)] * 25
    results = await asyncio.gather(
        *(call(server, "get_food_diary", {"day": DAY}, key=k) for _, k in callers))
    for (expected, _), (is_error, text) in zip(callers, results):
        assert not is_error and expected in text
        assert ("BO" in text) == (expected == "BO-toast")
