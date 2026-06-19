"""
A small, from-scratch OAuth 2.1 authorization server for the Garmin MCP server.

The MCP library runs the "door" (rejecting anyone without a valid token and
telling clients where to authenticate). This file is the "front desk" we build
by hand: it issues tokens, checks the owner password, and verifies PKCE.
"""

import base64
import hashlib
import html as _html
import os
import secrets
import time
from urllib.parse import urlencode

import jwt  # the PyJWT library, for signing tokens

from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


# --- Configuration, read from environment variables ------------------------
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")   # your Railway https address
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "")       # the single password you choose
JWT_SECRET = os.environ.get("JWT_SECRET", "")               # random string used to sign tokens
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30                       # access tokens last 30 days
AUTH_CODE_TTL_SECONDS = 300                                 # login codes last 5 minutes
REFRESH_TTL_SECONDS = 60 * 60 * 24 * 180                    # refresh tokens last 180 days

# Auth only switches on when all three are set. On your Mac they're unset, so
# the server still runs locally with no auth, exactly as it does today.
AUTH_ENABLED = bool(PUBLIC_URL and OWNER_PASSWORD and JWT_SECRET)


# --- Access tokens: signed JWTs (the "wristband") --------------------------
def make_access_token(client_id: str, scope: str = "garmin") -> str:
    now = int(time.time())
    payload = {
        "iss": PUBLIC_URL, "sub": "owner", "client_id": client_id,
        "scope": scope, "iat": now, "exp": now + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# --- Authorization codes: short-lived JWTs handed out after login ----------
def make_auth_code(client_id: str, redirect_uri: str, code_challenge: str, scope: str = "garmin") -> str:
    now = int(time.time())
    payload = {
        "typ": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": code_challenge, "scope": scope,
        "iat": now, "exp": now + AUTH_CODE_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_auth_code(code: str) -> dict | None:
    try:
        claims = jwt.decode(code, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("typ") == "code" else None


# --- Refresh tokens: let Claude get fresh access tokens silently -----------
def make_refresh_token(client_id: str, scope: str = "garmin") -> str:
    now = int(time.time())
    payload = {
        "typ": "refresh", "client_id": client_id, "scope": scope,
        "iat": now, "exp": now + REFRESH_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_refresh_token(token: str) -> dict | None:
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("typ") == "refresh" else None


# --- PKCE: proves the client finishing the login is the one that started it -
def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return secrets.compare_digest(expected, code_challenge)


# --- The "door" side: how the MCP library checks each wristband -------------
class JwtTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        claims = verify_access_token(token)
        if not claims:
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("client_id", "claude"),
            scopes=claims.get("scope", "").split(),
            expires_at=claims.get("exp"),
        )


# --- The login page shown at /authorize ------------------------------------
def _login_page(fields: dict, error: str = "") -> str:
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_html.escape(v)}">' for k, v in fields.items()
    )
    error_html = f'<p class="err">{_html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Connect to Garmin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family:-apple-system,system-ui,sans-serif; background:#0b1220; color:#e6edf3;
         display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0; }}
  .card {{ background:#131c2e; padding:32px; border-radius:14px; width:320px;
          box-shadow:0 10px 40px rgba(0,0,0,.45); }}
  h1 {{ font-size:18px; margin:0 0 6px; }}
  p.sub {{ color:#8b98a9; font-size:13px; margin:0 0 18px; }}
  p.err {{ color:#ff6b6b; font-size:13px; margin:0 0 12px; }}
  input[type=password] {{ width:100%; padding:11px 12px; border-radius:9px; border:1px solid #2a3650;
          background:#0b1220; color:#e6edf3; font-size:15px; box-sizing:border-box; }}
  button {{ width:100%; margin-top:14px; padding:11px; border:0; border-radius:9px;
          background:#3b82f6; color:#fff; font-size:15px; font-weight:600; cursor:pointer; }}
</style></head>
<body>
  <form class="card" method="post" action="/authorize">
    <h1>Connect your Garmin data</h1>
    <p class="sub">Enter your access password to let Claude connect.</p>
    {error_html}
    <input type="password" name="password" placeholder="Password" autofocus required>
    {hidden}
    <button type="submit">Authorize</button>
  </form>
</body></html>"""


def _metadata() -> dict:
    return {
        "issuer": PUBLIC_URL,
        "authorization_endpoint": f"{PUBLIC_URL}/authorize",
        "token_endpoint": f"{PUBLIC_URL}/token",
        "registration_endpoint": f"{PUBLIC_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["garmin"],
    }


def install_oauth_routes(app):
    """Attach our authorization-server endpoints to the running MCP server."""

    @app.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def authorization_server_metadata(request: Request):
        return JSONResponse(_metadata())

    @app.custom_route("/register", methods=["POST"])
    async def register(request: Request):
        body = await request.json()
        client_id = "claude-" + secrets.token_urlsafe(8)
        return JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": int(time.time()),
                "token_endpoint_auth_method": "none",
                "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
                "response_types": body.get("response_types", ["code"]),
                "redirect_uris": body.get("redirect_uris", []),
            },
            status_code=201,
        )

    @app.custom_route("/authorize", methods=["GET"])
    async def authorize_form(request: Request):
        p = request.query_params
        fields = {
            "client_id": p.get("client_id", ""),
            "redirect_uri": p.get("redirect_uri", ""),
            "state": p.get("state", ""),
            "code_challenge": p.get("code_challenge", ""),
            "scope": p.get("scope", "garmin"),
        }
        return HTMLResponse(_login_page(fields))

    @app.custom_route("/authorize", methods=["POST"])
    async def authorize_submit(request: Request):
        form = await request.form()
        fields = {
            "client_id": form.get("client_id", ""),
            "redirect_uri": form.get("redirect_uri", ""),
            "state": form.get("state", ""),
            "code_challenge": form.get("code_challenge", ""),
            "scope": form.get("scope", "garmin"),
        }
        password = form.get("password", "")
        if not (OWNER_PASSWORD and secrets.compare_digest(password, OWNER_PASSWORD)):
            return HTMLResponse(_login_page(fields, error="Incorrect password."), status_code=401)

        redirect_uri = fields["redirect_uri"]
        code = make_auth_code(fields["client_id"], redirect_uri, fields["code_challenge"], fields["scope"])
        params = {"code": code}
        if fields["state"]:
            params["state"] = fields["state"]
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    @app.custom_route("/token", methods=["POST"])
    async def token(request: Request):
        form = await request.form()
        grant_type = form.get("grant_type", "")

        if grant_type == "authorization_code":
            claims = verify_auth_code(form.get("code", ""))
            if not claims:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if not verify_pkce(form.get("code_verifier", ""), claims.get("code_challenge", "")):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE check failed"},
                    status_code=400,
                )
            client_id = claims.get("client_id", "claude")
            scope = claims.get("scope", "garmin")
        elif grant_type == "refresh_token":
            claims = verify_refresh_token(form.get("refresh_token", ""))
            if not claims:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            client_id = claims.get("client_id", "claude")
            scope = claims.get("scope", "garmin")
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        return JSONResponse(
            {
                "access_token": make_access_token(client_id, scope),
                "token_type": "Bearer",
                "expires_in": TOKEN_TTL_SECONDS,
                "refresh_token": make_refresh_token(client_id, scope),
                "scope": scope,
            }
        )