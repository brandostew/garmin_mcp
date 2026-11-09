"""Step 6: Garmin, with a pretend Garmin library (no internet)."""

import json
import re

import pytest
from sqlalchemy import select

from nutrition_server import db, sources
from nutrition_server.sources import LoginFailed, NeedsCode, garmin as garmin_mod
from nutrition_server.sources.garmin import GarminConnector

from . import fake_garmin
from .conftest import call, make_user
from .fake_garmin import GARMIN_SERVER, FakeGarmin, add_account
from .test_website import PASSWORD, browser, csrf, onboard

ALEX_GARMIN_PW = "alex-garmin-password-xyz"


@pytest.fixture(autouse=True)
def fake_garmin_library(monkeypatch, database):
    fake_garmin.reset()
    monkeypatch.setattr(garmin_mod, "Garmin", FakeGarmin)
    monkeypatch.setattr(garmin_mod, "_login_session", lambda: None)
    monkeypatch.setattr(sources, "CONNECTORS", {"garmin": GarminConnector})
    garmin_mod._paused.clear()


def click_connect(b, email, password):
    page = b.get("/connect")
    return b.post("/connect/garmin", data={"csrf": csrf(page.text), "email": email,
                                          "password": password}, follow_redirects=False)


def submit_code(b, url, code):
    page = b.get("/connect")
    return b.post(url, data={"csrf": csrf(page.text), "code": code})


def stored(display_name):
    with db.open_session() as s:
        uid = s.scalar(select(db.User.id).where(db.User.display_name == display_name))
        return db.get_credential(s, uid, "garmin")


def every_byte_in_the_database() -> bytes:
    """All stored values, with sealed ones opened, as one blob to search."""
    from nutrition_server.crypto import unseal
    blob = b""
    with db.open_session() as s:
        for table in db.Base.metadata.sorted_tables:
            for row in s.execute(select(table)):
                for value in row:
                    if isinstance(value, bytes):
                        blob += value + json.dumps(unseal(value)).encode()
                    else:
                        blob += str(value).encode()
    return blob


# ---- Connecting through the website --------------------------------------------


def test_connect_with_code_keeps_tokens_not_the_password():
    add_account("alex@example.com", ALEX_GARMIN_PW)
    alex = onboard("Alex", "alex")
    r = click_connect(alex, "alex@example.com", ALEX_GARMIN_PW)
    assert r.status_code == 303
    url = r.headers["location"]
    assert "Enter the code Garmin just sent you" in alex.get(url).text
    # While paused, the database holds no password either.
    assert ALEX_GARMIN_PW.encode() not in every_byte_in_the_database()
    done = submit_code(alex, url, "654321")
    assert "Garmin connected." in done.text
    state = stored("Alex")
    assert set(state) == {"tokens"}
    # Brief test 10: no Garmin password anywhere in the database.
    assert ALEX_GARMIN_PW.encode() not in every_byte_in_the_database()
    assert garmin_mod._paused == {}


def test_one_click_is_exactly_one_login_request():
    add_account("alex@example.com", ALEX_GARMIN_PW)
    alex = onboard("Alex", "alex")
    click_connect(alex, "alex@example.com", ALEX_GARMIN_PW)
    assert GARMIN_SERVER["login_requests"] == 1


def test_wrong_password_one_request_and_a_clear_message():
    add_account("alex@example.com", ALEX_GARMIN_PW)
    alex = onboard("Alex", "alex")
    page = alex.get("/connect")
    r = alex.post("/connect/garmin", data={"csrf": csrf(page.text), "email": "alex@example.com",
                                          "password": "wrong"})
    assert "Garmin did not accept that email and password." in r.text
    assert GARMIN_SERVER["login_requests"] == 1 and stored("Alex") is None


def test_garmin_too_many_requests_says_try_in_an_hour():
    add_account("alex@example.com", ALEX_GARMIN_PW)
    GARMIN_SERVER["busy"] = True
    alex = onboard("Alex", "alex")
    page = alex.get("/connect")
    r = alex.post("/connect/garmin", data={"csrf": csrf(page.text), "email": "alex@example.com",
                                          "password": ALEX_GARMIN_PW})
    assert r.status_code == 429 and "Try again in an hour" in r.text
    assert GARMIN_SERVER["login_requests"] == 1


def test_account_without_two_factor_connects_straight_away():
    add_account("alex@example.com", ALEX_GARMIN_PW, needs_code=False)
    alex = onboard("Alex", "alex")
    page = alex.get("/connect")
    r = alex.post("/connect/garmin", data={"csrf": csrf(page.text), "email": "alex@example.com",
                                          "password": ALEX_GARMIN_PW})
    assert "Garmin connected." in r.text and set(stored("Alex")) == {"tokens"}


def test_wrong_code_then_right_code():
    add_account("alex@example.com", ALEX_GARMIN_PW)
    alex = onboard("Alex", "alex")
    url = click_connect(alex, "alex@example.com", ALEX_GARMIN_PW).headers["location"]
    assert "2 tries left" in submit_code(alex, url, "000000").text
    assert "Garmin connected." in submit_code(alex, url, "654321").text
    assert GARMIN_SERVER["login_requests"] == 1  # the password was sent once


def test_three_wrong_codes_frees_the_paused_login_from_memory():
    add_account("alex@example.com", ALEX_GARMIN_PW)
    alex = onboard("Alex", "alex")
    url = click_connect(alex, "alex@example.com", ALEX_GARMIN_PW).headers["location"]
    for code in ("1", "2", "3"):
        submit_code(alex, url, code)
    assert garmin_mod._paused == {}


