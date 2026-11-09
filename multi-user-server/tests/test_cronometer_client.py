"""Step 4 (real client wrappers), tested with no internet.

These prove the wrappers around the community Cronometer libraries:
- use only the login they were given (never server-wide settings),
- never write session files to disk,
- re-login at most once when Cronometer ends a session, then ask to reconnect.
"""

import json
from datetime import date

import httpx
import pytest

from nutrition_server.sources import NeedsReconnect
from nutrition_server.sources.cronometer import CronometerSource, _Mobile

DAY = date(2026, 11, 9)


@pytest.fixture(autouse=True)
def hostile_environment(tmp_path, monkeypatch):
    """Server-wide settings point at someone else; home is an empty folder."""
    monkeypatch.setenv("CRONOMETER_USERNAME", "someone-else@example.com")
    monkeypatch.setenv("CRONOMETER_PASSWORD", "someone-else-pw")
    monkeypatch.setenv("CRONOMETER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


def fake_mobile_api(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/api/v2/login":
            return httpx.Response(200, json={"result": "SUCCESS", "id": 42, "sessionKey": "sess-42"})
        return httpx.Response(200, json={"result": "SUCCESS", "diary": []})
    return httpx.MockTransport(handler)


def use_fake_api(mobile: _Mobile, seen: list):
    mobile._http = httpx.Client(base_url="https://mobile.cronometer.com",
                                transport=fake_mobile_api(seen))


def test_mobile_logs_in_with_its_own_login_only(hostile_environment):
    seen = []
    source = CronometerSource({"email": "alex@example.com", "password": "alex-pw"})
    use_fake_api(source._mobile, seen)
    source.get_diary(DAY)
    login = next(body for path, body in seen if path == "/api/v2/login")
    assert login["email"] == "alex@example.com"
    assert login["password"] == "alex-pw"
    # The refreshed session comes back to be sealed, not written to disk.
    assert source.state["mobile"] == {"user_id": 42, "token": "sess-42"}
    assert [p for p in hostile_environment.rglob("*") if p.is_file()] == []


def test_mobile_reuses_saved_session_without_logging_in():
    seen = []
    source = CronometerSource({"email": "alex@example.com", "password": "alex-pw",
                               "mobile": {"user_id": 42, "token": "saved"}})
    use_fake_api(source._mobile, seen)
    source.get_diary(DAY)
    assert [path for path, _ in seen] == ["/api/v2/get_diary"]
    assert seen[0][1]["auth"]["token"] == "saved"


def test_two_people_in_one_process_never_mix(hostile_environment):
    seen_a, seen_b = [], []
    a = CronometerSource({"email": "alex@example.com", "password": "a-pw"})
    b = CronometerSource({"email": "bo@example.com", "password": "b-pw"})
    use_fake_api(a._mobile, seen_a)
    use_fake_api(b._mobile, seen_b)
    a.get_diary(DAY)
    b.get_diary(DAY)
    assert {body["email"] for p, body in seen_a if p == "/api/v2/login"} == {"alex@example.com"}
    assert {body["email"] for p, body in seen_b if p == "/api/v2/login"} == {"bo@example.com"}


def test_web_client_ignores_server_wide_settings():
    source = CronometerSource({"email": "alex@example.com", "password": "alex-pw"})
    assert source._web.username == "alex@example.com"
    assert source._web.password == "alex-pw"
    assert source._web._cookie_path is None


class _StubWeb:
    """Replaces the website calls: first call says the session ended."""

    def __init__(self, web, failures: int):
        self.failures = failures
        self.logins = 0
        web.authenticate = self.authenticate
        web.get_daily_macro_targets = self.get_targets
        self.web = web

    def authenticate(self):
        # Same order as the real library: saved session first, else log in.
        if self.web._authenticated:
            return
        if not self.web._restore_session():
            self.logins += 1
            self.web.nonce = f"fresh-{self.logins}"
            self.web._save_session()
        self.web._authenticated = True

    def get_targets(self, day):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("GWT-RPC call failed. Response: //EX[...NotLoggedInException...]")
        return {"calories": 2925}


def test_expired_web_session_relogs_in_exactly_once():
    source = CronometerSource({"email": "alex@example.com", "password": "alex-pw",
                               "web": {"cookies": {"sesnonce": "old"}, "nonce": "old",
                                       "user_id": "1"}})
    stub = _StubWeb(source._web, failures=1)
    assert source.get_daily_targets(DAY) == {"calories": 2925}
    assert stub.logins == 1
    assert source.state["web"]["nonce"] != "old"


def test_still_expired_after_one_relogin_asks_to_reconnect():
    source = CronometerSource({"email": "alex@example.com", "password": "alex-pw"})
    stub = _StubWeb(source._web, failures=5)
    with pytest.raises(NeedsReconnect):
        source.get_daily_targets(DAY)
    assert stub.logins == 2  # the first login, plus one re-login. No loop.


def test_refused_login_shows_cronometers_reason_but_never_the_email(caplog):
    from nutrition_server.sources import LoginFailed
    from nutrition_server.sources.cronometer import CronometerConnector

    def handler(request):
        return httpx.Response(200, json={"result": "FAIL", "error": "Invalid email or password",
                                         "email": "alex@example.com"})

    connector = CronometerConnector()
    original = _Mobile.__init__

    def patched(self, *a, **k):
        original(self, *a, **k)
        self._http = httpx.Client(base_url="https://mobile.cronometer.com",
                                  transport=httpx.MockTransport(handler))

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(_Mobile, "__init__", patched)
        with _pytest.raises(LoginFailed) as excinfo:
            connector.start(1, {"email": "alex@example.com", "password": "secret-pw"})
    message = str(excinfo.value)
    assert 'Cronometer said: "Invalid email or password"' in message
    assert "alex@example.com" not in message and "secret-pw" not in message
    assert "secret-pw" not in caplog.text and "alex@example.com" not in caplog.text
