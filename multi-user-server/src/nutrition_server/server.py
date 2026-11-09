"""The MCP server and its tools.

Rules every tool follows (see section 2 of the brief):
- It never takes a user name, email or id as input.
- It gets the caller only through `audited(ctx, ...)`, which uses current_user().
- Every call is written to the audit log with user id, tool name and ok/error.
"""

import logging
import os
from contextlib import contextmanager

from mcp.server.fastmcp import Context, FastMCP

from nutrition_server import db
from nutrition_server.auth import KeyAuthMiddleware, current_user

log = logging.getLogger("nutrition_server")


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
