"""Download links: deliver files too big to attach.

Email providers cap attachments (about 25 MB, and the agent stays under 8 MB to be safe). A
dataset that outgrows that used to end up "needs manual delivery". Now the delivery email carries
a private link instead, served by the agent's own public server (the tunnel) at ``/d/<token>``:

* the token is random (192 bits) and only its SHA-256 is stored;
* it expires after ``download_link_days`` (7) and works ``download_link_uses`` (5) times;
* it serves exactly one file inside the data folder, as an attachment download;
* order recovery issues fresh links, so a buyer can always get their file again.

Table ``download_tokens``.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SCHEMA = """CREATE TABLE IF NOT EXISTS download_tokens (
    token_hash TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    email TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    downloads INTEGER NOT NULL DEFAULT 0,
    max_downloads INTEGER NOT NULL
)"""


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ensure(state: Any) -> None:
    state._exec(_SCHEMA)


def available(config: Any) -> bool:
    return bool(config.lead_capture_base)


def issue(state: Any, config: Any, rel_path: str, email: str = "") -> str:
    """A fresh link for ``rel_path`` (relative to the data folder)."""
    ensure(state)
    token = secrets.token_urlsafe(24)
    now = state.clock()
    state._exec("INSERT INTO download_tokens (token_hash, path, email, created_at, expires_at, max_downloads) VALUES (?,?,?,?,?,?)",
                (_h(token), rel_path, email.lower(), now.isoformat(timespec="seconds"),
                 (now + timedelta(days=int(config.download_link_days))).isoformat(timespec="seconds"),
                 int(config.download_link_uses)))
    return f"{config.lead_capture_base}/d/{token}"


def redeem(state: Any, files: Any, token: str) -> tuple[Path | None, str]:
    """(file to serve, "") or (None, why not). Counts the download."""
    ensure(state)
    if not token or len(token) > 100:
        return None, "invalid link"
    row = state._one("SELECT * FROM download_tokens WHERE token_hash = ?", (_h(token),))
    if not row:
        return None, "invalid link"
    if datetime.fromisoformat(row["expires_at"]) <= state.clock():
        return None, "this link has expired: reply to your order email, or use the order-recovery form, for a new one"
    if int(row["downloads"]) >= int(row["max_downloads"]):
        return None, "this link has been used up: reply to your order email for a new one"
    try:
        path = files.resolve(row["path"])
    except Exception:  # noqa: BLE001 - a path outside the data folder is never served
        return None, "file unavailable"
    if not path.is_file():
        return None, "file unavailable"
    state._exec("UPDATE download_tokens SET downloads = downloads + 1 WHERE token_hash = ?", (_h(token),))
    return path, ""


def prune(state: Any, older_than_days: int = 30) -> int:
    ensure(state)
    cutoff = (state.clock() - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    return state._exec("DELETE FROM download_tokens WHERE expires_at < ?", (cutoff,)).rowcount
