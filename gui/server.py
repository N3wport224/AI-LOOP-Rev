"""The GUI's aiohttp app and ``run_gui`` (``automonetize gui``)."""

from __future__ import annotations

import asyncio
import html
import logging
import webbrowser
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from agent.config import Config
from agent.state import StateStore
from gui.auth import COOKIE, LOOPBACK_HOSTS, SESSION_TTL, Auth, allowed_hosts, allowed_origins, load_or_create_token, token_path
from gui.context import CTX, SESSION_KEY, GuiContext, ctx
from gui.routes import control as control_routes
from gui.routes import outreach as outreach_routes
from gui.routes import settings as settings_routes

log = logging.getLogger("automonetize.gui")
STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"app.js": "application/javascript", "style.css": "text/css", "index.html": "text/html"}
PUBLIC = {("GET", "/login"), ("POST", "/login"), ("GET", "/auth/launch"), ("GET", "/static/style.css"),
          ("GET", "/favicon.ico")}
SAFE = {"GET", "HEAD", "OPTIONS"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")


def _login_page(message: str = "", status: int = 200) -> web.Response:
    note = f'<p class="error">{html.escape(message)}</p>' if message else ""
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoMonetize: sign in</title><link rel="stylesheet" href="/static/style.css"></head>
<body class="login"><main class="card narrow"><h1>AutoMonetize</h1>
<p>Paste the access token from <code>data/.gui_token</code>, or start the panel with <code>automonetize gui</code>, which signs you in automatically.</p>
{note}<form method="post" action="/login"><label for="token">Access token</label>
<input id="token" name="token" type="password" autocomplete="current-password" required autofocus>
<button type="submit" class="primary">Sign in</button></form></main></body></html>"""
    return web.Response(text=body, content_type="text/html", status=status)


def _set_session(resp: web.StreamResponse, sid: str) -> None:
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="Strict", path="/", max_age=SESSION_TTL)


@web.middleware
async def guard(request: web.Request, handler: Callable) -> web.StreamResponse:
    gctx = ctx(request)
    # DNS rebinding: a page on evil.example re-pointed at 127.0.0.1 still sends Host: evil.example.
    if request.host not in allowed_hosts(gctx.port):
        return web.Response(status=421, text="misdirected request: use http://127.0.0.1:%d" % gctx.port)
    origin = request.headers.get("Origin")
    if request.method not in SAFE and origin and origin not in allowed_origins(gctx.port):
        return web.json_response({"error": "cross-origin request refused"}, status=403)
    if (request.method, request.path) not in PUBLIC:
        session = gctx.auth.session(request.cookies.get(COOKIE))
        if session is None:
            if request.path.startswith("/api/"):
                return web.json_response({"error": "not signed in"}, status=401)
            raise web.HTTPFound("/login")
        if request.method not in SAFE and not gctx.auth.csrf_ok(session, request.headers.get("X-CSRF-Token")):
            return web.json_response({"error": "missing or invalid CSRF token"}, status=403)
        request[SESSION_KEY] = session
    resp = await handler(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    # same-origin, not no-referrer: with no-referrer browsers send "Origin: null" on form posts,
    # which the origin check (rightly) refuses. Nothing leaks to other sites either way.
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.path.startswith("/api/") or request.path in ("/", "/login"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


async def login_get(request: web.Request) -> web.Response:
    return _login_page()


async def login_post(request: web.Request) -> web.StreamResponse:
    gctx = ctx(request)
    if not gctx.auth.login_allowed():
        return _login_page("Too many attempts. Wait a few minutes.", 429)
    form = await request.post()
    if not gctx.auth.check_token(str(form.get("token", ""))):
        gctx.auth.record_failure()
        return _login_page("That token isn't right.", 401)
    sid, _ = gctx.auth.create_session()
    resp = web.HTTPFound("/")
    _set_session(resp, sid)
    raise resp


async def launch(request: web.Request) -> web.StreamResponse:
    gctx = ctx(request)
    if not gctx.auth.redeem_launch_code(request.query.get("code", "")):
        return _login_page("That sign-in link has expired. Paste the token instead.", 401)
    sid, _ = gctx.auth.create_session()
    resp = web.HTTPFound("/")
    _set_session(resp, sid)
    raise resp


async def logout(request: web.Request) -> web.Response:
    ctx(request).auth.end_session(request.cookies.get(COOKIE))
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def session_info(request: web.Request) -> web.Response:
    gctx = ctx(request)
    return web.json_response({"csrf": request[SESSION_KEY].csrf, "port": gctx.port, "env_file": str(gctx.env_file),
                              "token_file": str(token_path(gctx.config.data_dir))})


async def static(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if name not in STATIC_FILES:
        raise web.HTTPNotFound()
    return web.Response(body=(STATIC / name).read_bytes(), content_type=STATIC_FILES[name], charset="utf-8")


async def index(request: web.Request) -> web.Response:
    return web.Response(body=(STATIC / "index.html").read_bytes(), content_type="text/html", charset="utf-8")


async def favicon(request: web.Request) -> web.Response:
    return web.Response(status=204)


def build_app(gctx: GuiContext) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app[CTX] = gctx
    app.add_routes([
        web.get("/", index), web.get("/login", login_get), web.post("/login", login_post), web.get("/auth/launch", launch),
        web.post("/logout", logout), web.get("/api/session", session_info), web.get("/static/{name}", static),
        web.get("/favicon.ico", favicon),
        *settings_routes.routes(), *control_routes.routes(), *outreach_routes.routes(),
    ])
    return app


def make_context(config_path: str | None, env_file: Path, workdir: Path, port: int | None = None,
                 **overrides: Any) -> GuiContext:
    from agent.setup_autonomous import load_env_into
    from gui.control import ServiceController

    config = Config.load(config_path, env=load_env_into(env_file))
    config.ensure_dirs()
    state = StateStore(config.db_path)
    controller = overrides.pop("controller", None) or ServiceController(config, state, workdir, env_file)
    return GuiContext(
        config=config, config_path=config_path, env_file=env_file, workdir=workdir, state=state, controller=controller,
        auth=overrides.pop("auth", None) or Auth(load_or_create_token(config.data_dir)), port=port or config.gui_port,
        preflight=overrides.pop("preflight", None) or settings_routes.make_preflight(env_file), **overrides,
    )


def check_loopback(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing to serve the control panel on {host!r}: it holds your secrets. Use 127.0.0.1.")


async def serve(gctx: GuiContext, host: str, open_browser: bool = True, stop: asyncio.Event | None = None,
                opener: Callable[[str], Any] = webbrowser.open) -> None:
    check_loopback(host)
    runner = web.AppRunner(build_app(gctx), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, gctx.port)
    await site.start()
    base = f"http://127.0.0.1:{gctx.port}"
    log.info("control panel on %s", base)
    print(f"AutoMonetize control panel: {base}  (token: {token_path(gctx.config.data_dir)})", flush=True)
    if open_browser:
        url = f"{base}/auth/launch?code={gctx.auth.issue_launch_code()}"
        await asyncio.get_running_loop().run_in_executor(None, opener, url)
    try:
        await (stop or asyncio.Event()).wait()
    finally:
        await runner.cleanup()
        gctx.state.close()


def run_gui(config_path: str | None, env_file: Path, workdir: Path, host: str = "127.0.0.1", port: int | None = None,
            open_browser: bool = True) -> None:
    check_loopback(host)
    gctx = make_context(config_path, env_file, workdir, port)
    try:
        asyncio.run(serve(gctx, host, open_browser))
    except KeyboardInterrupt:
        pass
