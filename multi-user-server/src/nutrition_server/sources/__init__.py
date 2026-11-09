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
