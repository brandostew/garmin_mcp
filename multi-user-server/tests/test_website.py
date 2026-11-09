"""Step 5: the onboarding website.

Each person gets their own browser (TestClient with its own cookie jar).
"""

import logging
import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

from nutrition_server import admin, db, sources
from nutrition_server.security import hash_secret
from nutrition_server.server import build_app

from .fake_sources import FakeCodeConnector

PASSWORD = "a-long-service-password"
FAKE_PASSWORD = "right-password"


@pytest.fixture(autouse=True)
def fake_source(monkeypatch, database):
    FakeCodeConnector.contacts = 0
    monkeypatch.setattr(sources, "CONNECTORS", {"fake": FakeCodeConnector})


def browser() -> TestClient:
    return TestClient(build_app(), base_url="https://testserver")


def csrf(page_html: str) -> str:
    return re.search(r"name='csrf' value='([^']+)'", page_html).group(1)


def onboard(name: str, login_name: str) -> TestClient:
    """Accept a fresh invite in a new browser. Returns the signed-in browser."""
    code = admin.create_invite(name).rsplit("/", 1)[1]
    b = browser()
    page = b.get(f"/invite/{code}")
    resp = b.post(f"/invite/{code}", data={"csrf": csrf(page.text), "login_name": login_name,
                                          "password": PASSWORD, "password2": PASSWORD})
    assert resp.status_code == 200 and "Connect your apps" in resp.text
    return b