def test_paused_logins_in_memory_are_keyed_by_user():
    add_account("alex@example.com", ALEX_GARMIN_PW)
    connector = GarminConnector()
    result = connector.start(1, {"email": "alex@example.com", "password": ALEX_GARMIN_PW})
    assert isinstance(result, NeedsCode)
    assert all(key.startswith("1:") for key in garmin_mod._paused)
    with pytest.raises(LoginFailed):  # user 2 holding user 1's handle gets nothing
        connector.finish(2, result.paused, "654321")
    assert GARMIN_SERVER["code_checks"] == 0
    assert set(connector.finish(1, result.paused, "654321").state) == {"tokens"}


def test_paused_login_in_memory_expires(monkeypatch):
    add_account("alex@example.com", ALEX_GARMIN_PW)
    connector = GarminConnector()
    result = connector.start(1, {"email": "alex@example.com", "password": ALEX_GARMIN_PW})
    monkeypatch.setattr(garmin_mod, "PAUSE_SECONDS", 0)
    key = next(iter(garmin_mod._paused))
    garmin_mod._paused[key] = (0, garmin_mod._paused[key][1])
    with pytest.raises(LoginFailed):
        connector.finish(1, result.paused, "654321")
    assert garmin_mod._paused == {}


# ---- Using Garmin from Claude ------------------------------------------------------


def connect_directly(name, email):
    """Connect a user as the website would, returning (user_id, key)."""
    add_account(email, "pw", needs_code=False)
    uid, key = make_user(name)
    result = GarminConnector().start(uid, {"email": email, "password": "pw"})
    with db.open_session() as s:
        db.save_credential(s, uid, "garmin", result.state)
    return uid, key


async def test_each_person_sees_only_their_own_garmin(server):
    _, alex_key = connect_directly("Alex", "alex@example.com")
    _, bo_key = connect_directly("Bo", "bo@example.com")
    _, alex = await call(server, "get_recent_activities", {"limit": 2}, key=alex_key)
    _, bo = await call(server, "get_recent_activities", {"limit": 2}, key=bo_key)
    assert "alex@example.com-run" in alex and "bo@" not in alex
    assert "bo@example.com-run" in bo and "alex@" not in bo
    _, sleep = await call(server, "get_sleep", {"day": "2026-11-09"}, key=bo_key)
    _, ready = await call(server, "get_training_readiness", {"day": "2026-11-09"}, key=bo_key)
    assert "alex" not in sleep + ready and "bo@example.com" in sleep + ready


async def test_rotated_tokens_are_saved_so_the_next_call_works(server):
    uid, key = connect_directly("Alex", "alex@example.com")
    for _ in range(3):  # each call refreshes, making the previous token useless
        is_error, text = await call(server, "get_recent_activities", {"limit": 1}, key=key)
        assert not is_error, text
    with db.open_session() as s:
        saved = json.loads(db.get_credential(s, uid, "garmin")["tokens"])
    assert saved["di_refresh_token"] == GARMIN_SERVER["valid_refresh"]["alex@example.com"]


async def test_revoked_tokens_ask_to_reconnect_without_logging_in(server):
    _, key = connect_directly("Alex", "alex@example.com")
    GARMIN_SERVER["valid_refresh"]["alex@example.com"] = "revoked"
    before = GARMIN_SERVER["login_requests"]
    is_error, text = await call(server, "get_sleep", {}, key=key)
    assert is_error and "Please reconnect Garmin at https://nutrition.example/connect" in text
    assert GARMIN_SERVER["login_requests"] == before  # never tried a password login


async def test_not_connected_says_please_reconnect(server):
    _, key = make_user("Alex")
    is_error, text = await call(server, "get_recent_activities", {}, key=key)
    assert is_error and "Please reconnect Garmin" in text



def test_one_person_two_threads_take_turns_with_their_tokens(monkeypatch):
    """Garmin refresh tokens work once. If two requests by the same person ran
    at the same moment, both would refresh with the same token and one would
    fail. The per-person lock in sources.connected makes them take turns,
    while different people still run side by side."""
    import threading
    import time

    inside = {"now": 0, "max": 0}
    guard = threading.Lock()

    class SlowSource:
        def __init__(self, state):
            self.state = state

        def work(self):
            with guard:
                inside["now"] += 1
                inside["max"] = max(inside["max"], inside["now"])
            time.sleep(0.05)
            with guard:
                inside["now"] -= 1

    monkeypatch.setitem(sources.FACTORIES, "garmin", SlowSource)
    uid, _ = make_user("Alex")
    with db.open_session() as s:
        db.save_credential(s, uid, "garmin", {"tokens": "x"})
        user = s.get(db.User, uid)

    def run():
        with db.open_session() as s, sources.connected(s, user, "garmin") as src:
            src.work()

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert inside["max"] == 1


def test_refusal_reason_is_logged_without_login_details(caplog, monkeypatch):
    import logging
    from garminconnect import GarminConnectConnectionError
    caplog.set_level(logging.DEBUG)

    def blocked(self, sess, email, password):
        raise GarminConnectConnectionError(f"Mobile login failed: {{'user': '{email}', 'pw': '{password}'}}")

    monkeypatch.setattr(fake_garmin.FakeGarminClient, "_do_mobile_login", blocked)
    from nutrition_server.sources import SourceBusy
    with pytest.raises(SourceBusy):
        GarminConnector().start(1, {"email": "alex@example.com", "password": ALEX_GARMIN_PW})
    assert "GarminConnectConnectionError" in caplog.text
    assert "alex@example.com" not in caplog.text and ALEX_GARMIN_PW not in caplog.text
