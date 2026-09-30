"""Local-only access control for the GUI.

Threats this handles, since "it's only on localhost" isn't a security boundary on its own:

* **Other local users / processes**: every request needs a session, obtained with the token in
  ``data/.gui_token`` (mode 600, readable only by you) or a one-time launch code that
  ``automonetize gui`` opens in your browser (valid 2 minutes, usable once).
* **Malicious web pages you visit** (CSRF): the session cookie is ``SameSite=Strict`` and
  ``HttpOnly``, and every state-changing request must carry the session's CSRF token in an
  ``X-CSRF-Token`` header (a cross-site page can't set custom headers without a CORS preflight,
  which this server never grants). A present ``Origin`` header must be this server's.
* **DNS rebinding** (a public hostname re-pointed at 127.0.0.1): requests whose ``Host`` isn't
  ``127.0.0.1:<port>`` / ``localhost:<port>`` are refused.
* **Token guessing**: 256-bit token, constant-time comparison, login attempts rate-limited.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TOKEN_FILE = ".gui_token"
COOKIE = "am_gui"
SESSION_TTL = 12 * 3600
LAUNCH_TTL = 120
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def token_path(data_dir: Path) -> Path:
    return Path(data_dir) / TOKEN_FILE


def load_or_create_token(data_dir: Path) -> str:
    path = token_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text().strip()
        if len(token) >= 32:
            os.chmod(path, 0o600)
            return token
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    os.chmod(path, 0o600)
    return token


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def allowed_origins(port: int) -> set[str]:
    return {f"http://{h}" for h in allowed_hosts(port)}


@dataclass
class Session:
    csrf: str
    expires: float


class Auth:
    def __init__(self, token: str, clock: Callable[[], float] = time.monotonic):
        self.token = token
        self.clock = clock
        self._sessions: dict[str, Session] = {}
        self._launch: dict[str, float] = {}
        self._failures: list[float] = []
        self._lock = threading.Lock()

    def check_token(self, candidate: str) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.strip().encode(), self.token.encode())

    def login_allowed(self) -> bool:
        now = self.clock()
        with self._lock:
            self._failures = [t for t in self._failures if now - t < 300]
            return len(self._failures) < 10

    def record_failure(self) -> None:
        with self._lock:
            self._failures.append(self.clock())

    def create_session(self) -> tuple[str, str]:
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            self._sessions = {k: v for k, v in self._sessions.items() if v.expires > now}
            self._sessions[sid] = Session(csrf, now + SESSION_TTL)
        return sid, csrf

    def session(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s is None or s.expires <= self.clock():
                self._sessions.pop(sid, None)
                return None
            return s

    def end_session(self, sid: str | None) -> None:
        with self._lock:
            self._sessions.pop(sid or "", None)

    def issue_launch_code(self) -> str:
        code = secrets.token_urlsafe(24)
        with self._lock:
            self._launch[code] = self.clock() + LAUNCH_TTL
        return code

    def redeem_launch_code(self, code: str) -> bool:
        with self._lock:
            expires = self._launch.pop(code or "", None)
        return expires is not None and expires > self.clock()

    @staticmethod
    def csrf_ok(session: Session, header: str | None) -> bool:
        return bool(header) and hmac.compare_digest(header.encode(), session.csrf.encode())
