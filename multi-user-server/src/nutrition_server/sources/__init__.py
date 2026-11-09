"""Nutrition and training sources (Cronometer, later Garmin).

A tool never builds a source client itself. It calls `connected(...)`,
which loads that one user's sealed login, builds a fresh client for this
request only, and saves back anything the client refreshed (for example a
new session token). Nothing is shared between users or between requests.
"""

import os
from contextlib import contextmanager

from nutrition_server import db


class NeedsReconnect(Exception):
    """The user has no working connection to this source."""

    def __init__(self, source: str):
        base = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
        super().__init__(f"Please reconnect {source.title()} at {base}/connect")
        self.source = source


# --- Connecting (used by the onboarding website) ---------------------------


class Connected:
    """Login finished. `state` is what gets sealed in the credentials table."""

    def __init__(self, state: dict):
        self.state = state


class NeedsCode:
    """The source wants a two-factor code. `paused` is sealed into
    pending_logins until the person types the code."""

    def __init__(self, paused: dict, prompt: str):
        self.paused = paused
        self.prompt = prompt


class LoginFailed(Exception):
    """Wrong email or password (shown to the person as-is)."""


class WrongCode(Exception):
    """The two-factor code was not accepted."""


class SourceBusy(Exception):
    """The source said 'too many requests' or blocked the login."""


def _cronometer_connector():
    from nutrition_server.sources.cronometer import CronometerConnector

    return CronometerConnector()


# Which connector handles each source on the Connect page. Tests swap in fakes.
CONNECTORS = {"cronometer": _cronometer_connector}


def _cronometer_factory(state: dict):
    from nutrition_server.sources.cronometer import CronometerSource

    return CronometerSource(state)


# Which class serves each source. Tests swap in fakes here. This holds code,
# never user data.
FACTORIES = {"cronometer": _cronometer_factory}


@contextmanager
def connected(s, user: db.User, source: str):
    state = db.get_credential(s, user.id, source)
    if state is None:
        raise NeedsReconnect(source)
    client = FACTORIES[source](state)
    try:
        yield client
    finally:
        # Save refreshed tokens even if the call itself failed afterwards.
        if client.state != state:
            db.save_credential(s, user.id, source, client.state)
