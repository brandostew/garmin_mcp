"""One person's Cronometer connection, built fresh for one request.

Wraps the two community libraries the single-user server already uses:
- cronometer-api-mcp (mobile app API): food diary.
- cronometer-mcp (website GWT API): macro targets.

Both libraries were written for one person per server: they read the login
from server-wide environment variables and save sessions to a shared file.
The subclasses below replace exactly those parts, so each client holds only
this person's login and session, in memory, and hands any refreshed session
back through `state` to be sealed in the database.

Saved state (sealed in the credentials table):
    {"email", "password", "mobile": {"user_id", "token"}, "web": {...}}
"""

import logging
from datetime import date
from pathlib import Path

from cronometer_api_mcp.client import CronometerClient as _MobileClient
from cronometer_mcp import CronometerClient as _WebClient

from nutrition_server.sources import NeedsReconnect

# The libraries log the login email and the start of the session token.
for _name in ("cronometer_api_mcp", "cronometer_mcp"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class _Mobile(_MobileClient):
    """Mobile-API client that never touches env vars or disk."""

    def __init__(self, email: str, password: str, saved: dict | None):
        self._email = email
        self._password = password
        # A path that is never read or written (the methods below are no-ops).
        super().__init__(session_path=Path("/dev/null/unused"))
        if saved:
            self._user_id = saved.get("user_id")
            self._token = saved.get("token")

    def _get_credentials(self) -> tuple[str, str]:
        return self._email, self._password

    def _cache_key(self) -> str:
        return ""

    def _load_cached_session(self) -> None:
        pass

    def _save_cached_session(self) -> None:
        pass

    def _invalidate_session(self) -> None:
        self._token = None

    def saved(self) -> dict | None:
        if self._token is None:
            return None
        return {"user_id": self._user_id, "token": self._token}


class _Web(_WebClient):
    """Website (GWT) client that never touches env vars or disk."""

    def __init__(self, email: str, password: str, saved: dict | None):
        super().__init__(username=email, password=password)
        self._cookie_path = None
        self._saved = saved

    def _save_session(self) -> None:
        self._saved = {
            "cookies": self.session.cookies.get_dict(),
            "nonce": self.nonce,
            "user_id": self.user_id,
            "gwt_permutation": self.gwt_permutation,
            "gwt_header": self.gwt_header,
        }

    def _restore_session(self) -> bool:
        # Trust the saved session; if Cronometer has ended it, the call
        # fails with NotLoggedIn and CronometerSource logs in again once.
        if not self._saved:
            return False
        for k, v in self._saved["cookies"].items():
            self.session.cookies.set(k, v)
        self.nonce = self._saved["nonce"]
        self.user_id = self._saved["user_id"]
        self.gwt_permutation = self._saved.get("gwt_permutation", self.gwt_permutation)
        self.gwt_header = self._saved.get("gwt_header", self.gwt_header)
        return True

    def saved(self) -> dict | None:
        return self._saved


class CronometerSource:
    """What the tools use. `state` is the person's sealed connection."""

    def __init__(self, state: dict):
        self.state = dict(state)
        self._mobile = _Mobile(state["email"], state["password"], state.get("mobile"))
        self._web = _Web(state["email"], state["password"], state.get("web"))

    def _sync_state(self) -> None:
        self.state = {
            **self.state,
            "mobile": self._mobile.saved(),
            "web": self._web.saved(),
        }

    def _web_call(self, method: str, *args, **kwargs):
        """Call the website API. If Cronometer has ended the session, log in
        again once (no loop). A second failure asks the person to reconnect."""
        try:
            self._web.authenticate()
            return getattr(self._web, method)(*args, **kwargs)
        except Exception as e:
            if "NotLoggedIn" not in str(e):
                raise
        finally:
            self._sync_state()
        self._web._saved = None
        self._web._authenticated = False
        self._web.session.cookies.clear()
        try:
            self._web.authenticate()
            return getattr(self._web, method)(*args, **kwargs)
        except Exception as e:
            if "Login failed" in str(e) or "NotLoggedIn" in str(e):
                raise NeedsReconnect("cronometer") from None
            raise
        finally:
            self._sync_state()

    def get_diary(self, day: date) -> dict:
        try:
            return self._mobile.get_diary(day)
        except Exception as e:
            if "Login failed" in str(e):
                raise NeedsReconnect("cronometer") from None
            raise
        finally:
            self._sync_state()

    def get_daily_targets(self, day: date) -> dict:
        return self._web_call("get_daily_macro_targets", day)

    def set_daily_targets(self, day: date, calories: float, protein_g: float,
                          carbs_g: float, fat_g: float) -> bool:
        return self._web_call(
            "update_daily_targets",
            day=day, protein_g=protein_g, fat_g=fat_g, carbs_g=carbs_g,
            calories=calories, template_name="Custom Targets",
        )
