"""Unit tests for the hosted OAuth 2.1 server."""

import base64
import hashlib

import jwt
import pytest
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.testclient import TestClient
from urllib.parse import parse_qs, urlsplit

from garmin_mcp import auth_server


@pytest.fixture(autouse=True)
def oauth_config(monkeypatch):
    monkeypatch.setattr(auth_server, "PUBLIC_URL", "https://garmin.example")
    monkeypatch.setattr(auth_server, "OWNER_PASSWORD", "test-password")
    monkeypatch.setattr(auth_server, "JWT_SECRET", "test-secret-that-is-long-enough")
    auth_server._consumed_codes.clear()
    auth_server._consumed_refresh_tokens.clear()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def test_access_token_is_bound_to_mcp_resource():
    token = auth_server.make_access_token("client-1")
    claims = auth_server.verify_access_token(token)
    assert claims["aud"] == "https://garmin.example/mcp"
    assert claims["iss"] == "https://garmin.example/"


def test_access_token_for_another_audience_is_rejected():
    now = auth_server.time.time()
    token = jwt.encode(
        {
            "iss": "https://garmin.example/",
            "sub": "owner",
            "aud": "https://other.example",
            "client_id": "client-1",
            "iat": int(now),
            "exp": int(now) + 300,
        },
        auth_server.JWT_SECRET,
        algorithm="HS256",
    )
    assert auth_server.verify_access_token(token) is None


def test_registered_client_is_bound_to_exact_redirect_uri():
    client_id = auth_server.make_client_id(
        ["https://chatgpt.com/connector_platform_oauth_redirect"]
    )
    fields = {
        "client_id": client_id,
        "redirect_uri": "https://attacker.example/callback",
        "state": "state",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "response_type": "code",
        "scope": "garmin",
        "resource": "https://garmin.example/mcp",
    }
    assert "callback URL" in auth_server._validate_authorization_fields(fields)


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("https://chatgpt.com/connector_platform_oauth_redirect", True),
        ("http://127.0.0.1:8765/callback", True),
        ("http://localhost:8765/callback", True),
        ("http://attacker.example/callback", False),
        ("javascript:alert(1)", False),
        ("https://example.com/callback#fragment", False),
    ],
)
def test_redirect_uri_validation(uri, expected):
    assert auth_server.valid_redirect_uri(uri) is expected


def test_pkce_s256_and_one_time_authorization_codes():
    verifier = "a-valid-pkce-verifier-with-sufficient-entropy-1234567890"
    client_id = auth_server.make_client_id(["https://chatgpt.com/callback"])
    code = auth_server.make_auth_code(
        client_id,
        "https://chatgpt.com/callback",
        _challenge(verifier),
    )
    claims = auth_server.verify_auth_code(code)
    assert claims is not None
    assert auth_server.verify_pkce(verifier, claims["code_challenge"])
    auth_server._consume(auth_server._consumed_codes, claims)
    assert auth_server.verify_auth_code(code) is None


def test_metadata_advertises_oauth_21_features():
    metadata = auth_server._metadata()
    assert metadata["issuer"] == "https://garmin.example/"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["registration_endpoint"] == "https://garmin.example/register"
    assert metadata["scopes_supported"] == ["garmin", "offline_access"]
    assert metadata["authorization_response_iss_parameter_supported"] is True


def _test_app() -> FastMCP:
    app = FastMCP(
        "OAuth test",
        token_verifier=auth_server.JwtTokenVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(auth_server.issuer_url()),
            resource_server_url=AnyHttpUrl(auth_server.resource_url()),
            required_scopes=[auth_server.SUPPORTED_SCOPE],
        ),
    )
    auth_server.install_oauth_routes(app)
    return app


def test_protected_resource_metadata_names_the_mcp_endpoint():
    with TestClient(_test_app().streamable_http_app()) as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    assert response.json()["resource"] == "https://garmin.example/mcp"
    assert response.json()["authorization_servers"] == ["https://garmin.example/"]


def test_dynamic_registration_and_authorization_code_flow():
    verifier = "another-valid-pkce-verifier-with-sufficient-entropy-123456"
    callback = "https://chatgpt.com/connector_platform_oauth_redirect"
    resource = "https://garmin.example/mcp"

    with TestClient(_test_app().streamable_http_app()) as client:
        registration = client.post(
            "/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": [callback],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]

        authorization = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "state": "state-123",
                "scope": "garmin offline_access",
                "resource": resource,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
            },
        )
        assert authorization.status_code == 200

        approval = client.post(
            "/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "state": "state-123",
                "scope": "garmin offline_access",
                "resource": resource,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "password": "test-password",
            },
            follow_redirects=False,
        )
        assert approval.status_code == 302
        query = parse_qs(urlsplit(approval.headers["location"]).query)
        assert query["state"] == ["state-123"]
        assert query["iss"] == ["https://garmin.example/"]

        exchange = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": callback,
                "resource": resource,
                "code": query["code"][0],
                "code_verifier": verifier,
            },
        )
        assert exchange.status_code == 200
        token_body = exchange.json()
        claims = auth_server.verify_access_token(token_body["access_token"])
        assert claims["aud"] == resource
        assert claims["scope"] == "garmin offline_access"
        assert token_body["refresh_token"]

        refresh = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "resource": resource,
                "refresh_token": token_body["refresh_token"],
            },
        )
        assert refresh.status_code == 200
        assert refresh.json()["refresh_token"] != token_body["refresh_token"]

        refresh_replay = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "resource": resource,
                "refresh_token": token_body["refresh_token"],
            },
        )
        assert refresh_replay.status_code == 400
        assert refresh_replay.json()["error"] == "invalid_grant"

        replay = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": callback,
                "resource": resource,
                "code": query["code"][0],
                "code_verifier": verifier,
            },
        )
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"


def test_resource_bound_token_opens_the_streamable_http_endpoint():
    request_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ChatGPT test", "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(
        _test_app().streamable_http_app(), base_url="http://localhost:8000"
    ) as client:
        unauthorized = client.post("/mcp", json=request_body, headers=headers)
        assert unauthorized.status_code == 401
        assert (
            'resource_metadata="https://garmin.example/'
            '.well-known/oauth-protected-resource/mcp"'
            in unauthorized.headers["www-authenticate"]
        )

        headers["Authorization"] = (
            "Bearer " + auth_server.make_access_token("registered-client")
        )
        authorized = client.post("/mcp", json=request_body, headers=headers)
        assert authorized.status_code == 200
        assert '"protocolVersion":"2025-06-18"' in authorized.text
