"""A pretend Cronometer that never touches the internet.

FAKE_ACCOUNTS plays the role of Cronometer's own servers: each email is a
separate account with its own diary and targets.
"""

from datetime import date

FAKE_ACCOUNTS: dict[str, dict] = {}


class FakeCronometer:
    logins = 0

    def __init__(self, state: dict):
        self.state = dict(state)
        account = FAKE_ACCOUNTS.get(state["email"])
        if account is None or account["password"] != state["password"]:
            from nutrition_server.sources import NeedsReconnect
            raise NeedsReconnect("cronometer")
        self._account = account

    def _ensure_session(self):
        if not self.state.get("mobile"):
            FakeCronometer.logins += 1
            self.state = {**self.state, "mobile": {"token": f"tok-{FakeCronometer.logins}"}}

    def get_diary(self, day: date) -> dict:
        self._ensure_session()
        return {"day": day.isoformat(), "entries": self._account["diary"].get(day.isoformat(), [])}

    def get_daily_targets(self, day: date) -> dict:
        self._ensure_session()
        return self._account["targets"].get(day.isoformat(), {})

    def set_daily_targets(self, day, calories, protein_g, carbs_g, fat_g) -> bool:
        self._ensure_session()
        self._account["targets"][day.isoformat()] = {
            "calories": calories, "protein_g": protein_g, "carbs_g": carbs_g, "fat_g": fat_g}
        return True


def add_fake_account(email: str, password: str, diary=None, targets=None):
    FAKE_ACCOUNTS[email] = {"password": password, "diary": diary or {}, "targets": targets or {}}


class FakeCodeConnector:
    """A pretend source that always demands a two-factor code (123456)."""

    label = "Fake Source"
    fields = [("email", "Email", "email"), ("password", "Password", "password")]
    contacts = 0  # how many times the "source" was contacted

    def start(self, fields):
        from nutrition_server.sources import LoginFailed, NeedsCode
        FakeCodeConnector.contacts += 1
        if fields["password"] != "right-password":
            raise LoginFailed("Fake Source did not accept that login.")
        return NeedsCode({"email": fields["email"], "ticket": "t-1"},
                         "Enter the code Fake Source sent you.")

    def finish(self, paused, code):
        from nutrition_server.sources import Connected, WrongCode
        FakeCodeConnector.contacts += 1
        if code != "123456":
            raise WrongCode()
        # Note: no password kept, only a token.
        return Connected({"email": paused["email"], "token": "fake-token"})

    def test(self, state):
        return "Fake test ok.", state