def start_fake_login(b: TestClient) -> str:
    """Submit the fake source's login form. Returns the paused-login page URL."""
    page = b.get("/connect")
    resp = b.post("/connect/fake", data={"csrf": csrf(page.text), "email": "a@x.com",
                                        "password": FAKE_PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303
    return resp.headers["location"]


def submit_code(b: TestClient, url: str, code: str):
    page = b.get("/connect")  # any signed-in page carries this session's token
    return b.post(url, data={"csrf": csrf(page.text), "code": code})


def pending_count() -> int:
    with db.open_session() as s:
        return s.scalar(select(func.count()).select_from(db.PendingLogin))


def credential(user_name: str):
    with db.open_session() as s:
        user = s.scalar(select(db.User).where(db.User.display_name == user_name))
        return db.get_credential(s, user.id, "fake")


# ---- Brief test 7: the paused (two-factor) login --------------------------------


def test_paused_login_happy_path_and_cannot_be_used_twice():
    alex = onboard("Alex", "alex")
    url = start_fake_login(alex)
    page = alex.get(url)
    assert "Enter the code Fake Source sent you." in page.text
    done = submit_code(alex, url, "123456")
    assert "Fake Source connected." in done.text
    assert credential("Alex") == {"email": "a@x.com", "token": "fake-token"}
    assert pending_count() == 0
    again = submit_code(alex, url, "123456")
    assert again.status_code == 404 and "expired or was already finished" in again.text


def test_another_signed_in_user_cannot_complete_it():
    alex = onboard("Alex", "alex")
    bo = onboard("Bo", "bo")
    url = start_fake_login(alex)
    contacts = FakeCodeConnector.contacts
    resp = submit_code(bo, url, "123456")
    assert resp.status_code == 404
    assert "Enter the code" not in bo.get(url).text
    assert FakeCodeConnector.contacts == contacts  # Bo never reached the source
    assert credential("Bo") is None and credential("Alex") is None
    # Alex's paused login is untouched: no try used up, still finishable.
    with db.open_session() as s:
        assert s.scalar(select(db.PendingLogin)).tries_left == 3
    assert "Fake Source connected." in submit_code(alex, url, "123456").text


def test_same_user_in_a_different_browser_cannot_complete_it():
    alex = onboard("Alex", "alex")
    url = start_fake_login(alex)
    other = browser()
    page = other.get("/login")
    other.post("/login", data={"csrf": csrf(page.text), "login_name": "alex",
                               "password": PASSWORD})
    assert submit_code(other, url, "123456").status_code == 404
    assert credential("Alex") is None


def test_it_expires_after_ten_minutes():
    alex = onboard("Alex", "alex")
    url = start_fake_login(alex)
    with db.open_session() as s:
        p = s.scalar(select(db.PendingLogin))
        assert abs(db.as_utc(p.expires_at) - (db.utcnow() + timedelta(minutes=10))) < timedelta(seconds=30)
        p.expires_at = db.utcnow() - timedelta(seconds=1)
        s.commit()
    assert submit_code(alex, url, "123456").status_code == 404
    assert credential("Alex") is None and pending_count() == 0


def test_three_wrong_codes_and_it_is_gone():
    alex = onboard("Alex", "alex")
    url = start_fake_login(alex)
    assert "2 tries left" in submit_code(alex, url, "000000").text
    assert "1 tries left" in submit_code(alex, url, "111111").text
    assert "Too many wrong codes" in submit_code(alex, url, "222222").text
    assert pending_count() == 0
    assert submit_code(alex, url, "123456").status_code == 404
    assert credential("Alex") is None


def test_code_form_needs_the_anti_forgery_token():
    alex = onboard("Alex", "alex")
    url = start_fake_login(alex)
    resp = alex.post(url, data={"code": "123456"})
    assert "Form expired" in resp.text and credential("Alex") is None


# ---- Brief test 8: invites ---------------------------------------------------------


def test_invite_works_once():
    code = admin.create_invite("Alex").rsplit("/", 1)[1]
    b = browser()
    form = {"csrf": csrf(b.get(f"/invite/{code}").text), "login_name": "alex",
            "password": PASSWORD, "password2": PASSWORD}
    assert "Connect your apps" in b.post(f"/invite/{code}", data=form).text
    assert browser().get(f"/invite/{code}").status_code == 404
    assert browser().post(f"/invite/{code}", data=form).status_code == 404


def test_expired_invite_is_refused():
    code = admin.create_invite("Alex").rsplit("/", 1)[1]
    with db.open_session() as s:
        s.scalar(select(db.Invite)).expires_at = db.utcnow() - timedelta(seconds=1)
        s.commit()
    assert browser().get(f"/invite/{code}").status_code == 404


def test_made_up_invite_is_refused():
    assert browser().get("/invite/not-a-real-code").status_code == 404


def test_short_password_and_taken_name_are_refused():
    onboard("Alex", "alex")
    code = admin.create_invite("Bo").rsplit("/", 1)[1]
    b = browser()
    token = csrf(b.get(f"/invite/{code}").text)
    short = b.post(f"/invite/{code}", data={"csrf": token, "login_name": "bo",
                                           "password": "short", "password2": "short"})
    assert "at least 10" in short.text
    taken = b.post(f"/invite/{code}", data={"csrf": token, "login_name": "alex",
                                           "password": PASSWORD, "password2": PASSWORD})
    assert "taken" in taken.text


# ---- Brief test 9: at most 3 source logins per hour ---------------------------------


def test_fourth_attempt_in_an_hour_never_contacts_the_source():
    alex = onboard("Alex", "alex")
    for _ in range(3):
        start_fake_login(alex)
    assert FakeCodeConnector.contacts == 3
    page = alex.get("/connect")
    resp = alex.post("/connect/fake", data={"csrf": csrf(page.text), "email": "a@x.com",
                                           "password": FAKE_PASSWORD})
    assert resp.status_code == 429 and "Try again in an hour" in resp.text
    assert FakeCodeConnector.contacts == 3


def test_attempt_limit_is_per_person():
    alex = onboard("Alex", "alex")
    for _ in range(3):
        start_fake_login(alex)
    bo = onboard("Bo", "bo")
    start_fake_login(bo)  # Bo is unaffected by Alex's attempts


# ---- Sign-in, cookies, forms, headers ----------------------------------------------


def test_sign_in_and_wrong_password_limit():
    onboard("Alex", "alex")
    b = browser()
    for _ in range(5):
        page = b.get("/login")
        r = b.post("/login", data={"csrf": csrf(page.text), "login_name": "alex",
                                   "password": "wrong-password"})
        assert "Wrong sign-in name or password" in r.text
    page = b.get("/login")
    r = b.post("/login", data={"csrf": csrf(page.text), "login_name": "alex",
                               "password": PASSWORD})
    assert "Too many wrong passwords" in r.text


def test_signed_out_pages_send_you_to_sign_in():
    b = browser()
    for path in ("/connect", "/connector"):
        r = b.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_forged_form_post_is_refused():
    alex = onboard("Alex", "alex")
    r = alex.post("/connect/fake", data={"email": "a@x.com", "password": FAKE_PASSWORD})
    assert "Form expired" in r.text and FakeCodeConnector.contacts == 0


def test_cookies_and_security_headers():
    code = admin.create_invite("Alex").rsplit("/", 1)[1]
    b = browser()
    page = b.get(f"/invite/{code}")
    for name in ("strict-transport-security", "x-frame-options", "content-security-policy",
                 "referrer-policy", "x-content-type-options"):
        assert name in page.headers
    assert page.headers["referrer-policy"] == "no-referrer"
    r = b.post(f"/invite/{code}", data={"csrf": csrf(page.text), "login_name": "alex",
                                       "password": PASSWORD, "password2": PASSWORD},
               follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_plain_http_is_redirected_to_https():
    r = browser().get("/login", headers={"x-forwarded-proto": "http"}, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"].startswith("https://")


# ---- Connector link ------------------------------------------------------------------


def test_connector_link_shown_once_and_regenerating_kills_the_old_one():
    alex = onboard("Alex", "alex")
    page = alex.get("/connector")
    made = alex.post("/connector", data={"csrf": csrf(page.text)})
    link = re.search(r"<code>([^<]+)</code>", made.text).group(1).replace("&amp;", "&")
    key = link.split("key=", 1)[1]
    assert link.startswith("https://nutrition.example/mcp?key=nk_")
    assert key not in alex.get("/connector").text  # not shown again
    with db.open_session() as s:
        stored = s.scalar(select(db.User.key_hash).where(db.User.login_name == "alex"))
    assert stored != key and len(stored) == 64
    remade = alex.post("/connector", data={"csrf": csrf(page.text)})
    new_key = re.search(r"key=([^<]+)</code>", remade.text).group(1)
    assert new_key != key
    with db.open_session() as s:
        assert db.find_user_by_key_hash(s, hash_secret(key)) is None


# ---- Brief test 6 for the website: no secrets in logs ---------------------------------


def test_logs_never_contain_passwords_codes_or_links(caplog):
    caplog.set_level(logging.DEBUG)
    alex = onboard("Alex", "alex")
    url = start_fake_login(alex)
    submit_code(alex, url, "123456")
    page = alex.get("/connector")
    made = alex.post("/connector", data={"csrf": csrf(page.text)})
    key = re.search(r"key=([^<]+)</code>", made.text).group(1)
    text = caplog.text
    for secret in (PASSWORD, FAKE_PASSWORD, "123456", key, "fake-token"):
        assert secret not in text
