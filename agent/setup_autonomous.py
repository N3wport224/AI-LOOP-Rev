"""``automonetize setup-autonomous``: one command from "configured" to "running unattended".

Steps (each reports ok / warn / fail / skip; a failure stops the steps that depend on it):

1. **Validate .env**: Stripe key shape and mode (live vs test), webhook secret, public URL from
   the tunnel, the mailer the config selects, the dry-run switch, and .env file permissions.
2. **Preflight**: network, Stripe API (``GET /v1/balance``), the mailer (SMTP login, SendGrid
   scopes or Postmark server), database integrity, free disk.
3. **Register the Stripe webhook endpoint** for the public URL (idempotent: an existing
   AutoMonetize endpoint for the same URL is reused and its events are brought up to date). Stripe
   returns the signing secret only when an endpoint is *created*, so a new secret goes straight
   into .env (mode 600); it's never printed.
4. **Load launchd**: the agent and tunnel jobs (``deploy/install_launchd.sh``). On other systems
   the plists are rendered to ``data/launchd/`` and the step reports what it would do.
5. **Handshake**: a signed ``automonetize.handshake`` event is POSTed to the *public* URL, so it
   goes through Cloudflare, the tunnel and the local listener, and must be verified and recorded
   under its nonce. If the listener isn't running yet (no launchd, e.g. on Linux), a temporary one
   is started for the test.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import smtplib
import stat
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from agent.recovery import self_diagnostic
from agent.tunnel import TunnelConfigError, read_env_file, update_env_file, validate_hostname
from tools.errors import HttpError, OperationalFailure, ToolError

STRIPE_API = "https://api.stripe.com/v1"
KEY_PREFIXES = {"sk_live_": "live", "rk_live_": "live", "sk_test_": "test", "rk_test_": "test"}
ENDPOINT_METADATA = "automonetize"
HANDSHAKE_WAIT_SECONDS = 20.0


@dataclass
class Check:
    step: str
    name: str
    status: str   # ok | warn | fail | skip
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def stripe_mode(key: str) -> str | None:
    for prefix, mode in KEY_PREFIXES.items():
        if key.startswith(prefix) and len(key) > len(prefix) + 8:
            return mode
    return None


def _bad_perms(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    except OSError:
        return False


# ----------------------------------------------------------------------------- 1. validate
def validate_env(config: Any, env_file: Path, require_live: bool = False) -> list[Check]:
    step = "validate"
    out: list[Check] = []
    if not env_file.exists():
        out.append(Check(step, ".env", "fail", f"{env_file} not found (copy .env.example)"))
    elif _bad_perms(env_file):
        out.append(Check(step, ".env permissions", "warn", f"{env_file} is readable by other users: chmod 600 {env_file}"))
    else:
        out.append(Check(step, ".env", "ok", str(env_file)))

    key = config.stripe_secret_key
    mode = stripe_mode(key)
    if not key:
        out.append(Check(step, "STRIPE_SECRET_KEY", "fail", "missing"))
    elif mode is None:
        out.append(Check(step, "STRIPE_SECRET_KEY", "fail", "not a Stripe secret or restricted key (sk_live_/sk_test_/rk_...)"))
    elif mode == "test":
        out.append(Check(step, "STRIPE_SECRET_KEY", "fail" if require_live else "warn",
                         "TEST mode key: nothing will be charged. Switch to sk_live_ for real revenue."))
    else:
        out.append(Check(step, "STRIPE_SECRET_KEY", "ok", "live mode"))

    whsec = config.stripe_webhook_secret
    if not whsec:
        out.append(Check(step, "STRIPE_WEBHOOK_SECRET", "warn", "not set yet: step 3 creates the endpoint and saves it"))
    elif not whsec.startswith("whsec_"):
        out.append(Check(step, "STRIPE_WEBHOOK_SECRET", "fail", "should start with whsec_"))
    else:
        out.append(Check(step, "STRIPE_WEBHOOK_SECRET", "ok", "set"))

    url = config.public_webhook_url
    try:
        parts = urlsplit(url)
        if not url:
            raise TunnelConfigError("missing: run deploy/tunnel/setup_tunnel.sh <hostname>")
        if parts.scheme != "https":
            raise TunnelConfigError("must be https:// (Stripe requires TLS)")
        validate_hostname(parts.hostname or "")
        if (parts.path or "/") != config.webhook_path:
            raise TunnelConfigError(f"path {parts.path!r} doesn't match webhook_path {config.webhook_path!r}")
        out.append(Check(step, "PUBLIC_WEBHOOK_URL", "ok", url))
    except TunnelConfigError as exc:
        out.append(Check(step, "PUBLIC_WEBHOOK_URL", "fail", str(exc)))

    out += validate_mailer(config)
    if config.dry_run:
        out.append(Check(step, "DRY_RUN", "warn", "dry run: order delivery and outreach emails are logged, not sent"))
    else:
        out.append(Check(step, "DRY_RUN", "ok", "live email"))
    return out


def mailer_name(config: Any) -> str:
    return config.email_backend or config.outreach_email_backend or ""


def validate_mailer(config: Any) -> list[Check]:
    step = "validate"
    name = mailer_name(config)
    problems: list[str] = []
    if name == "smtp":
        if not config.smtp_host:
            return [Check(step, "mailer", "warn" if config.allow_manual_fulfillment else "fail",
                          "no email backend: set smtp_host (or email_backend = sendgrid|postmark) so paid orders are delivered automatically")]
        if config.smtp_username and not config.smtp_password:
            problems.append("SMTP_PASSWORD missing")
        if not (0 < int(config.smtp_port) < 65536):
            problems.append(f"bad smtp_port {config.smtp_port}")
    elif name == "sendgrid":
        if not config.sendgrid_api_key.startswith("SG."):
            problems.append("SENDGRID_API_KEY missing or not an SG. key")
    elif name == "postmark":
        if len(config.postmark_server_token) < 20:
            problems.append("POSTMARK_SERVER_TOKEN missing")
    else:
        problems.append(f"unknown email backend {name!r}")
    if not config.sender_email or "@" not in config.sender_email:
        problems.append("sender_email missing")
    if problems:
        return [Check(step, f"mailer ({name})", "fail", "; ".join(problems))]
    return [Check(step, f"mailer ({name})", "ok", "credentials present")]


# ----------------------------------------------------------------------------- 2. preflight
def _stripe_headers(config: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.stripe_secret_key}"}


def _err(exc: BaseException) -> str:
    inner = exc.last_exception if isinstance(exc, OperationalFailure) and exc.last_exception else exc
    if isinstance(inner, HttpError):
        return f"HTTP {inner.status}"
    return str(inner)[:200]


def check_mailer_connectivity(config: Any, http: Any, smtp_factory: Callable[..., Any] = smtplib.SMTP) -> Check:
    step = "preflight"
    name = mailer_name(config)
    try:
        if name == "smtp":
            if not config.smtp_host:
                return Check(step, "mailer", "skip", "no SMTP host")
            factory = smtplib.SMTP_SSL if config.smtp_port == 465 and smtp_factory is smtplib.SMTP else smtp_factory
            with factory(config.smtp_host, config.smtp_port, timeout=20) as smtp:
                smtp.ehlo()
                if config.smtp_port != 465:
                    smtp.starttls()
                    smtp.ehlo()
                if config.smtp_username:
                    smtp.login(config.smtp_username, config.smtp_password)
            return Check(step, "mailer (smtp)", "ok", f"logged in to {config.smtp_host}:{config.smtp_port}")
        if name == "sendgrid":
            http.get("https://api.sendgrid.com/v3/scopes", headers={"Authorization": f"Bearer {config.sendgrid_api_key}"},
                     check_robots=False, attempts=1)
            return Check(step, "mailer (sendgrid)", "ok", "API key accepted")
        if name == "postmark":
            http.get("https://api.postmarkapp.com/server", headers={"X-Postmark-Server-Token": config.postmark_server_token,
                                                                    "Accept": "application/json"}, check_robots=False, attempts=1)
            return Check(step, "mailer (postmark)", "ok", "server token accepted")
    except (smtplib.SMTPException, OSError, ToolError) as exc:
        return Check(step, f"mailer ({name})", "fail", _err(exc))
    return Check(step, "mailer", "skip", f"unknown backend {name!r}")


def preflight(config: Any, state: Any, http: Any, online_check: Callable[[], bool],
              smtp_factory: Callable[..., Any] = smtplib.SMTP) -> list[Check]:
    step = "preflight"
    diag = self_diagnostic(state, config, online_check)
    out = [
        Check(step, "network", "ok" if diag["network"] else "fail", "reachable" if diag["network"] else "no route to the API hosts"),
        Check(step, "database", "ok" if diag["db_integrity"] is True and diag["db_write"] is True else "fail",
              f"integrity={diag['db_integrity']} write={diag['db_write']}"),
        Check(step, "disk", "ok" if diag.get("disk_ok") is True else "fail", f"{diag.get('disk_free_mb', '?')} MB free"),
    ]
    if not diag["network"]:
        out.append(Check(step, "stripe api", "skip", "offline"))
        return out
    try:
        bal = http.get_json(f"{STRIPE_API}/balance", headers=_stripe_headers(config), check_robots=False, attempts=2)
        out.append(Check(step, "stripe api", "ok", f"authenticated ({'live' if bal.get('livemode') else 'test'} mode)"))
    except ToolError as exc:
        out.append(Check(step, "stripe api", "fail", _err(exc)))
    out.append(check_mailer_connectivity(config, http, smtp_factory))
    return out


# ----------------------------------------------------------------------------- 3. stripe endpoint
def webhook_events() -> list[str]:
    from tools.storefront.webhook_listener import HANDLED, HANDSHAKE_TYPE

    return sorted(e for e in HANDLED if e != HANDSHAKE_TYPE)


def register_webhook_endpoint(config: Any, http: Any, env_file: Path) -> Check:
    """Find or create the Stripe endpoint for ``config.public_webhook_url``; save a new secret to .env."""
    from tools.storefront.stripe_pages_publisher import flatten_form

    step = "stripe webhook"
    url = config.public_webhook_url
    events = webhook_events()
    headers = _stripe_headers(config)
    try:
        listing = http.get_json(f"{STRIPE_API}/webhook_endpoints", params={"limit": 100}, headers=headers,
                                check_robots=False)
        existing = [e for e in listing.get("data", []) if e.get("url") == url]
        if existing:
            ep = existing[0]
            enabled = set(ep.get("enabled_events") or [])
            if "*" not in enabled and not set(events) <= enabled:
                http.post(f"{STRIPE_API}/webhook_endpoints/{ep['id']}", headers=headers, check_robots=False,
                          data=flatten_form({"enabled_events": sorted(enabled | set(events))}))
            if ep.get("status") == "disabled":
                http.post(f"{STRIPE_API}/webhook_endpoints/{ep['id']}", headers=headers, check_robots=False,
                          data={"disabled": "false"})
            if config.stripe_webhook_secret:
                return Check(step, "endpoint", "ok", f"{ep['id']} already registered for {url}")
            # Stripe never returns an existing endpoint's secret. Only replace endpoints we created.
            if (ep.get("metadata") or {}).get(ENDPOINT_METADATA) != "1":
                return Check(step, "endpoint", "fail",
                             f"{ep['id']} exists for {url} but its signing secret isn't in .env; copy it from the "
                             "Stripe dashboard into STRIPE_WEBHOOK_SECRET")
            http.request("DELETE", f"{STRIPE_API}/webhook_endpoints/{ep['id']}", headers=headers, check_robots=False)
        created = http.post(
            f"{STRIPE_API}/webhook_endpoints", headers=headers, check_robots=False,
            data=flatten_form({"url": url, "enabled_events": events, "description": "AutoMonetize (setup-autonomous)",
                               "metadata": {ENDPOINT_METADATA: "1"}}),
        ).json()
    except ToolError as exc:
        return Check(step, "endpoint", "fail", _err(exc))
    secret = created.get("secret", "")
    if not secret.startswith("whsec_"):
        return Check(step, "endpoint", "fail", "Stripe didn't return a signing secret")
    update_env_file(env_file, {"STRIPE_WEBHOOK_SECRET": secret})
    config.stripe_webhook_secret = secret
    return Check(step, "endpoint", "ok", f"created {created.get('id')} for {url}; signing secret saved to {env_file}")


# ----------------------------------------------------------------------------- 4. launchd
def install_services(workdir: Path, data_dir: Path, system: str | None = None, as_daemon: bool = False,
                     runner: Callable[..., Any] = subprocess.run) -> Check:
    step = "launchd"
    script = workdir / "deploy" / "install_launchd.sh"
    if (system or platform.system()) != "Darwin":
        out_dir = data_dir / "launchd"
        proc = runner(["/bin/bash", str(script), "--render-only", str(out_dir)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return Check(step, "services", "fail", (proc.stderr or proc.stdout).strip()[:300])
        return Check(step, "services", "skip", f"not macOS: plists rendered to {out_dir}; "
                     "on the Mac, this step runs deploy/install_launchd.sh")
    cmd = ["/bin/bash", str(script)] + (["--system"] if as_daemon else [])
    if as_daemon and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    proc = runner(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return Check(step, "services", "fail", (proc.stderr or proc.stdout).strip()[:300])
    return Check(step, "services", "ok", " / ".join(line for line in proc.stdout.splitlines() if line.startswith("installed")))


# ----------------------------------------------------------------------------- 5. handshake
def handshake_event(nonce: str, now: float) -> bytes:
    from tools.storefront.webhook_listener import HANDSHAKE_TYPE

    return json.dumps({
        "id": f"evt_handshake_{nonce}", "object": "event", "type": HANDSHAKE_TYPE, "created": int(now),
        "livemode": False, "data": {"object": {"nonce": nonce, "via": "public_url"}},
    }, separators=(",", ":")).encode()


def local_listener_up(config: Any, http: Any) -> bool:
    try:
        http.get(f"http://{config.webhook_host}:{config.webhook_port}/healthz", check_robots=False, attempts=1)
        return True
    except (ToolError, OSError):
        return False


def handshake(config: Any, state: Any, http: Any, now: Callable[[], float] = time.time,
              sleep: Callable[[float], None] = time.sleep, wait_seconds: float = HANDSHAKE_WAIT_SECONDS,
              nonce: str | None = None) -> Check:
    from tools.storefront.webhook_listener import sign_payload

    step = "handshake"
    if not config.stripe_webhook_secret or not config.public_webhook_url:
        return Check(step, "public url", "skip", "needs STRIPE_WEBHOOK_SECRET and PUBLIC_WEBHOOK_URL")
    nonce = nonce or secrets.token_hex(12)
    body = handshake_event(nonce, now())
    header = sign_payload(body, config.stripe_webhook_secret, int(now()))
    deadline = now() + wait_seconds
    last = ""
    # The tunnel or a freshly restarted agent may need a few seconds: retry until the deadline.
    while True:
        try:
            resp = http.request("POST", config.public_webhook_url, headers={"Stripe-Signature": header,
                                "Content-Type": "application/json"},
                                check_robots=False, attempts=1, raw_body=body)
            reply = resp.json() if resp.body else {}
            if reply.get("status") == "processed" and state.get(f"handshake:{nonce}"):
                return Check(step, "public url", "ok", f"{config.public_webhook_url} → tunnel → listener verified nonce {nonce[:6]}…")
            if reply.get("duplicate") and state.get(f"handshake:{nonce}"):
                return Check(step, "public url", "ok", "verified (duplicate delivery)")
            last = f"unexpected reply {reply}"
        except ToolError as exc:
            inner = exc.last_exception if isinstance(exc, OperationalFailure) and exc.last_exception else exc
            if isinstance(inner, HttpError) and inner.status == 400:
                return Check(step, "public url", "fail", "listener rejected the signature: the running agent has a "
                             "different STRIPE_WEBHOOK_SECRET (restart it: launchctl kickstart -k gui/$UID/com.automonetize.agent)")
            last = _err(exc)
        if now() >= deadline:
            hint = "is the tunnel running? launchctl print gui/$UID/com.automonetize.tunnel"
            if "HTTP 5" in last or "HTTP 4" in last:
                hint = "the tunnel answered but the listener didn't; check ~/Library/Logs/automonetize/agent.stderr.log"
            return Check(step, "public url", "fail", f"{last}; {hint}")
        sleep(1.0)


class TemporaryListener:
    """Runs the webhook listener in a thread for the handshake when no agent is serving it."""

    def __init__(self, tools: Any):
        from tools.storefront.webhook_listener import WebhookServer

        self.server = WebhookServer(tools)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.run, args=(self.stop,), name="setup-webhook", daemon=True)

    def __enter__(self) -> "TemporaryListener":
        self.thread.start()
        if not self.server.started.wait(10):
            raise RuntimeError("temporary webhook listener didn't start (port in use?)")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop.set()
        self.thread.join(15)


# ----------------------------------------------------------------------------- orchestration
@dataclass
class SetupOptions:
    env_file: Path
    workdir: Path
    require_live: bool = False
    skip_register: bool = False
    skip_launchd: bool = False
    skip_handshake: bool = False
    as_daemon: bool = False
    system: str | None = None


def sudoers_hint(user: str | None = None) -> str:
    user = user or os.environ.get("USER", "you")
    return f"{user} ALL=(root) NOPASSWD: /usr/bin/pmset schedule wake *"


def run_setup(config: Any, tools: Any, opts: SetupOptions, online_check: Callable[[], bool],
              runner: Callable[..., Any] = subprocess.run, smtp_factory: Callable[..., Any] = smtplib.SMTP,
              now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
              listener_factory: Callable[[Any], Any] = TemporaryListener) -> list[Check]:
    checks = validate_env(config, opts.env_file, opts.require_live)
    blocking = {"STRIPE_SECRET_KEY", "PUBLIC_WEBHOOK_URL", ".env"}
    if any(c.failed and c.name in blocking for c in checks):
        return checks + [Check(s, "-", "skip", "fix the validation failures first")
                         for s in ("preflight", "stripe webhook", "launchd", "handshake")]
    pre = preflight(config, tools.state, tools.http, online_check, smtp_factory)
    checks += pre
    stripe_ok = any(c.name == "stripe api" and c.status == "ok" for c in pre)

    if opts.skip_register:
        checks.append(Check("stripe webhook", "endpoint", "skip", "--skip-register"))
    elif not stripe_ok:
        checks.append(Check("stripe webhook", "endpoint", "skip", "Stripe API not reachable"))
    else:
        checks.append(register_webhook_endpoint(config, tools.http, opts.env_file))

    if opts.skip_launchd:
        checks.append(Check("launchd", "services", "skip", "--skip-launchd"))
    else:
        checks.append(install_services(opts.workdir, config.data_dir, opts.system, opts.as_daemon, runner))

    if opts.skip_handshake:
        checks.append(Check("handshake", "public url", "skip", "--skip-handshake"))
    elif any(c.failed for c in checks if c.step == "stripe webhook"):
        checks.append(Check("handshake", "public url", "skip", "no webhook secret"))
    else:
        # launchd needs a moment to start the agent; wait for its listener, else serve one ourselves.
        up = local_listener_up(config, tools.http)
        for _ in range(10):
            if up:
                break
            sleep(1.0)
            up = local_listener_up(config, tools.http)
        if up:
            checks.append(handshake(config, tools.state, tools.http, now, sleep))
        else:
            try:
                with listener_factory(tools):
                    checks.append(handshake(config, tools.state, tools.http, now, sleep))
            except (RuntimeError, OSError) as exc:
                checks.append(Check("handshake", "public url", "fail", f"no local listener: {exc}"))

    if config.schedule_wake and (opts.system or platform.system()) == "Darwin":
        ok = shutil.which("sudo") and runner(["sudo", "-n", "-l", "/usr/bin/pmset", "schedule", "wake", "01/01/30 00:00:00"],
                                             capture_output=True, text=True, check=False).returncode == 0
        checks.append(Check("power", "scheduled wake", "ok" if ok else "warn",
                            "pmset wake allowed" if ok else f"to wake the Mac for cycles, add via `sudo visudo -f /etc/sudoers.d/automonetize`: {sudoers_hint()}"))
    return checks


def summarize(checks: list[Check]) -> dict[str, Any]:
    return {"ok": not any(c.failed for c in checks), "checks": [asdict(c) for c in checks]}


def load_env_into(env_file: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` overlaid with ``.env`` values (the file wins, as it does for launchd)."""
    merged = dict(os.environ if base is None else base)
    merged.update(read_env_file(env_file))
    return merged
