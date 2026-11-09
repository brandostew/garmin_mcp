"""The onboarding website: invites, sign-in, Connect page, two-factor step,
and the page that makes a person's private Claude connector link.

Rules from the brief, section 3:
- Passwords and codes are typed only here, never logged, never in a URL.
- A paused login (waiting for a two-factor code) belongs to one signed-in
  user AND one browser session, lasts 10 minutes, allows 3 tries, and is
  deleted when used.
- One source login attempt per click, at most 3 per source per user per hour.
"""

import hashlib
import hmac
import html
import os
from datetime import timedelta

from sqlalchemy import func, select
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from nutrition_server import db, sources
from nutrition_server.crypto import encryption_secret, seal, unseal
from nutrition_server.security import (
    hash_password,
    hash_secret,
    new_session_id,
    new_user_key,
    verify_password,
)

SESSION_COOKIE = "ns_session"
PRE_COOKIE = "ns_pre"  # anti-forgery cookie for forms shown before sign-in
SESSION_LIFETIME = timedelta(days=30)
PENDING_LIFETIME = timedelta(minutes=10)
PENDING_TRIES = 3
SOURCE_ATTEMPTS_PER_HOUR = 3
SIGNIN_FAILURES_ALLOWED = 5
SIGNIN_WINDOW = timedelta(minutes=15)
MIN_PASSWORD = 10


# --- Page layout -------------------------------------------------------------

