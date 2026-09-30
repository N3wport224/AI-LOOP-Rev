"""Mounts the API on the public aiohttp app and enforces auth, throttling and metering.

Every ``/v1`` request goes through ``ApiService.guarded``:

1. client blocked for failed authentications?   → 429 ``too_many_auth_failures``
2. key present / known / live?                  → 401 ``missing_api_key`` / ``invalid_api_key`` / ``key_revoked``
3. permission for this endpoint?                → 403 ``insufficient_permission``
4. token bucket (burst)                         → 429 ``rate_limited`` + ``Retry-After``
5. daily quota (persisted, atomic)              → 429 ``quota_exceeded`` + ``Retry-After`` (to 00:00 UTC)
6. handler (in the executor; SQLite and file reads never block the event loop)

Responses are deterministic JSON (sorted keys) with ``X-RateLimit-*``, ``X-Request-Id`` and
``Cache-Control: no-store`` headers, success or not.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web

from api.auth import ApiKeys
from api.data import QueryError, SignalIndex
from api.docs import CONSOLE_JS, CSP, render_docs
from api.openapi import ERROR_CODES, spec
from api.ratelimit import FailureLimiter, RateLimiter

API_PATHS = {"/v1/signals": {"GET"}, "/v1/me": {"GET"}, "/v1/auth/rotate": {"POST"}}
API_PATH_RE = re.compile(r"^/v1/companies/[^/]+$")


def dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def seconds_to_utc_midnight(now: datetime) -> int:
    utc = now.astimezone(timezone.utc)
    tomorrow = (utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((tomorrow - utc).total_seconds()))


class ApiService:
    def __init__(self, tools: Any, executor: Any = None, held: Callable[..., Any] | None = None,
                 client_ip: Callable[[web.Request], str] | None = None, limiter: RateLimiter | None = None,
                 failures: FailureLimiter | None = None):
        cfg = tools.config
        self.tools = tools
        self.keys = ApiKeys(tools.state, cfg)
        self.index = SignalIndex(tools.files, tools.state)
        self.limiter = limiter or RateLimiter(cfg.api_burst, cfg.api_rate_per_second)
        self.failures = failures or FailureLimiter(cfg.api_auth_failures_per_ip_hour)
        self.executor = executor
        self.held = held or (lambda fn, *a: fn(*a))
        self.client_ip = client_ip or (lambda r: r.remote or "")
        self.base_url = cfg.lead_capture_base

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self.executor, self.held, fn, *args)

    # -- responses ---------------------------------------------------------------------
    def error(self, code: str, status: int, message: str | None = None, param: str | None = None,
              headers: dict[str, str] | None = None, request_id: str = "") -> web.Response:
        body = {"error": {"code": code, "message": message or ERROR_CODES.get(code, code), "status": status,
                          "param": param, "doc_url": f"{self.base_url}/docs/api#errors", "request_id": request_id}}
        return self.respond(body, status, headers, request_id)

    @staticmethod
    def respond(body: Any, status: int = 200, headers: dict[str, str] | None = None, request_id: str = "") -> web.Response:
        resp = web.Response(text=dumps(body), status=status, content_type="application/json")
        resp.headers.update(headers or {})
        resp.headers["X-Request-Id"] = request_id
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @staticmethod
    def presented_key(request: web.Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        return request.headers.get("X-API-Key", "").strip() or None

    # -- the gate --------------------------------------------------------------------------
    def guarded(self, endpoint: str, permission: str | None,
                handler: Callable[[web.Request, dict[str, Any]], Awaitable[web.Response]]):
        async def wrapped(request: web.Request) -> web.Response:
            rid = "req_" + secrets.token_hex(8)
            ip = self.client_ip(request)
            wait = self.failures.blocked(ip)
            if wait:
                return self.error("too_many_auth_failures", 429, headers={"Retry-After": str(math.ceil(wait))}, request_id=rid)
            key = self.presented_key(request)
            if not key:
                self.failures.record(ip)
                return self.error("missing_api_key", 401, headers={"WWW-Authenticate": 'Bearer realm="api"'}, request_id=rid)
            row = await self.run(self.keys.lookup, key)
            if row is None:
                self.failures.record(ip)
                return self.error("invalid_api_key", 401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                                  request_id=rid)
            if row["status"] not in ("active", "degraded"):
                return self.error("key_revoked", 401, "This key was " + ("rotated" if row["status"] == "rotated" else "revoked")
                                  + (": the subscription ended." if row["status"] == "revoked" else "."), request_id=rid)
            if permission and permission not in row["permissions"]:
                return self.error("insufficient_permission", 403, request_id=rid)
            ok, retry = self.limiter.allow(row["id"])
            now = self.tools.state.clock()
            reset = int((now.astimezone(timezone.utc) + timedelta(seconds=seconds_to_utc_midnight(now))).timestamp())
            limits = {"X-RateLimit-Limit": str(row["daily_quota"]), "X-RateLimit-Reset": str(reset)}
            if row["status"] == "degraded":
                limits["X-API-Key-Status"] = "past_due"
            if not ok:
                used = await self.run(self.keys.used_today, row["id"], now)
                return self.error("rate_limited", 429, headers={
                    **limits, "X-RateLimit-Remaining": str(max(0, row["daily_quota"] - used)),
                    "Retry-After": str(max(1, math.ceil(retry)))}, request_id=rid)
            allowed, used = await self.run(self.keys.consume, row, endpoint, now)
            limits["X-RateLimit-Remaining"] = str(max(0, row["daily_quota"] - used))
            if not allowed:
                return self.error("quota_exceeded", 429, headers={**limits, "Retry-After": str(seconds_to_utc_midnight(now))},
                                  request_id=rid)
            try:
                resp = await handler(request, row)
            except QueryError as exc:
                status = 404 if exc.code == "not_found" else 400
                return self.error(exc.code, status, exc.message, exc.param, limits, rid)
            except Exception as exc:  # noqa: BLE001 - never leak a traceback to a client
                self.tools.state.log_error("api", f"{endpoint} {rid} failed: {exc!r}")
                return self.error("internal_error", 500, headers=limits, request_id=rid)
            resp.headers.update(limits)
            resp.headers["X-Request-Id"] = rid
            return resp
        return wrapped

    # -- handlers ---------------------------------------------------------------------------
    async def signals(self, request: web.Request, row: dict[str, Any]) -> web.Response:
        params = {k: v for k, v in request.query.items()}
        result = await self.run(self.index.query, params, self.tools.config.api_max_page_size)
        return self.respond(result)

    async def company(self, request: web.Request, row: dict[str, Any]) -> web.Response:
        ident = request.match_info["domain"]
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", ident):
            raise QueryError("invalid_parameter", "domain must be a hostname like acme.com or a company_id", "domain")
        result = await self.run(self.index.company, ident, self.tools.state.clock())
        if result is None:
            raise QueryError("not_found", f"no company {ident!r} in the dataset", "domain")
        return self.respond(result)

    async def me(self, request: web.Request, row: dict[str, Any]) -> web.Response:
        used = await self.run(self.keys.used_today, row["id"], self.tools.state.clock())
        return self.respond({"object": "api_key", "prefix": row["prefix"], "status": row["status"], "plan": row["plan"],
                             "daily_quota": row["daily_quota"], "used_today": used, "permissions": row["permissions"]})

    async def rotate(self, request: web.Request, row: dict[str, Any]) -> web.Response:
        key, new = await self.run(self.keys.rotate, row)
        self.limiter.forget(row["id"])
        self.tools.state.log_action(int(self.tools.state.get("iteration", 0)), None, "api:rotate", "ok",
                                    f"{row['prefix']}… → {new['prefix']}…")
        return self.respond({"object": "api_key", "key": key, "prefix": new["prefix"], "status": new["status"],
                             "previous_prefix": row["prefix"]}, 201)

    # -- public, unauthenticated ------------------------------------------------------------------
    async def openapi(self, request: web.Request) -> web.Response:
        resp = self.respond(spec(self.base_url))
        resp.headers["Cache-Control"] = "public, max-age=300"
        resp.headers["Access-Control-Allow-Origin"] = "*"  # the document holds nothing secret
        return resp

    async def docs(self, request: web.Request) -> web.Response:
        resp = web.Response(text=render_docs(spec(self.base_url), self.base_url), content_type="text/html")
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    async def console_js(self, request: web.Request) -> web.Response:
        return web.Response(text=CONSOLE_JS, content_type="application/javascript",
                            headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})

    async def fallback(self, request: web.Request) -> web.Response:
        rid = "req_" + secrets.token_hex(8)
        path = request.path
        known = API_PATHS.get(path) or ({"GET"} if API_PATH_RE.match(path) else None)
        if known:
            return self.error("method_not_allowed", 405, f"use {', '.join(sorted(known))}",
                              headers={"Allow": ", ".join(sorted(known))}, request_id=rid)
        return self.error("not_found", 404, f"no route {path}", request_id=rid)


def mount(app: web.Application, tools: Any, executor: Any = None, held: Callable[..., Any] | None = None,
          client_ip: Callable[[web.Request], str] | None = None) -> ApiService:
    svc = ApiService(tools, executor, held, client_ip)
    app.router.add_get("/v1/signals", svc.guarded("signals", "signals:read", svc.signals))
    app.router.add_get("/v1/companies/{domain}", svc.guarded("companies", "companies:read", svc.company))
    app.router.add_get("/v1/me", svc.guarded("me", None, svc.me))
    app.router.add_post("/v1/auth/rotate", svc.guarded("rotate", None, svc.rotate))
    app.router.add_get("/openapi.json", svc.openapi)
    app.router.add_get("/docs/api", svc.docs)
    app.router.add_get("/docs/api/console.js", svc.console_js)
    app.router.add_route("*", "/v1/{tail:.*}", svc.fallback)
    return svc
