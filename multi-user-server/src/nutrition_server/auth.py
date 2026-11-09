"""The key check, and current_user(): the only way code learns who is calling.

The key arrives either as `Authorization: Bearer <key>` or inside the
connector link as `?key=<key>`. The middleware checks it before any tool
runs, strips it from the request so nothing downstream can log it, and
pins the user id onto that one HTTP request. Tools read it back from the
request they were called with, never from their own inputs.
"""

import json
from urllib.parse import parse_qsl, urlencode

from mcp.server.fastmcp import Context

from nutrition_server import db
from nutrition_server.security import hash_secret

USER_ID_SCOPE_KEY = "nutrition_user_id"


class NotAuthenticated(Exception):
    pass


def _extract_key(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode("latin-1")
            if text.lower().startswith("bearer "):
                return text[7:].strip() or None
    params = parse_qsl(scope.get("query_string", b"").decode("latin-1"))
    for name, value in params:
        if name == "key":
            return value or None
    return None


def _strip_key(scope) -> None:
    """Remove the key from the request so no later code or log sees it."""
    scope["headers"] = [(n, v) for n, v in scope.get("headers", []) if n != b"authorization"]
    params = parse_qsl(scope.get("query_string", b"").decode("latin-1"))
    scope["query_string"] = urlencode([(n, v) for n, v in params if n != "key"]).encode()


async def _reject(send) -> None:
    body = json.dumps({"error": "Missing, wrong or disabled key."}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body})


class KeyAuthMiddleware:
    """Runs before the MCP endpoint. No valid key, no data."""

    def __init__(self, app, protected_prefix: str = "/mcp"):
        self.app = app
        self.protected_prefix = protected_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(self.protected_prefix):
            await self.app(scope, receive, send)
            return
        key = _extract_key(scope)
        _strip_key(scope)
        # Never trust an id that arrived with the request itself.
        scope.pop(USER_ID_SCOPE_KEY, None)
        user = None
        if key:
            with db.open_session() as s:
                user = db.find_user_by_key_hash(s, hash_secret(key))
        if user is None:
            await _reject(send)
            return
        scope[USER_ID_SCOPE_KEY] = user.id
        await self.app(scope, receive, send)


def current_user(ctx: Context) -> db.User:
    """The one gate. Returns the user this request's key belongs to."""
    request = ctx.request_context.request
    user_id = request.scope.get(USER_ID_SCOPE_KEY) if request is not None else None
    if user_id is None:
        raise NotAuthenticated("No authenticated user for this request.")
    with db.open_session() as s:
        user = db.get_active_user(s, user_id)
    if user is None:
        raise NotAuthenticated("This user is disabled.")
    return user
