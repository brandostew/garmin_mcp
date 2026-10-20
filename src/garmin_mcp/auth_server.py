"""OAuth 2.1 authorization server for the hosted Garmin MCP server.

The MCP SDK protects the MCP endpoint and advertises protected-resource
metadata. This module supplies the authorization-server endpoints used by
remote MCP clients such as ChatGPT and Claude.
"""

import base64
import hashlib
import html as _html
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

import jwt

from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30
AUTH_CODE_TTL_SECONDS = 300
REFRESH_TTL_SECONDS = 60 * 60 * 24 * 180
SUPPORTED_SCOPE = "garmin"
OFFLINE_SCOPE = "offline_access"
SUPPORTED_SCOPES = {SUPPORTED_SCOPE, OFFLINE_SCOPE}

# Auth stays off for local stdio use unless all hosted-server settings exist.
AUTH_ENABLED = bool(PUBLIC_URL and OWNER_PASSWORD and JWT_SECRET)

# One-time token identifiers. These are intentionally small, process-local
# replay caches; authorization codes live for only five minutes.
_consumed_codes: dict[str, int] = {}
_consumed_refresh_tokens: dict[str, int] = {}


def issuer_url() -> str:
    """Canonical authorization-server issuer (including its root slash)."""
    return f"{PUBLIC_URL}/"


def resource_url() -> str:
    """Canonical protected resource: the actual Streamable HTTP endpoint."""
    return f"{PUBLIC_URL}/mcp"


def _jwt_decode(token: str, *, audience: str | None = None) -> dict | None:
    """Decode a server-issued JWT with strict issuer and claim validation."""
    try:
        options = {"require": ["iss", "iat", "exp"]}
        if not audience:
            options["verify_aud"] = False
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            issuer=issuer_url(),
            audience=audience,
            options=options,
        )
    except jwt.PyJWTError:
        return None


def _resource(value: str | None) -> str | None:
    """Return the canonical MCP resource, rejecting tokens for other servers."""
    requested = (value or resource_url()).rstrip("/")
    return resource_url() if requested == resource_url() else None


