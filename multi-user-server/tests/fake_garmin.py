"""A pretend Garmin library (replaces garminconnect.Garmin in tests).

GARMIN_SERVER plays Garmin's own servers. Like the real Garmin, each refresh
hands out a new refresh token and the old one stops working.
"""

import json

from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError
from garminconnect.client import _MFARequired

GARMIN_SERVER = {"accounts": {}, "valid_refresh": {}, "busy": False,
                 "login_requests": 0, "code_checks": 0}


def reset():
    GARMIN_SERVER.update(accounts={}, valid_refresh={}, busy=False,
                         login_requests=0, code_checks=0)


def add_account(email, password, needs_code=True, code="654321"):
    GARMIN_SERVER["accounts"][email] = {"password": password, "needs_code": needs_code,
                                        "code": code, "gen": 0}


def _issue(email):
    acct = GARMIN_SERVER["accounts"][email]
    acct["gen"] += 1
    refresh = f"refresh-{email}-{acct['gen']}"
    GARMIN_SERVER["valid_refresh"][email] = refresh
    return {"di_token": f"access-{email}-{acct['gen']}", "di_refresh_token": refresh,
            "di_client_id": "fake"}


class FakeGarminClient:
    def __init__(self):
        self.tokens = None
        self._email = None

    # --- login ---
    def _do_mobile_login(self, sess, email, password):
        GARMIN_SERVER["login_requests"] += 1
        if GARMIN_SERVER["busy"]:
            raise GarminConnectTooManyRequestsError("429")
        acct = GARMIN_SERVER["accounts"].get(email)
        if acct is None or acct["password"] != password:
            raise GarminConnectAuthenticationError("Invalid Username or Password")
        self._email = email
        if acct["needs_code"]:
            raise _MFARequired()
        self.tokens = _issue(email)

    def _complete_mfa(self, code):
        GARMIN_SERVER["code_checks"] += 1
        if code != GARMIN_SERVER["accounts"][self._email]["code"]:
            raise GarminConnectAuthenticationError("MFA verification failed")
        self.tokens = _issue(self._email)

    # --- tokens ---
    def dumps(self):
        return json.dumps(self.tokens)

    def loads(self, text):
        self.tokens = json.loads(text)
        self._email = self.tokens["di_token"].split("-", 1)[1].rsplit("-", 1)[0]

    @property
    def di_refresh_token(self):
        return self.tokens and self.tokens["di_refresh_token"]

    def _token_expires_soon(self):
        return True  # refresh on every request, to exercise rotation

    def _refresh_session(self):
        if GARMIN_SERVER["valid_refresh"].get(self._email) != self.tokens["di_refresh_token"]:
            raise GarminConnectAuthenticationError("refresh token already used")
        self.tokens = _issue(self._email)

    def connectapi(self, path):
        return {"displayName": self._email}


class FakeGarmin:
    def __init__(self):
        self.client = FakeGarminClient()
        self.display_name = None

    def get_activities(self, start, limit):
        return [{"activityName": f"{self.display_name}-run-{i}"} for i in range(limit)]

    def get_sleep_data(self, cdate):
        return {"owner": self.display_name, "date": cdate}

    def get_training_readiness(self, cdate):
        return [{"owner": self.display_name, "date": cdate, "score": 70}]
