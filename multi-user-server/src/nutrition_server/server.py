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

    return mcp


def build_app():
    """The full web app: the key check wrapped around the MCP endpoint."""
    return KeyAuthMiddleware(build_mcp().streamable_http_app())


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    # access_log=False: request lines can contain the connector key.
    uvicorn.run(
        build_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        access_log=False,
    )


if __name__ == "__main__":
    main()
