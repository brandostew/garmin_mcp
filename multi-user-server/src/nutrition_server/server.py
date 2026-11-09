"""The MCP server and its tools.

Rules every tool follows (see section 2 of the brief):
- It never takes a user name, email or id as input.
- It gets the caller only through `audited(ctx, ...)`, which uses current_user().
- Every call is written to the audit log with user id, tool name and ok/error.
"""

import logging
import os
from contextlib import contextmanager
from datetime import date

from pydantic import BaseModel, Field

from mcp.server.fastmcp import Context, FastMCP

from nutrition_server import db
from nutrition_server.auth import KeyAuthMiddleware, current_user
from nutrition_server.sources import connected
from nutrition_server.web import install_routes

log = logging.getLogger("nutrition_server")


class TrainingSession(BaseModel):
    day: str = Field(description="Day of the week, e.g. 'Monday'")
    name: str = Field(description="Short name, e.g. 'Long run' or 'Upper body'")
    details: str = Field(default="", description="Anything else: distance, intensity, notes")


@contextmanager
def audited(ctx: Context, tool: str):
    """Identify the caller, run the tool body, and log who called what."""
    user = current_user(ctx)
    ok = False
    try:
        yield user
        ok = True
    finally:
        with db.open_session() as s:
            db.record_audit(s, user.id, tool, ok)
        log.info("user=%s tool=%s ok=%s", user.id, tool, ok)


def _day(value: str | None) -> date:
    return date.fromisoformat(value) if value else date.today()


def build_mcp() -> FastMCP:
    # Stateless: every request stands alone, so nothing about one caller can
    # carry over to the next request.
    mcp = FastMCP("Nutrition", stateless_http=True, json_response=True)

    @mcp.tool()
    def whoami(ctx: Context) -> str:
        """Return the display name of the person this connector belongs to."""
        with audited(ctx, "whoami") as user:
            return user.display_name

    @mcp.tool()
    def get_profile(ctx: Context) -> dict:
        """Get your food likes, dislikes, dietary notes and goals."""
        with audited(ctx, "get_profile") as user, db.open_session() as s:
            return db.get_profile(s, user.id)

    @mcp.tool()
    def update_profile(
        ctx: Context,
        likes: str | None = None,
        dislikes: str | None = None,
        dietary_notes: str | None = None,
        goals: str | None = None,
    ) -> dict:
        """Update your food profile. Only the fields you pass are changed;
        each one replaces the old text. Returns the full profile."""
        changes = {"likes": likes, "dislikes": dislikes,
                   "dietary_notes": dietary_notes, "goals": goals}
        with audited(ctx, "update_profile") as user, db.open_session() as s:
            return db.update_profile(s, user.id, changes)

    @mcp.tool()
    def save_training_week(
        ctx: Context, week_start: str, sessions: list[TrainingSession]
    ) -> str:
        """Save your training plan for the week starting on week_start
        (YYYY-MM-DD). Replaces anything already saved for that week."""
        with audited(ctx, "save_training_week") as user, db.open_session() as s:
            start = date.fromisoformat(week_start)
            db.save_training_week(s, user.id, start, [x.model_dump() for x in sessions])
            return f"Saved {len(sessions)} session(s) for the week of {start.isoformat()}."

    @mcp.tool()
    def get_training_week(ctx: Context, week_start: str | None = None) -> dict:
        """Get your training plan for the week starting on week_start
        (YYYY-MM-DD), or your most recently saved week if not given."""
        with audited(ctx, "get_training_week") as user, db.open_session() as s:
            start = date.fromisoformat(week_start) if week_start else None
            week = db.get_training_week(s, user.id, start)
            return week or {"week_start": None, "sessions": [],
                            "note": "No training week saved yet."}

    @mcp.tool()
    def get_food_diary(ctx: Context, day: str | None = None) -> dict:
        """Get your Cronometer food diary for a date (YYYY-MM-DD, default today)."""
        with audited(ctx, "get_food_diary") as user, db.open_session() as s:
            with connected(s, user, "cronometer") as cronometer:
                return cronometer.get_diary(_day(day))

    @mcp.tool()
    def get_daily_targets(ctx: Context, day: str | None = None) -> dict:
        """Get your Cronometer macro targets (calories, protein, carbs, fat)
        for a date (YYYY-MM-DD, default today)."""
        with audited(ctx, "get_daily_targets") as user, db.open_session() as s:
            with connected(s, user, "cronometer") as cronometer:
                return cronometer.get_daily_targets(_day(day))

    @mcp.tool()
    def set_daily_targets(
        ctx: Context, day: str, calories: float, protein: float, carbs: float, fat: float
    ) -> str:
        """Set your Cronometer macro targets for one date (YYYY-MM-DD).
        protein, carbs and fat are in grams. Only that date changes."""
        with audited(ctx, "set_daily_targets") as user, db.open_session() as s:
            target_day = _day(day)
            with connected(s, user, "cronometer") as cronometer:
                cronometer.set_daily_targets(target_day, calories, protein, carbs, fat)
            return (f"Set targets for {target_day.isoformat()}: {calories:g} kcal, "
                    f"{protein:g} g protein, {carbs:g} g carbs, {fat:g} g fat.")

    @mcp.tool()
    def get_recent_activities(ctx: Context, limit: int = 10) -> list:
        """Get your most recent Garmin activities (newest first, up to 50)."""
        with audited(ctx, "get_recent_activities") as user, db.open_session() as s:
            with connected(s, user, "garmin") as garmin:
                return garmin.get_recent_activities(max(1, min(limit, 50)))

    @mcp.tool()
    def get_sleep(ctx: Context, day: str | None = None) -> dict:
        """Get your Garmin sleep data for the night ending on a date
        (YYYY-MM-DD, default today)."""
        with audited(ctx, "get_sleep") as user, db.open_session() as s:
            with connected(s, user, "garmin") as garmin:
                return garmin.get_sleep(_day(day))

    @mcp.tool()
    def get_training_readiness(ctx: Context, day: str | None = None) -> list | dict:
        """Get your Garmin training readiness for a date (YYYY-MM-DD, default today)."""
        with audited(ctx, "get_training_readiness") as user, db.open_session() as s:
            with connected(s, user, "garmin") as garmin:
                return garmin.get_training_readiness(_day(day))

    install_routes(mcp)
    return mcp


_SECURITY_HEADERS = [
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # no-referrer: page addresses carry invite codes; never pass them on.
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy",
     b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
     b"frame-ancestors 'none'; base-uri 'none'"),
    (b"cache-control", b"no-store"),
]


class SecurityMiddleware:
    """HTTPS only (Railway reports the original scheme in X-Forwarded-Proto)
    and standard security headers on every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        if headers.get(b"x-forwarded-proto") == b"http":
            host = headers.get(b"host", b"").decode("latin-1")
            await send({"type": "http.response.start", "status": 308,
                        "headers": [(b"location", f"https://{host}{scope['path']}".encode())]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + _SECURITY_HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


def build_app():
    """The full web app: security headers, then the key check (which guards
    only /mcp), then the MCP endpoint and the onboarding website."""
    return SecurityMiddleware(KeyAuthMiddleware(build_mcp().streamable_http_app()))


def main():
    import uvicorn

    from nutrition_server.crypto import encryption_secret

    logging.basicConfig(level=logging.INFO)
    encryption_secret()  # refuse to start without a real secret
    # access_log=False: request lines can contain the connector key.
    uvicorn.run(
        build_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        access_log=False,
    )


if __name__ == "__main__":
    main()
