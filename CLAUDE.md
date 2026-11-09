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
- `CRONOMETER_DATA_DIR` — `/root/.local/share/cronometer-mcp` (points GWT session cookies at the Volume)

## Healthy Startup Log (all 4 lines required)
```
Restored Garmin tokens from GARMIN_TOKENS_B64.   (or: Using saved Garmin tokens from '...')
Garmin Connect client initialized successfully.
Cronometer client initialized successfully.
Cronometer GWT client (macro tools) initialized successfully.
```
Also expected on Railway (not counted above):
```
OAuth gate enabled.
Tool filter: denylist of N tool(s).
```
- Missing `OAuth gate enabled.` → one of `PUBLIC_URL`, `OWNER_PASSWORD`, `JWT_SECRET` is unset, so the server is running **without auth**. Fix immediately.
- `Cronometer GWT login failed: ...` → see failure mode #2.
- `Tool filter: warning — name(s) not found and ignored: ...` → typo in `GARMIN_DISABLED_TOOLS`.


## Known Failure Modes & Fixes

### 1. Garmin token expired
**Symptom:** `ERROR: OAuth tokens not found and no interactive terminal available`, followed by
`Garmin tools disabled (Cronometer tools still available)`. The server stays up, but no Garmin tools show in Claude.
The `Garmin token login failed: ...` line just above shows the underlying reason.
**Why it happened before:** Garmin rotates the refresh token on every refresh, so the `GARMIN_TOKENS_B64` blob goes
stale while the server runs. The server now keeps the live tokens on the Volume
(`/root/.local/share/cronometer-mcp/garminconnect`) and only replaces them when `GARMIN_TOKENS_B64` itself changes,
so redeploys no longer break Garmin. Startup log shows `Using saved Garmin tokens from ...` in that case.
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
**Now self-healing:** the GWT tools catch `NotLoggedInException`, delete the stale cookie, log in again
and retry once. The log shows `Cronometer GWT session expired; logged in again.` when this happens.
**Symptom (if self-healing fails):** `GWT-RPC call failed... NotLoggedInException` when trying to update macro targets
**Manual fix:**
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

### 4. Claude connector OAuth token expired
**Symptom:** Claude shows "Connect to Garmin" login page
**Fix:** Enter OWNER_PASSWORD on the login page and click Authorize. If no login
page appears, disconnect and reconnect the connector in claude.ai → Settings → Connectors.
**Frequency:** Every 30 days

### 5. Everything down at once
Usually means the Garmin token expired AND triggered a cascade. Fix Garmin token
first (failure mode #1), then check if Cronometer GWT also needs a reset (#2).

## Recurring Maintenance
| Every | What expires | Fix |
|---|---|---|
| ~6 months | Garmin token | Failure mode #1 |
| 1–2 weeks | Cronometer GWT session | Automatic (manual fallback: failure mode #2) |
| 30 days | Claude connector OAuth token | Failure mode #4 |

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

## Build History
1. **Garmin local setup** — `uv` + `garmin-mcp-auth`, added to Claude Desktop via `claude_desktop_config.json`.
2. **Railway hosting** — forked `Taxuspt/garmin_mcp`; `MCP_TRANSPORT` env var (defaults to `stdio` so desktop still works);
   `_restore_tokens_from_env()` unpacks `GARMIN_TOKENS_B64` for headless login; `python:3.14-slim` Dockerfile; Volume added.
3. **OAuth 2.1 gate** (`auth_server.py`) — discovery, dynamic client registration, login page, PKCE S256 token exchange,
   refresh tokens, `JwtTokenVerifier`. `AUTH_ENABLED` only when `PUBLIC_URL`, `OWNER_PASSWORD`, `JWT_SECRET` are all set.
4. **Cronometer mobile API tools** (`cronometer_tools.py`, `rwestergren/cronometer-api-mcp`) — diary, nutrients, targets,
   scores, food search/details, macro schedules/templates, fasting; writes: add serving, copy day, custom food, mark day complete.
   `delete_cronometer_entries` is set to **Never** in the Claude connector's per-tool permissions.
5. **Cronometer GWT macro tools** (`cronometer_gwt_tools.py`, `cphoskins/cronometer-mcp`) — read daily targets, templates,
   weekly schedule; write one-off daily targets, save/delete named templates, assign weekly schedule.
   Cookies persist on the Volume via `CRONOMETER_DATA_DIR`.
6. **mcp pin** — mcp v2 renamed `FastMCP` → `MCPServer`; pinned `mcp>=1.23.0,<2.0.0`.
7. **This playbook** (`CLAUDE.md`).
8. **Resilience** — server stays up when Garmin login fails; GWT tools re-login automatically on `NotLoggedInException`.

## Planned
- InBody scale integration (read-only, `rwestergren/inbody-api-mcp`)
- Browser-based Garmin token refresh (removes the 6-month manual step)
