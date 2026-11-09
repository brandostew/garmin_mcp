"""One person's Garmin connection.

Connecting (onboarding website):
- Exactly ONE login request per click. The library's own `login()` tries up
  to five strategies and rotates browser fingerprints, which can add up to
  around ten attempts; Garmin locks accounts for days after a few failures.
  So we call a single strategy (the iOS-app login) directly.
- If Garmin wants a code, the half-finished login is a live connection that
  cannot be saved to the database, so it is kept in memory for 10 minutes
  under a key that starts with the user's id. A server restart in those 10
  minutes means starting again.
- Only tokens are kept afterwards. The password is never stored.

Using it (tools): tokens are loaded into a fresh client for each request.
Garmin hands out a new refresh token each time it refreshes, so the client's
current tokens are always handed back through `state` to be sealed again.
"""

import logging
import threading
import time
from datetime import date

import requests
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.client import HAS_CFFI, _MFARequired

from nutrition_server.security import new_session_id
from nutrition_server.sources import (
    Connected,
    LoginFailed,
    NeedsCode,
    NeedsReconnect,
    SourceBusy,
    WrongCode,
)

if HAS_CFFI:
    from curl_cffi import requests as cffi_requests

log = logging.getLogger("nutrition_server")

# The library logs request details at debug level.
logging.getLogger("garminconnect").setLevel(logging.WARNING)


def _why(e: Exception, *secrets: str) -> str:
    """A short description of why Garmin refused, for the log. The library's
    messages can include Garmin's reply, so the login details are blanked."""
    text = str(e)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[hidden]")
    return f"{type(e).__name__}: {text[:160]}"

PAUSE_SECONDS = 600

# Paused logins waiting for a code: "user_id:handle" -> (expires, Garmin).
_paused: dict[str, tuple[float, Garmin]] = {}
_paused_lock = threading.Lock()


def _pause_key(user_id: int, handle: str) -> str:
    return f"{user_id}:{handle}"


def _purge_expired() -> None:
    now = time.monotonic()
    with _paused_lock:
        for key in [k for k, (exp, _) in _paused.items() if exp < now]:
            del _paused[key]


def _login_session():
    """One HTTP session for one login attempt."""
    if HAS_CFFI:
        return cffi_requests.Session(impersonate="safari_ios")
    return requests.Session()


class GarminConnector:
    """Connect-page logic for Garmin."""

    label = "Garmin"
    fields = [("email", "Garmin email", "email"),
              ("password", "Garmin password", "password")]

    def start(self, user_id: int, fields: dict):
        _purge_expired()
        garmin = Garmin()
        email, password = fields["email"].strip(), fields["password"]
        try:
            garmin.client._do_mobile_login(_login_session(), email, password)
        except _MFARequired:
            log.info("Garmin login needs a code")
            handle = new_session_id()
            with _paused_lock:
                _paused[_pause_key(user_id, handle)] = (time.monotonic() + PAUSE_SECONDS, garmin)
            return NeedsCode({"handle": handle},
                             "Enter the code Garmin just sent you (by email or text).")
        except GarminConnectTooManyRequestsError as e:
            log.warning("Garmin login refused (rate limit): %s", _why(e, email, password))
            raise SourceBusy() from None
        except GarminConnectAuthenticationError as e:
            log.warning("Garmin login refused (credentials): %s", _why(e, email, password))
            raise LoginFailed("Garmin did not accept that email and password.") from None
        except (GarminConnectConnectionError, requests.RequestException, OSError) as e:
            log.warning("Garmin login failed (connection/blocked): %s", _why(e, email, password))
            raise SourceBusy() from None
        log.info("Garmin login succeeded without a code")
        return Connected({"tokens": garmin.client.dumps()})

    def finish(self, user_id: int, paused: dict, code: str) -> Connected:
        _purge_expired()
        key = _pause_key(user_id, paused["handle"])
        with _paused_lock:
            entry = _paused.get(key)
        if entry is None:
            raise LoginFailed("That Garmin login has expired. Please start again.")
        garmin = entry[1]
        try:
            garmin.client._complete_mfa(code)
        except GarminConnectTooManyRequestsError:
            self.forget(user_id, paused)
            raise SourceBusy() from None
        except GarminConnectAuthenticationError:
            raise WrongCode() from None
        except (GarminConnectConnectionError, requests.RequestException, OSError):
            self.forget(user_id, paused)
            raise SourceBusy() from None
        self.forget(user_id, paused)
        return Connected({"tokens": garmin.client.dumps()})

    def forget(self, user_id: int, paused: dict) -> None:
        with _paused_lock:
            _paused.pop(_pause_key(user_id, paused["handle"]), None)

    def test(self, state: dict) -> tuple[str, dict]:
        source = GarminSource(state)
        activities = source.get_recent_activities(1)
        return f"Read your latest activities ({len(activities)} found).", source.state


class GarminSource:
    """What the tools use. `state` is {"tokens": ...}; never a password."""

    def __init__(self, state: dict):
        self.state = dict(state)
        self._garmin: Garmin | None = None

    def _api(self) -> Garmin:
        if self._garmin is not None:
            return self._garmin
        garmin = Garmin()
        client = garmin.client
        try:
            client.loads(self.state["tokens"])
            if client.di_refresh_token and client._token_expires_soon():
                client._refresh_session()
            profile = client.connectapi("/userprofile-service/socialProfile")
        except (GarminConnectAuthenticationError, GarminConnectConnectionError, KeyError):
            raise NeedsReconnect("garmin") from None
        if not isinstance(profile, dict):
            raise NeedsReconnect("garmin")
        garmin.display_name = profile.get("displayName")
        self._garmin = garmin
        return garmin

    def _call(self, fn):
        try:
            return fn(self._api())
        except GarminConnectAuthenticationError:
            raise NeedsReconnect("garmin") from None
        finally:
            if self._garmin is not None:
                self.state = {"tokens": self._garmin.client.dumps()}

    def get_recent_activities(self, limit: int) -> list:
        return self._call(lambda g: g.get_activities(0, limit))

    def get_sleep(self, day: date) -> dict:
        return self._call(lambda g: g.get_sleep_data(day.isoformat()))

    def get_training_readiness(self, day: date):
        return self._call(lambda g: g.get_training_readiness(day.isoformat()))
