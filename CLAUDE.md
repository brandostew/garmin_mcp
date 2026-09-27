# Garmin + Cronometer MCP Server — Maintenance Playbook

## What This Is
A self-hosted MCP server running on Railway that connects Garmin and Cronometer
data to Claude. Built from scratch with a hand-rolled OAuth 2.1 gate.

## Key Info
- **Railway URL:** `https://garminmcp-production-3976.up.railway.app/mcp`
- **Project folder:** `~/Coding Fun/garmin_mcp`
- **GitHub repo:** `brandostew/garmin_mcp`
- **Python version:** 3.14
- **Railway service:** garmin_mcp
- **Railway Volume mount path:** `/root/.local/share/cronometer-mcp`

## Architecture
- `src/garmin_mcp/__init__.py` — main entry point, wires everything together
- `src/garmin_mcp/auth_server.py` — hand-built OAuth 2.1 gate (discovery, register, authorize, token)
- `src/garmin_mcp/cronometer_tools.py` — Cronometer read/write tools (mobile API)
- `src/garmin_mcp/cronometer_gwt_tools.py` — Cronometer macro target tools (GWT/web API)
- `Dockerfile` — Railway build config, must use `python:3.14-slim`
- `pyproject.toml` — dependencies, mcp must stay pinned to `<2.0.0`

## Railway Environment Variables
- `GARMIN_TOKENS_B64` — base64-encoded Garmin OAuth token bundle
- `CRONOMETER_USERNAME` — Cronometer email
- `CRONOMETER_PASSWORD` — Cronometer password
- `MCP_TRANSPORT` — set to `streamable-http`
- `PUBLIC_URL` — `https://garminmcp-production-3976.up.railway.app`
- `OWNER_PASSWORD` — password used to connect Claude via OAuth login page
- `JWT_SECRET` — random string used to sign OAuth tokens
- `GARMIN_DISABLED_TOOLS` — comma-separated denylist of write/delete tools

## Healthy Startup Log (all 4 lines required)


## Known Failure Modes & Fixes

### 1. Garmin token expired
**Symptom:** `ERROR: OAuth tokens not found and no interactive terminal available`
**Fix:**
```bash
# Step 1 — re-authenticate on Mac
uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth

# Step 2 — copy fresh token to clipboard
tar czf - -C ~ .garminconnect | base64 | tr -d '\n' | pbcopy

# Step 3 — paste into Railway Variables → GARMIN_TOKENS_B64
# Railway will auto-redeploy
```
**Frequency:** Every ~6 months (or sooner if Railway redeploys wipe it)

### 2. Cronometer GWT session expired
**Symptom:** `GWT-RPC call failed... NotLoggedInException` when trying to update macro targets
**Fix:**
```bash
# Step 1 — clear stale cookie via Railway Console tab
rm -f /root/.local/share/cronometer-mcp/.session_cookies

# Step 2 — Railway → Deployments → latest → three dots → Redeploy
```
**Frequency:** Every 1-2 weeks

### 3. mcp library auto-updated to v2
**Symptom:** `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`
**Fix:** Ensure `pyproject.toml` has `"mcp>=1.23.0,<2.0.0"` — this is already pinned.
If it breaks again, check that the pin is still in place and run `uv sync`.

### 4. Cronometer OAuth token expired (Claude connector)
**Symptom:** Claude shows "Connect to Garmin" login page
**Fix:** Enter OWNER_PASSWORD on the login page and click Authorize.
**Frequency:** Every 30 days

### 5. Everything down at once
Usually means the Garmin token expired AND triggered a cascade. Fix Garmin token
first (failure mode #1), then check if Cronometer GWT also needs a reset (#2).

## Connecting Claude
- Connector URL: `https://garminmcp-production-3976.up.railway.app/mcp`
- Added via claude.ai → Settings → Connectors → Add custom connector
- Login page uses OWNER_PASSWORD (not Garmin or Cronometer password)
- Syncs automatically to Claude mobile app once added on web

## Key Dependencies
- `garminconnect` — unofficial Garmin API wrapper
- `cronometer-api-mcp` — Cronometer mobile API (stable, read/write food diary)
- `cronometer-mcp` — Cronometer GWT/web API (macro targets only, session expires frequently)
- `mcp>=1.23.0,<2.0.0` — MUST stay pinned below v2 (v2 renamed FastMCP)
- `pyjwt` — JWT signing for OAuth tokens
- `httpcore>=1.0.9` — pinned to fix Python 3.14 compatibility bug