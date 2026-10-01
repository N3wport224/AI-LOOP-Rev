"""Connections check (Phase 71): is every outside service the agent uses actually reachable?

``check_all`` tries each configured integration with a harmless, read-only call and reports
``ok`` / ``fail`` / ``skip`` (not set up) with what to do:

* **Stripe**: read the balance (proves the key works; says whether it's live or test).
* **Sending email**: log in to the SMTP server (nothing is sent).
* **Reading email** (support desk, bounces, commands): log in over IMAP, read-only.
* **Public URL** (webhook, free-sample form, download links): fetch ``/healthz`` through the tunnel.
* **Website**: fetch the site's home page.
* **GitHub** and **Dev.to**: read the account the token belongs to.
* **Heartbeat**: ping it (that's what it's for).

Used by ``automonetize connections``, the setup wizard and the "agent started" notice. No
network calls outside these, and nothing is created, sent or changed.
"""

from __future__ import annotations

import imaplib
from dataclasses import asdict, dataclass
from typing import Any, Callable


@dataclass
class Conn:
    name: str
    status: str  # ok | fail | skip
    detail: str
    fix: str = ""


def _try(name: str, fn: Callable[[], str], fix: str) -> Conn:
    try:
        return Conn(name, "ok", fn() or "works")
    except Exception as exc:  # noqa: BLE001 - every failure is reported, none raised
        status = getattr(exc, "status", None)
        why = f"HTTP {status}" if status else (str(exc) or type(exc).__name__)
        return Conn(name, "fail", why[:200], fix)


def check_all(config: Any, http: Any, smtp_login: Callable[..., str | None] | None = None,
              imap_factory: Callable[[str], Any] = imaplib.IMAP4_SSL) -> list[Conn]:
    from tools.inbox import imap_settings

    out: list[Conn] = []
    key = str(config.stripe_secret_key or "")
    if key:
        def stripe() -> str:
            bal = http.get_json("https://api.stripe.com/v1/balance", headers={"Authorization": f"Bearer {key}"}, check_robots=False,
                                attempts=1)
            return "live account" if bal.get("livemode") else "TEST mode (nothing real can be sold)"
        out.append(_try("Stripe", stripe, "automonetize go-live (paste a fresh key)"))
    else:
        out.append(Conn("Stripe", "skip", "no key yet", "automonetize go-live"))

    if config.smtp_host and config.smtp_username:
        def smtp() -> str:
            if smtp_login is None:
                from cli.go_live import smtp_login as real_login
                login = real_login
            else:
                login = smtp_login
            problem = login(config.smtp_host, int(config.smtp_port), config.smtp_username, config.smtp_password)
            if problem:
                raise RuntimeError(problem)
            return f"{config.smtp_username} can send"
        out.append(_try("Sending email", smtp, "control panel → Settings → Delivery mailer (Gmail: use an App password)"))
    else:
        out.append(Conn("Sending email", "skip", "no mail server set", "automonetize go-live"))

    host, user, password = imap_settings(config)
    if host and user and password:
        def imap() -> str:
            conn = imap_factory(host)
            try:
                conn.login(user, password)
                conn.select("INBOX", readonly=True)
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001
                    pass
            return f"{user} inbox readable"
        out.append(_try("Reading email", imap, "Gmail: Settings → Forwarding and POP/IMAP → enable IMAP"))
    else:
        out.append(Conn("Reading email", "skip", "IMAP not available for this mail server",
                        "set imap_host in automonetize.toml for support replies and bounces"))

    base = config.lead_capture_base
    if base:
        out.append(_try("Public URL", lambda: (http.get(f"{base}/healthz", check_robots=False, attempts=1), "tunnel and listener up")[1],
                        "is the tunnel running? automonetize doctor --fix"))
    else:
        out.append(Conn("Public URL", "skip", "no tunnel: instant delivery, the free sample and download links need it",
                        "deploy/tunnel/setup_tunnel.sh"))

    if config.pages_base_url:
        out.append(_try("Website", lambda: (http.get(config.pages_base_url, check_robots=False, attempts=1), config.pages_base_url)[1],
                        "automonetize connect-marketing"))
    else:
        out.append(Conn("Website", "skip", "no public site yet", "automonetize connect-marketing"))

    if config.github_token:
        def github() -> str:
            me = http.get_json("https://api.github.com/user", headers={"Authorization": f"Bearer {config.github_token}"},
                               check_robots=False, attempts=1)
            return f"signed in as {me.get('login', '?')}"
        out.append(_try("GitHub", github, "make a new token: automonetize connect-marketing"))
    else:
        out.append(Conn("GitHub", "skip", "no token", "automonetize connect-marketing"))

    if config.devto_api_key:
        def devto() -> str:
            me = http.get_json("https://dev.to/api/users/me", headers={"api-key": config.devto_api_key}, check_robots=False, attempts=1)
            return f"signed in as {me.get('username', '?')}"
        out.append(_try("Dev.to", devto, "make a new key at dev.to/settings/extensions"))
    else:
        out.append(Conn("Dev.to", "skip", "no key", "automonetize connect-marketing"))

    if config.heartbeat_url:
        out.append(_try("Heartbeat", lambda: (http.get(config.heartbeat_url, check_robots=False, attempts=1), "pinged")[1],
                        "automonetize heartbeat <url>"))
    else:
        out.append(Conn("Heartbeat", "skip", "not set up", "automonetize heartbeat"))
    return out


def as_dicts(conns: list[Conn]) -> list[dict[str, str]]:
    return [asdict(c) for c in conns]


def summary(conns: list[Conn]) -> str:
    bad = [c for c in conns if c.status == "fail"]
    ok = sum(1 for c in conns if c.status == "ok")
    return f"{ok} working, {len(bad)} failing" + (": " + ", ".join(c.name for c in bad) if bad else "")
