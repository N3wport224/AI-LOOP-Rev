"""Rate-limited HTTP client with robots.txt support, rotating user agents and self-correcting retries."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any, Callable

from tools.circuit_breaker import CircuitBreaker, RateLimiter
from tools.errors import NON_RETRYABLE, HttpError, RobotsDisallowed
from tools.recovery import retry_with_adjustment

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 Edg/128.0",
]

# Status codes worth retrying; anything else non-2xx fails fast after logging.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}

_SECRET_PARAMS = re.compile(r"(access_token|api_key|token|key|secret)=([^&]+)", re.I)


def redact(url: str) -> str:
    return _SECRET_PARAMS.sub(lambda m: f"{m.group(1)}=***", url)


@dataclass
class Response:
    status: int
    url: str
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body or b"null")


Transport = Callable[[str, str, dict[str, str], bytes | None, float], Response]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> Response:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked by caller
            return Response(resp.status, url, resp.read(), {k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as exc:
        return Response(exc.code, url, exc.read() or b"", {k.lower(): v for k, v in (exc.headers or {}).items()})


class HttpClient:
    def __init__(
        self,
        rate_limiter: RateLimiter,
        breaker: CircuitBreaker | None = None,
        transport: Transport | None = None,
        error_sink: Callable[[str, str, str], None] | None = None,
        user_agents: list[str] | None = None,
        timeout: float = 15.0,
        max_attempts: int = 3,
        respect_robots: bool = True,
        backoff_seconds: float = 1.0,
        max_timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate_limiter = rate_limiter
        self.breaker = breaker
        self.transport = transport or urllib_transport
        self.error_sink = error_sink
        self.user_agents = list(user_agents or USER_AGENTS)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.respect_robots = respect_robots
        self.backoff_seconds = backoff_seconds
        self.max_timeout = max_timeout
        self._sleep = sleep
        self._ua_index = 0
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # -- user agents -------------------------------------------------------
    @property
    def current_user_agent(self) -> str:
        return self.user_agents[self._ua_index % len(self.user_agents)]

    def rotate_user_agent(self) -> str:
        self._ua_index = (self._ua_index + 1) % len(self.user_agents)
        return self.current_user_agent

    # -- robots.txt ---------------------------------------------------------
    def allowed_by_robots(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                resp = self._send("GET", origin + "/robots.txt", {"User-Agent": self.current_user_agent}, None, self.timeout)
                if resp.ok:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(resp.text.splitlines())
                elif resp.status in (401, 403):
                    # RFC 9309: an explicitly forbidden robots.txt means "disallow all".
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(["User-agent: *", "Disallow: /"])
            except Exception:  # noqa: BLE001 - unreachable robots.txt means no restrictions
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(self.current_user_agent, url)

    # -- requests ----------------------------------------------------------
    def _send(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> Response:
        host = urllib.parse.urlsplit(url).netloc
        if self.breaker is not None:
            self.breaker.check_api_call()
        self.rate_limiter.acquire(host)
        return self.transport(method, url, headers, body, timeout)

    def _adjust(self, attempt: int, exc: BaseException, params: dict[str, Any]) -> dict[str, Any]:
        """Change what we send after a failure instead of blindly repeating it."""
        headers = dict(params["headers"])
        headers["User-Agent"] = self.rotate_user_agent()
        # Fall back to the most permissive content negotiation.
        headers["Accept"] = "*/*"
        headers.pop("Accept-Encoding", None)
        params["headers"] = headers
        params["timeout"] = min(self.max_timeout, params["timeout"] * 1.5)
        if isinstance(exc, HttpError) and exc.status == 429:
            retry_after = _seconds(params.get("_retry_after"))
            if retry_after:
                self._sleep(min(retry_after, 60.0))
        return params

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        check_robots: bool | None = None,
        attempts: int | None = None,
        raw_body: bytes | None = None,
    ) -> Response:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {url!r}")
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = f"{url}{'&' if parts.query else '?'}{query}"
        if (self.respect_robots if check_robots is None else check_robots) and not self.allowed_by_robots(url):
            raise RobotsDisallowed(f"robots.txt disallows {redact(url)}")

        max_attempts = attempts or self.max_attempts
        body: bytes | None = None
        base_headers = {"User-Agent": self.current_user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
        if json_body is not None:
            body = json.dumps(json_body).encode()
            base_headers["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            base_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif raw_body is not None:
            body = raw_body  # sent byte-for-byte (e.g. a signed payload)
        base_headers.update(headers or {})

        def attempt(p: dict[str, Any]) -> Response:
            resp = self._send(method, url, p["headers"], body, p["timeout"])
            if resp.ok:
                return resp
            p["_retry_after"] = resp.headers.get("retry-after")
            raise HttpError(resp.status, redact(url), resp.text[:500], retry_after=_seconds(p["_retry_after"]))

        def on_error(n: int, exc: BaseException, tb: str) -> None:
            if self.error_sink is not None:
                self.error_sink("http_client", f"attempt {n}/{max_attempts} {method} {redact(url)}: {exc}", tb)

        def attempt_or_fail_fast(p: dict[str, Any]) -> Response:
            try:
                return attempt(p)
            except HttpError as exc:
                if exc.status not in RETRYABLE_STATUS:
                    # Treat as final: permanent 4xx won't be fixed by retrying.
                    on_error(0, exc, "")
                    raise _Permanent(exc) from exc
                raise

        try:
            return retry_with_adjustment(
                attempt_or_fail_fast,
                {"headers": base_headers, "timeout": self.timeout},
                attempts=max_attempts,
                adjust=self._adjust,
                on_error=on_error,
                no_retry=(_Permanent,) + NON_RETRYABLE,
                backoff_seconds=self.backoff_seconds,
                sleep=self._sleep,
                label=f"{method} {redact(url)}",
            )
        except _Permanent as exc:
            raise exc.inner from None

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()


def _seconds(value: str | None) -> float | None:
    """Retry-After as seconds (the delta-seconds form; an HTTP date is ignored)."""
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


class _Permanent(Exception):
    def __init__(self, inner: HttpError):
        super().__init__(str(inner))
        self.inner = inner

