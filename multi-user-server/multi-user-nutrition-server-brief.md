# Multi-user nutrition server — design and Claude Code build brief (v2)

## How to use this file
1. Make an empty folder (for example `nutrition-server`) and put this file in it.
2. Open Claude Code in that folder.
3. Paste the prompt in section 10 and press enter.
4. Claude Code will stop at checkpoints and wait for you. Say "go" when the plain-English summary makes sense.

What changed from v1: people now connect their own Cronometer and Garmin accounts through a web page on the server, including typing a Garmin two-factor code. Brandon never handles anyone's passwords or codes. See sections 3 and 8.

---

## 1. What we are building (plain English)
One small web server that Claude can connect to. Several people (Brandon, his wife, later a few friends) each connect their **own** Claude account to it. The server holds each person's food preferences, training week, and nutrition-app connections, and lets Claude read their food log and set their daily macro targets.

The most important property: **a person's Claude can only ever see that person's data.** Everything else is secondary.

## 2. How people are kept apart (the rules)
These are non-negotiable. Claude Code must follow every one and write tests proving them.

1. **Identity comes from the key, nothing else.** Each person has their own long random key. Claude sends it with every request. The server looks it up and learns "this is user #7". Nothing the AI model writes can change who the server thinks is calling.
2. **No tool accepts a user name, email or ID.** Tools only take things like a date or a calorie number. Claude therefore cannot ask for "Brandon's diary" even by accident.
3. **One gate.** A single function, `current_user()`, is the only way any code gets a user ID. It reads the identity set by the key check for this one request. Nothing reads a user ID from tool inputs.
4. **Every database lookup is limited to that user.** All data access goes through helper functions that require the current user object. Add Postgres row-level security as a second lock if practical.
5. **No shared memory between users.** No global login session, no global cache, no "last user" variable. A fresh connection to the nutrition app is built for each request from that user's own stored connection. Any cache key must start with the user ID.
6. **Secrets are sealed.** Keys are stored only as a hash. Source connections (tokens, and passwords where unavoidable) are encrypted at rest, decrypted only inside a call for that same user, never written to logs, never returned by any tool.
7. **Everything is logged by who.** Each call writes: user ID, tool name, time, success or failure. No payloads containing secrets.
8. **Invite-only.** Only Brandon can create invite links, using an admin script. Strangers cannot sign up. Admin commands: `create-invite`, `rotate-key`, `disable-user`, `delete-user` (removes all of that user's data). Brandon never sees anyone's source passwords or codes.

## 3. Self-service onboarding (users connect their own accounts)
This is a small website served by the same Railway app. It is where people type their Cronometer and Garmin logins and, when asked, their two-factor code.

### The flow
1. Brandon runs `create-invite "Name"`. It prints a one-time link that works once and expires in 7 days.
2. The person opens the link, chooses a password for **this service** (stored only as a strong hash), and is signed in with a secure session cookie.
3. A "Connect your apps" page lists each source with a Connect button.
   - **Cronometer**: email and password form. If Cronometer asks for a 6-digit authenticator code, the page asks for it and finishes the login.
   - **Garmin**: email and password form. If Garmin says a code is needed, the server pauses the login, shows "Enter the code Garmin sent you", and finishes the login when the person submits it.
4. Each source shows "Connected" plus a small Test button (for example, fetch yesterday's data).
5. A final page shows the person's private Claude connector link with three plain steps to add it in Claude. They can regenerate it (this rotates the key and kills the old link).
6. If a connection later stops working, tool replies say "Please reconnect at <link>" and the person repeats step 3. Two-factor codes are only needed at connect and reconnect, not on every request.

### Rules for the website
- Passwords and codes are typed only on this site. They are never sent through Claude, never in a URL, never logged.
- **Paused logins (the two-factor step) are the riskiest new piece.** Each one gets a random ID and is bound to the signed-in user **and** their session. It can only be completed by that same session, expires after 10 minutes, allows at most 3 code tries, and is deleted when used. A different signed-in user must never be able to submit a code to someone else's paused login.
- **Garmin: keep tokens, not the password.** After a successful login, store the tokens, then delete the password. The Garmin library keeps tokens fresh on its own, and a full re-login is only needed if they expire or are revoked.
- **Cronometer: check whether a session token can be stored instead of the password.** If not, store the password encrypted.
- **Be gentle with the source sites.** One login attempt per click, no automatic retries, at most 3 attempts per source per user per hour. If a source answers "too many requests" or blocks the login, stop and show "Try again in an hour". Hammering a login can lock a real account for days.
- Website basics: HTTPS only, secure cookies (HttpOnly, Secure, SameSite), protection against forged form posts (CSRF), rate limits on sign-in, standard security headers.

### Honest note for users
Whoever runs the server holds the encryption key in Railway, so technically could decrypt stored connections. Brandon should tell each person that plainly, never read their data, and log admin actions.

## 4. Pieces
- **Server and website**: Python, official MCP Python SDK (FastMCP) plus a small web framework, HTTPS only, deployed on Railway.
- **Database**: Postgres on Railway.
- **Key check**: runs before any tool. Missing, wrong or disabled key means an error and no data.
- **Sources**: plug-in modules. Each declares what it needs (fields, whether it may ask for a code) so the onboarding page is generic. Cronometer first (port from Brandon's existing code), Garmin second (Python library `garminconnect`, which supports a "needs code, then resume with code" web flow).
- Both sources use unofficial routes, so keep each behind a clean interface that can be swapped or faked in tests.

## 5. Data (tables)
Every table except `users` and `invites` has a `user_id` column.

| Table | What it holds |
|---|---|
| `users` | id, display name, service password hash, key hash, active yes/no, created date |
| `invites` | one-time code hash, name, expires, used yes/no |
| `web_sessions` | session id, user_id, expires |
| `pending_logins` | random id, user_id, session id, source, paused login state, expires, tries left |
| `credentials` | user_id, source name, encrypted tokens or login |
| `profile` | user_id, food likes, dislikes, dietary notes, goals |
| `training_week` | user_id, week start date, list of sessions (day, name, details) typed or pasted in |
| `audit_log` | user_id, tool or admin action, time, ok or error |

## 6. Tools in v1 (what Claude can do)
- `whoami` — returns only the caller's display name. Easy visual check that isolation works.
- `get_profile`, `update_profile`
- `save_training_week`, `get_training_week`
- `get_food_diary(date)`
- `get_daily_targets(date)`, `set_daily_targets(date, calories, protein, carbs, fat)`
- After onboarding works: Garmin tools (recent activities, sleep, training readiness), custom foods, food search, weekly summaries.
- When a source is disconnected or expired, every related tool returns the same plain message: "Please reconnect at <link>".

## 7. Known risks (Claude Code should design around these)
- **Garmin may block or rate-limit logins from cloud servers.** Community reports describe shared cloud IPs getting "too many requests" errors, and one account blocked for over 48 hours after a few failed tries. Garmin has also changed its login before and broken libraries (reports in March 2026). Expect to upgrade the library from time to time.
- **Fallback if Garmin blocks Railway:** tokens created once on the user's own computer can be uploaded to their account. This is a manual escape hatch and not the main path.
- **Cronometer two-factor is unverified.** Cronometer supports authenticator-app codes, but the community clients do not document handling them. Test early with a real account that has 2FA on. If it cannot work, say so plainly.
- **Connecting through Claude's own sign-in button (OAuth) is not first choice.** Several people report custom OAuth connectors failing on claude.ai even when the server looks correct. Use the private-link method in section 9 first.
- Everything depends on unofficial access to both apps, so keep the group small and tell people it can break.

## 8. Build order (stop and wait for approval at each checkpoint)
0. **Read first.** Look at the existing Cronometer code. Explain in plain English how it logs in and what it can do. Propose the plan. **Wait for approval.**
1. **Skeleton.** Project setup, database tables, admin script. Run it locally.
2. **Key check + `whoami` + isolation tests** using fake data and two fake users. **Checkpoint: show me the tests passing.**
3. **Profile and training-week tools**, with tests.
4. **Cronometer source** behind a fake version first, then the real one.
5. **Onboarding website**: invites, sign-in, Connect page, paused-login handling. Test the two-factor step first with a **fake source that demands a code**. **Checkpoint: show the paused-login tests passing.**
6. **Garmin source** with real two-factor, tested on Brandon's own account only.
7. **Deploy to Railway.** Ask before deploying anything or using real credentials.
8. **Connect my own Claude** and verify `whoami` says my name. Then invite a second person through the website and verify theirs says theirs.

## 9. Connecting Claude (after deploy)
v1: the final onboarding page shows a private connector link. The person pastes it into Claude (Settings, Connectors, Add custom connector). The key lives inside the link, so treat the link like a password and make sure the server never logs it. If Claude's connector form will not accept a link like this, fall back to the request-header method described in Anthropic's help center (a fixed `Authorization: Bearer <key>` header). Claude Code should test which one works on a free account before building more on top.
Later: replace this with Claude's built-in sign-in flow (OAuth) so the person never copies anything, once the rest is solid.
Each person has exactly one connector, which fits the Free plan limit of one.

## 10. Tests that must pass before any deploy
1. Two users with different fake data: each key returns only its own data from every tool.
2. Missing, wrong, or disabled key returns an error and no data.
3. Tools reject or ignore any extra user ID, email or name argument.
4. Concurrency: about 50 interleaved calls alternating between two users; every response matches its caller.
5. `delete-user` removes every row for that user.
6. Logs contain no keys, passwords, two-factor codes, connector links, or decrypted logins (search the logs).
7. Paused login: user A starts one; user B (signed in) cannot complete it; it fails after 10 minutes; it fails after 3 wrong codes; it cannot be used twice.
8. Invites: work once, expire, and cannot be reused.
9. Login limit: the 4th attempt in an hour is refused without contacting the source.
10. After a successful Garmin connect, no Garmin password remains in the database.

## 11. Prompt to paste into Claude Code

```
Read the file multi-user-nutrition-server-brief.md in this folder. It is the full spec.

I am not a programmer. Before each step, explain in plain language what you are about to do. Keep changes small. Run the tests after each step and show me the results in plain language.

Goal: build the multi-user MCP server described in the brief, including the self-service onboarding website where each person types their own Cronometer and Garmin logins and, when asked, a two-factor code. Python, FastMCP, Postgres, deployed on Railway. One key per user, invite-only signup, strict separation of each user's data as described in section 2. Follow the build order in section 8 and stop at each checkpoint.

My existing working Cronometer/Garmin MCP code is here: [PATH TO EXISTING CODE — fill in before running]. Start with step 0: read it, explain how the Cronometer part works, and wait for my approval before writing anything.

Rules: never put real passwords, keys or codes in code or logs. Do not deploy, create cloud resources, or use real credentials without asking me first. Be gentle with the Garmin and Cronometer logins: no automatic retries. If a rule in the brief conflicts with something that would be easier, keep the rule and tell me.
```

## 12. Deferred on purpose
Claude's built-in sign-in flow (OAuth), a web admin screen, Life Time plan import, and any app of our own. Add these only after the family pilot works.