def make_access_token(
    client_id: str, scope: str = SUPPORTED_SCOPE, resource: str | None = None
) -> str:
    now = int(time.time())
    audience = _resource(resource)
    if not audience:
        raise ValueError("invalid resource")
    payload = {
        "iss": issuer_url(),
        "sub": "owner",
        "aud": audience,
        "client_id": client_id,
        "scope": scope,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_access_token(token: str) -> dict | None:
    claims = _jwt_decode(token, audience=resource_url())
    if not claims or claims.get("sub") != "owner" or not claims.get("client_id"):
        return None
    return claims


def make_client_id(redirect_uris: list[str]) -> str:
    """Create a stateless registered client ID bound to exact redirect URIs.

    A signed client ID survives Railway restarts without requiring a database.
    """
    now = int(time.time())
    return jwt.encode(
        {
            "typ": "client",
            "iss": issuer_url(),
            "redirect_uris": redirect_uris,
            "iat": now,
            "exp": now + REFRESH_TTL_SECONDS,
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def verify_client_id(client_id: str) -> dict | None:
    claims = _jwt_decode(client_id)
    if not claims or claims.get("typ") != "client":
        return None
    redirects = claims.get("redirect_uris")
    return claims if isinstance(redirects, list) and redirects else None


def valid_redirect_uri(uri: str) -> bool:
    """Accept secure web callbacks, plus loopback HTTP callbacks for local tools."""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    if parsed.fragment or parsed.username or parsed.password or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def make_auth_code(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    scope: str = SUPPORTED_SCOPE,
    resource: str | None = None,
) -> str:
    now = int(time.time())
    audience = _resource(resource)
    if not audience:
        raise ValueError("invalid resource")
    payload = {
        "typ": "code",
        "iss": issuer_url(),
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "scope": scope,
        "resource": audience,
        "iat": now,
        "exp": now + AUTH_CODE_TTL_SECONDS,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_auth_code(code: str) -> dict | None:
    claims = _jwt_decode(code)
    if not claims or claims.get("typ") != "code" or not claims.get("jti"):
        return None
    if claims["jti"] in _consumed_codes:
        return None
    return claims


def make_refresh_token(
    client_id: str, scope: str = SUPPORTED_SCOPE, resource: str | None = None
) -> str:
    now = int(time.time())
    audience = _resource(resource)
    if not audience:
        raise ValueError("invalid resource")
    payload = {
        "typ": "refresh",
        "iss": issuer_url(),
        "client_id": client_id,
        "scope": scope,
        "resource": audience,
        "iat": now,
        "exp": now + REFRESH_TTL_SECONDS,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_refresh_token(token: str) -> dict | None:
    claims = _jwt_decode(token)
    if not claims or claims.get("typ") != "refresh" or not claims.get("jti"):
        return None
    if claims["jti"] in _consumed_refresh_tokens:
        return None
    return claims


def _consume(cache: dict[str, int], claims: dict) -> None:
    now = int(time.time())
    for token_id, expiry in list(cache.items()):
        if expiry <= now:
            cache.pop(token_id, None)
    cache[claims["jti"]] = int(claims["exp"])


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    if not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return secrets.compare_digest(expected, code_challenge)


def _valid_scope(scope: str) -> bool:
    requested = set(scope.split())
    return SUPPORTED_SCOPE in requested and requested <= SUPPORTED_SCOPES


def _validate_authorization_fields(fields: dict[str, str]) -> str | None:
    client = verify_client_id(fields["client_id"])
    if not client:
        return "Unknown or expired client registration."
    if fields["redirect_uri"] not in client["redirect_uris"]:
        return "The callback URL does not match the registered client."
    if fields["response_type"] != "code":
        return "Only the authorization code flow is supported."
    if fields["code_challenge_method"] != "S256" or not fields["code_challenge"]:
        return "PKCE with the S256 method is required."
    if not _valid_scope(fields["scope"]):
        return "The requested scope is not supported."
    if not _resource(fields["resource"]):
        return "The requested MCP resource is not supported."
    return None


class JwtTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        claims = verify_access_token(token)
        if not claims:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims.get("scope", "").split(),
            expires_at=claims.get("exp"),
            resource=claims.get("aud"),
        )


def _login_page(fields: dict[str, str], error: str = "") -> str:
    hidden = "".join(
        f'<input type="hidden" name="{_html.escape(k)}" value="{_html.escape(v)}">'
        for k, v in fields.items()
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
    <p class="sub">Enter your private access password to approve this connection.</p>
    {error_html}
    <input type="password" name="password" placeholder="Password" autofocus required>
    {hidden}
    <button type="submit">Authorize</button>
  </form>
</body></html>"""


def _metadata() -> dict:
    return {
        "issuer": issuer_url(),
        "authorization_endpoint": f"{PUBLIC_URL}/authorize",
        "token_endpoint": f"{PUBLIC_URL}/token",
        "registration_endpoint": f"{PUBLIC_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SUPPORTED_SCOPE, OFFLINE_SCOPE],
        "authorization_response_iss_parameter_supported": True,
    }


def _authorization_fields(values) -> dict[str, str]:
    return {
        "client_id": str(values.get("client_id", "")),
        "redirect_uri": str(values.get("redirect_uri", "")),
        "state": str(values.get("state", "")),
        "code_challenge": str(values.get("code_challenge", "")),
        "code_challenge_method": str(values.get("code_challenge_method", "")),
        "response_type": str(values.get("response_type", "")),
        "scope": str(values.get("scope", SUPPORTED_SCOPE)),
        "resource": str(values.get("resource", resource_url())),
    }


def _oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def install_oauth_routes(app):
    """Attach the OAuth authorization-server endpoints to a FastMCP app."""

    @app.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def authorization_server_metadata(request: Request):
        return JSONResponse(_metadata())

    @app.custom_route("/register", methods=["POST"])
    async def register(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _oauth_error("invalid_client_metadata", "The request body must be JSON.")
        if not isinstance(body, dict):
            return _oauth_error("invalid_client_metadata", "The request body must be an object.")

        redirect_uris = body.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or not all(isinstance(uri, str) and valid_redirect_uri(uri) for uri in redirect_uris)
        ):
            return _oauth_error(
                "invalid_redirect_uri",
                "Provide at least one HTTPS redirect URI "
                "(loopback HTTP is allowed for local development).",
            )
        response_types = body.get("response_types", ["code"])
        if not isinstance(response_types, list) or any(
            value not in {"code"} for value in response_types
        ):
            return _oauth_error("invalid_client_metadata", "Only response_type=code is supported.")
        supported_grants = {"authorization_code", "refresh_token"}
        grants = body.get("grant_types", ["authorization_code", "refresh_token"])
        if not isinstance(grants, list) or any(value not in supported_grants for value in grants):
            return _oauth_error("invalid_client_metadata", "Unsupported grant type.")
        if body.get("token_endpoint_auth_method", "none") != "none":
            return _oauth_error(
                "invalid_client_metadata",
                "Only public clients with token_endpoint_auth_method=none are supported.",
            )

        now = int(time.time())
        return JSONResponse(
            {
                "client_id": make_client_id(redirect_uris),
                "client_id_issued_at": now,
                "token_endpoint_auth_method": "none",
                "grant_types": grants,
                "response_types": response_types,
                "redirect_uris": redirect_uris,
                "client_name": body.get("client_name", "MCP client"),
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.custom_route("/authorize", methods=["GET"])
    async def authorize_form(request: Request):
        fields = _authorization_fields(request.query_params)
        error = _validate_authorization_fields(fields)
        if error:
            return _oauth_error("invalid_request", error)
        return HTMLResponse(_login_page(fields), headers={"Cache-Control": "no-store"})

    @app.custom_route("/authorize", methods=["POST"])
    async def authorize_submit(request: Request):
        form = await request.form()
        fields = _authorization_fields(form)
        error = _validate_authorization_fields(fields)
        if error:
            return _oauth_error("invalid_request", error)

        password = str(form.get("password", ""))
        if not (OWNER_PASSWORD and secrets.compare_digest(password, OWNER_PASSWORD)):
            return HTMLResponse(
                _login_page(fields, error="Incorrect password."),
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )

        redirect_uri = fields["redirect_uri"]
        code = make_auth_code(
            fields["client_id"],
            redirect_uri,
            fields["code_challenge"],
            fields["scope"],
            fields["resource"],
        )
        params = {"code": code, "iss": issuer_url()}
        if fields["state"]:
            params["state"] = fields["state"]
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    @app.custom_route("/token", methods=["POST"])
    async def token(request: Request):
        form = await request.form()
        grant_type = str(form.get("grant_type", ""))

        if grant_type == "authorization_code":
            claims = verify_auth_code(str(form.get("code", "")))
            if not claims:
                return _oauth_error(
                    "invalid_grant", "The authorization code is invalid or already used."
                )
            if not secrets.compare_digest(str(form.get("client_id", "")), claims["client_id"]):
                return _oauth_error(
                    "invalid_grant", "The client ID does not match the authorization code."
                )
            if str(form.get("redirect_uri", "")) != claims["redirect_uri"]:
                return _oauth_error(
                    "invalid_grant", "The redirect URI does not match the authorization code."
                )
            if not verify_pkce(str(form.get("code_verifier", "")), claims["code_challenge"]):
                return _oauth_error("invalid_grant", "PKCE verification failed.")
            resource = _resource(str(form.get("resource", claims["resource"])))
            if not resource or resource != claims["resource"]:
                return _oauth_error("invalid_target", "The requested MCP resource does not match.")
            _consume(_consumed_codes, claims)
        elif grant_type == "refresh_token":
            claims = verify_refresh_token(str(form.get("refresh_token", "")))
            if not claims:
                return _oauth_error(
                    "invalid_grant", "The refresh token is invalid or already used."
                )
            if not secrets.compare_digest(str(form.get("client_id", "")), claims["client_id"]):
                return _oauth_error(
                    "invalid_grant", "The client ID does not match the refresh token."
                )
            resource = _resource(str(form.get("resource", claims["resource"])))
            if not resource or resource != claims["resource"]:
                return _oauth_error("invalid_target", "The requested MCP resource does not match.")
            _consume(_consumed_refresh_tokens, claims)
        else:
            return _oauth_error(
                "unsupported_grant_type", "Use authorization_code or refresh_token."
            )

        client_id = claims["client_id"]
        scope = claims.get("scope", SUPPORTED_SCOPE)
        return JSONResponse(
            {
                "access_token": make_access_token(client_id, scope, resource),
                "token_type": "Bearer",
                "expires_in": TOKEN_TTL_SECONDS,
                "refresh_token": make_refresh_token(client_id, scope, resource),
                "scope": scope,
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