_STYLE = """
body{font-family:-apple-system,system-ui,sans-serif;background:#0f1115;color:#e8e8e8;
margin:0;padding:24px 16px}main{max-width:520px;margin:0 auto}
.card{background:#1a1d24;border-radius:12px;padding:20px;margin-bottom:16px}
h1{font-size:22px}h2{font-size:17px;margin-top:0}label{display:block;margin:10px 0 4px}
input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #333;
background:#0f1115;color:#e8e8e8;font-size:16px}
button{margin-top:12px;padding:10px 16px;border:0;border-radius:8px;background:#4f7cff;
color:#fff;font-size:15px}.muted{color:#9aa0aa;font-size:14px}.ok{color:#5fd38d}
.err{color:#ff7a7a}code{word-break:break-all;background:#0f1115;padding:8px;
display:block;border-radius:8px}a{color:#8fb0ff}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
           f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>")
    return HTMLResponse(doc, status_code=status)


def _e(value) -> str:
    return html.escape(str(value))


def _public_url() -> str:
    return os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")


# --- Anti-forgery tokens -----------------------------------------------------


def _csrf_secret() -> bytes:
    return hashlib.sha256(b"csrf:" + encryption_secret().encode()).digest()


def _csrf_for(value: str) -> str:
    return hmac.new(_csrf_secret(), value.encode(), hashlib.sha256).hexdigest()


def _csrf_field(token: str) -> str:
    return f"<input type='hidden' name='csrf' value='{_e(token)}'>"


async def _form(request: Request, expected_csrf: str | None) -> dict | None:
    """Read a posted form; None if the anti-forgery token is missing or wrong."""
    form = await request.form()
    sent = form.get("csrf", "")
    if not expected_csrf or not hmac.compare_digest(str(sent), expected_csrf):
        return None
    return {k: str(v) for k, v in form.items()}


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, secure=True,
                        samesite="strict", path="/")


def _pre_token(request: Request) -> str:
    """Anti-forgery token for signed-out forms (double-submit cookie)."""
    pre = request.cookies.get(PRE_COOKIE)
    if not pre:
        pre = new_session_id()
        request.state.new_pre = pre
    return _csrf_for("pre:" + pre)


def _with_pre_cookie(request: Request, response: Response) -> Response:
    new_pre = getattr(request.state, "new_pre", None)
    if new_pre:
        _set_cookie(response, PRE_COOKIE, new_pre, max_age=3600)
    return response


# --- Signed-in sessions ------------------------------------------------------


class _SignedIn:
    def __init__(self, user: db.User, session_hash: str, raw_session: str):
        self.user = user
        self.session_hash = session_hash
        self.csrf = _csrf_for("session:" + raw_session)


def _signed_in(request: Request) -> _SignedIn | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    session_hash = hash_secret(raw)
    with db.open_session() as s:
        ws = s.get(db.WebSession, session_hash)
        if ws is None or db.as_utc(ws.expires_at) < db.utcnow():
            return None
        user = db.get_active_user(s, ws.user_id)
    return _SignedIn(user, session_hash, raw) if user else None


def _start_session(user_id: int, response: Response) -> None:
    raw = new_session_id()
    with db.open_session() as s:
        s.add(db.WebSession(id=hash_secret(raw), user_id=user_id,
                            expires_at=db.utcnow() + SESSION_LIFETIME))
        s.commit()
    _set_cookie(response, SESSION_COOKIE, raw, max_age=int(SESSION_LIFETIME.total_seconds()))
    response.delete_cookie(PRE_COOKIE, path="/")


def _to_signin() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# --- Invite --------------------------------------------------------------------


def _valid_invite(s, code: str) -> db.Invite | None:
    invite = s.scalar(select(db.Invite).where(db.Invite.code_hash == hash_secret(code)))
    if invite is None or invite.used or db.as_utc(invite.expires_at) < db.utcnow():
        return None
    return invite


async def invite(request: Request) -> Response:
    code = request.path_params["code"]
    with db.open_session() as s:
        inv = _valid_invite(s, code)
        name = inv.name if inv else None
    if name is None:
        if _signed_in(request) is not None:
            # e.g. the account was just created and the form was sent twice.
            return RedirectResponse("/connect", status_code=303)
        return _page("Invite not valid", "<div class='card'>This invite link has expired "
                     "or was already used. Ask for a new one.</div>", 404)
    expected = _csrf_for("invite:" + code)
    error = ""
    if request.method == "POST":
        form = await _form(request, expected)
        if form is None:
            return _page("Error", "<div class='card err'>Form expired. Go back and try again.</div>", 400)
        login_name = form.get("login_name", "").strip().lower()
        password = form.get("password", "")
        if not login_name or len(login_name) > 100:
            error = "Choose a sign-in name."
        elif len(password) < MIN_PASSWORD:
            error = f"Password must be at least {MIN_PASSWORD} characters."
        elif password != form.get("password2", ""):
            error = "The two passwords don't match."
        else:
            with db.open_session() as s:
                inv = _valid_invite(s, code)
                taken = s.scalar(select(db.User).where(db.User.login_name == login_name))
                if inv is None:
                    return _page("Invite not valid", "<div class='card'>This invite was "
                                 "already used.</div>", 404)
                if taken:
                    error = "That sign-in name is taken. Choose another."
                else:
                    inv.used = True
                    user = db.User(display_name=inv.name, login_name=login_name,
                                   password_hash=hash_password(password))
                    s.add(user)
                    s.commit()
                    db.record_audit(s, user.id, "web:accept-invite", True)
                    response = RedirectResponse("/connect", status_code=303)
                    _start_session(user.id, response)
                    return response
    body = (f"<div class='card'><p>Hi {_e(name)}. Choose how you'll sign in to this "
            f"service. This is not your Cronometer or Garmin password.</p>"
            f"{f'<p class=err>{_e(error)}</p>' if error else ''}"
            f"<form method='post'>{_csrf_field(expected)}"
            f"<label>Sign-in name</label><input name='login_name' autocomplete='username' required>"
            f"<label>Password (at least {MIN_PASSWORD} characters)</label>"
            f"<input type='password' name='password' autocomplete='new-password' required>"
            f"<label>Password again</label>"
            f"<input type='password' name='password2' autocomplete='new-password' required>"
            f"<button>Create my account</button></form></div>")
    return _page("Welcome", body)


# --- Sign in / out -------------------------------------------------------------


def _recent_failures(s, user_id: int) -> int:
    since = db.utcnow() - SIGNIN_WINDOW
    return s.scalar(select(func.count()).select_from(db.AuditLog).where(
        db.AuditLog.user_id == user_id, db.AuditLog.action == "web:signin",
        db.AuditLog.ok.is_(False), db.AuditLog.at >= since))


async def login(request: Request) -> Response:
    error = ""
    if request.method == "POST":
        pre = request.cookies.get(PRE_COOKIE, "")
        form = await _form(request, _csrf_for("pre:" + pre) if pre else None)
        if form is None:
            error = "Form expired. Please try again."
        else:
            login_name = form.get("login_name", "").strip().lower()
            with db.open_session() as s:
                user = s.scalar(select(db.User).where(db.User.login_name == login_name,
                                                      db.User.active.is_(True)))
                if user and _recent_failures(s, user.id) >= SIGNIN_FAILURES_ALLOWED:
                    error = "Too many wrong passwords. Try again in 15 minutes."
                elif user and verify_password(form.get("password", ""), user.password_hash):
                    db.record_audit(s, user.id, "web:signin", True)
                    response = RedirectResponse("/connect", status_code=303)
                    _start_session(user.id, response)
                    return response
                else:
                    if user:
                        db.record_audit(s, user.id, "web:signin", False)
                    error = "Wrong sign-in name or password."
    token = _pre_token(request)
    body = (f"<div class='card'>{f'<p class=err>{_e(error)}</p>' if error else ''}"
            f"<form method='post'>{_csrf_field(token)}"
            f"<label>Sign-in name</label><input name='login_name' autocomplete='username' required>"
            f"<label>Password</label><input type='password' name='password' "
            f"autocomplete='current-password' required><button>Sign in</button></form></div>")
    return _with_pre_cookie(request, _page("Sign in", body))


async def logout(request: Request) -> Response:
    me = _signed_in(request)
    if me is None:
        return _to_signin()
    if await _form(request, me.csrf) is not None:
        with db.open_session() as s:
            ws = s.get(db.WebSession, me.session_hash)
            if ws:
                s.delete(ws)
                s.commit()
    response = _to_signin()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --- Connect page ------------------------------------------------------------------


def _attempts_last_hour(s, user_id: int, source: str) -> int:
    since = db.utcnow() - timedelta(hours=1)
    return s.scalar(select(func.count()).select_from(db.AuditLog).where(
        db.AuditLog.user_id == user_id, db.AuditLog.action == f"connect:{source}",
        db.AuditLog.at >= since))


def _connect_page(me: _SignedIn, message: str = "", status: int = 200) -> Response:
    with db.open_session() as s:
        connected = {c.source for c in s.scalars(
            select(db.Credential).where(db.Credential.user_id == me.user.id))}
    cards = []
    for source, make in sources.CONNECTORS.items():
        connector = make()
        if source in connected:
            inner = (f"<p class='ok'>Connected</p>"
                     f"<form method='post' action='/connect/{_e(source)}/test'>"
                     f"{_csrf_field(me.csrf)}<button>Test</button></form>"
                     f"<p class='muted'>To change the login, connect again below.</p>")
        else:
            inner = "<p class='muted'>Not connected</p>"
        fields = "".join(
            f"<label>{_e(label)}</label><input name='{_e(name)}' type='{_e(kind)}' "
            f"autocomplete='off' required>" for name, label, kind in connector.fields)
        inner += (f"<form method='post' action='/connect/{_e(source)}'>{_csrf_field(me.csrf)}"
                  f"{fields}<button>Connect</button></form>")
        cards.append(f"<div class='card'><h2>{_e(connector.label)}</h2>{inner}</div>")
    body = (f"<p>Signed in as {_e(me.user.display_name)}.</p>"
            f"{f'<div class=card>{message}</div>' if message else ''}"
            f"{''.join(cards)}"
            f"<div class='card'><a href='/connector'>Get your Claude connector link</a></div>"
            f"<form method='post' action='/logout'>{_csrf_field(me.csrf)}"
            f"<button>Sign out</button></form>")
    return _page("Connect your apps", body, status)


async def connect_home(request: Request) -> Response:
    me = _signed_in(request)
    return _connect_page(me) if me else _to_signin()


async def connect_source(request: Request) -> Response:
    me = _signed_in(request)
    if me is None:
        return _to_signin()
    source = request.path_params["source"]
    if source not in sources.CONNECTORS:
        return _page("Not found", "", 404)
    form = await _form(request, me.csrf)
    if form is None:
        return _connect_page(me, "<p class='err'>Form expired. Please try again.</p>", 400)
    connector = sources.CONNECTORS[source]()
    fields = {name: form.get(name, "") for name, _, _ in connector.fields}
    with db.open_session() as s:
        if _attempts_last_hour(s, me.user.id, source) >= SOURCE_ATTEMPTS_PER_HOUR:
            return _connect_page(me, "<p class='err'>Too many attempts. Try again in an "
                                 "hour.</p>", 429)
        # Count the attempt before contacting the source, so a crash still
        # counts. ok=True here means "attempt made", not "login worked".
        db.record_audit(s, me.user.id, f"connect:{source}", True)
        try:
            result = connector.start(me.user.id, fields)
        except sources.SourceBusy:
            return _connect_page(me, f"<p class='err'>{_e(connector.label)} is busy or "
                                 "blocked the login. Try again in an hour.</p>", 429)
        except sources.LoginFailed as e:
            return _connect_page(me, f"<p class='err'>{_e(e)}</p>", 400)
        if isinstance(result, sources.NeedsCode):
            pending_id = new_session_id()
            s.add(db.PendingLogin(
                id=pending_id, user_id=me.user.id, session_id=me.session_hash,
                source=source, state=seal({"paused": result.paused, "prompt": result.prompt}),
                expires_at=db.utcnow() + PENDING_LIFETIME, tries_left=PENDING_TRIES))
            s.commit()
            return RedirectResponse(f"/connect/code/{pending_id}", status_code=303)
        db.save_credential(s, me.user.id, source, result.state)
    return _connect_page(me, f"<p class='ok'>{_e(connector.label)} connected.</p>")


def _own_pending(s, me: _SignedIn, pending_id: str) -> db.PendingLogin | None:
    """Only the same user, in the same browser session, before it expires."""
    pending = s.get(db.PendingLogin, pending_id)
    if (pending is None or pending.user_id != me.user.id
            or not hmac.compare_digest(pending.session_id, me.session_hash)):
        return None
    if db.as_utc(pending.expires_at) < db.utcnow() or pending.tries_left <= 0:
        s.delete(pending)
        s.commit()
        return None
    return pending


_GONE = ("<p class='err'>That login has expired or was already finished. "
         "Start again from the Connect page.</p>")


async def connect_code(request: Request) -> Response:
    me = _signed_in(request)
    if me is None:
        return _to_signin()
    pending_id = request.path_params["pending_id"]
    error = ""
    with db.open_session() as s:
        pending = _own_pending(s, me, pending_id)
        if pending is None:
            return _connect_page(me, _GONE, 404)
        source = pending.source
        saved = unseal(pending.state)
        connector = sources.CONNECTORS[source]()
        if request.method == "POST":
            form = await _form(request, me.csrf)
            if form is None:
                error = "Form expired. Please try again."
            else:
                pending.tries_left -= 1
                s.commit()
                try:
                    result = connector.finish(me.user.id, saved["paused"],
                                              form.get("code", "").strip())
                except sources.WrongCode:
                    if pending.tries_left <= 0:
                        connector.forget(me.user.id, saved["paused"])
                        s.delete(pending)
                        s.commit()
                        return _connect_page(me, "<p class='err'>Too many wrong codes. "
                                             "Start again.</p>", 400)
                    error = f"That code didn't work. {pending.tries_left} tries left."
                except sources.SourceBusy:
                    s.delete(pending)
                    s.commit()
                    return _connect_page(me, f"<p class='err'>{_e(connector.label)} is busy "
                                         "or blocked the login. Try again in an hour.</p>", 429)
                except sources.LoginFailed as e:
                    s.delete(pending)
                    s.commit()
                    return _connect_page(me, f"<p class='err'>{_e(e)}</p>", 400)
                else:
                    s.delete(pending)
                    s.commit()
                    db.save_credential(s, me.user.id, source, result.state)
                    return _connect_page(me, f"<p class='ok'>{_e(connector.label)} "
                                         "connected.</p>")
    body = (f"<div class='card'><p>{_e(saved['prompt'])}</p>"
            f"{f'<p class=err>{_e(error)}</p>' if error else ''}"
            f"<form method='post'>{_csrf_field(me.csrf)}"
            f"<label>Code</label><input name='code' inputmode='numeric' "
            f"autocomplete='one-time-code' required><button>Finish connecting</button>"
            f"</form></div>")
    return _page(f"Enter your {connector.label} code", body)


async def connect_test(request: Request) -> Response:
    me = _signed_in(request)
    if me is None:
        return _to_signin()
    source = request.path_params["source"]
    if source not in sources.CONNECTORS or await _form(request, me.csrf) is None:
        return _connect_page(me, "<p class='err'>Form expired. Please try again.</p>", 400)
    connector = sources.CONNECTORS[source]()
    with db.open_session() as s:
        state = db.get_credential(s, me.user.id, source)
        if state is None:
            return _connect_page(me, "<p class='err'>Not connected yet.</p>", 400)
        try:
            summary, new_state = connector.test(state)
        except sources.NeedsReconnect:
            return _connect_page(me, f"<p class='err'>{_e(connector.label)} needs "
                                 "reconnecting. Enter your login again below.</p>", 400)
        except Exception:
            return _connect_page(me, f"<p class='err'>{_e(connector.label)} test "
                                 "failed. Try again later.</p>", 502)
        if new_state != state:
            db.save_credential(s, me.user.id, source, new_state)
        db.record_audit(s, me.user.id, f"test:{source}", True)
    return _connect_page(me, f"<p class='ok'>{_e(connector.label)} works. {_e(summary)}</p>")


# --- Claude connector link -------------------------------------------------------


async def connector_link(request: Request) -> Response:
    me = _signed_in(request)
    if me is None:
        return _to_signin()
    shown = ""
    if request.method == "POST":
        if await _form(request, me.csrf) is None:
            return _page("Error", "<div class='card err'>Form expired. Try again.</div>", 400)
        key = new_user_key()
        with db.open_session() as s:
            user = s.get(db.User, me.user.id)
            user.key_hash = hash_secret(key)
            s.commit()
            db.record_audit(s, me.user.id, "web:new-connector-link", True)
        link = f"{_public_url()}/mcp?key={key}"
        shown = (f"<div class='card'><h2>Your private link</h2>"
                 f"<p class='err'>Treat this like a password. It is shown only once.</p>"
                 f"<code>{_e(link)}</code><ol>"
                 f"<li>In Claude, open Settings, then Connectors.</li>"
                 f"<li>Choose <b>Add custom connector</b>.</li>"
                 f"<li>Paste the link above as the URL and save.</li></ol></div>")
    with db.open_session() as s:
        has_key = s.get(db.User, me.user.id).key_hash is not None
    warning = ("<p class='muted'>Making a new link switches off your old one.</p>"
               if has_key else "")
    body = (f"{shown}<div class='card'>{warning}<form method='post'>"
            f"{_csrf_field(me.csrf)}<button>Make a new link</button></form>"
            f"<p><a href='/connect'>Back to Connect</a></p></div>")
    return _page("Your Claude connector", body)


async def health(request: Request) -> Response:
    """For Railway: the app is up and can reach its database."""
    with db.open_session() as s:
        s.scalar(select(func.count()).select_from(db.User))
    return Response("ok", media_type="text/plain")


def install_routes(mcp) -> None:
    mcp.custom_route("/health", methods=["GET"])(health)
    mcp.custom_route("/invite/{code}", methods=["GET", "POST"])(invite)
    mcp.custom_route("/login", methods=["GET", "POST"])(login)
    mcp.custom_route("/logout", methods=["POST"])(logout)
    mcp.custom_route("/connect", methods=["GET"])(connect_home)
    mcp.custom_route("/connect/code/{pending_id}", methods=["GET", "POST"])(connect_code)
    mcp.custom_route("/connect/{source}", methods=["POST"])(connect_source)
    mcp.custom_route("/connect/{source}/test", methods=["POST"])(connect_test)
    mcp.custom_route("/connector", methods=["GET", "POST"])(connector_link)
