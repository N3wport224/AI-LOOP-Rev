"""Credentials & secret manager: ``GET/POST /api/settings``, ``POST /api/preflight``.

* Secrets are **write-only** from the browser's side: the page learns whether one is set and its
  last 4 characters, never the value. Leaving a secret field empty keeps the stored value;
  clearing one is an explicit action.
* Every value is validated before anything is written: formats (``sk_live_``/``sk_test_`` must
  match the Live/Test toggle, ``whsec_``, ``SG.``, emails, ports, hostnames, URLs) and, for every
  field, no line breaks or control characters, so a pasted value can't inject extra lines into
  ``.env``.
* ``.env`` is rewritten atomically in place: comments, ordering and unrelated keys are kept, and
  the file stays mode 600.
* After saving, the same checks as ``automonetize setup-autonomous`` run (format validation plus
  live preflight: Stripe API, mailer login, database, disk), and a warning names any field that
  ``automonetize.toml`` or an ``AUTOMONETIZE_*`` variable overrides.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from aiohttp import web

from agent.setup_autonomous import stripe_mode
from agent.tunnel import TunnelConfigError, public_webhook_url, read_env_file, update_env_file, validate_hostname
from gui.context import ctx

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24}$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MAX_LEN = 500


def _v_stripe_key(v: str) -> str | None:
    return None if stripe_mode(v) else "must be a Stripe secret or restricted key (sk_live_, sk_test_, rk_live_, rk_test_)"


def _v_prefix(prefix: str, what: str) -> Callable[[str], str | None]:
    return lambda v: None if v.startswith(prefix) and len(v) > len(prefix) + 8 else f"{what} starts with {prefix}"


def _v_email(v: str) -> str | None:
    return None if EMAIL_RE.match(v) else "not a valid email address"


def _v_port(v: str) -> str | None:
    return None if v.isdigit() and 0 < int(v) < 65536 else "port must be 1-65535"


def _v_host(v: str) -> str | None:
    return None if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", v) and "." in v else "not a hostname"


def _v_tunnel(v: str) -> str | None:
    try:
        validate_hostname(v)
        return None
    except TunnelConfigError as exc:
        return str(exc)


def _v_https(v: str) -> str | None:
    p = urlsplit(v)
    return None if p.scheme == "https" and p.netloc and not p.query else "must be an https:// URL"


def _v_repo(v: str) -> str | None:
    return None if re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}", v) else "use owner/repo"


def _v_github(v: str) -> str | None:
    return None if re.fullmatch(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{40,})", v) else \
        "doesn't look like a GitHub token (ghp_..., github_pat_...)"


def _v_postal(v: str) -> str | None:
    return None if len(v) >= 10 and re.search(r"\d", v) else "enter a full postal address (street, city, postcode, country)"


def _v_choice(*choices: str) -> Callable[[str], str | None]:
    return lambda v: None if v in choices else f"choose one of: {', '.join(choices)}"


@dataclass
class Field:
    key: str                         # the .env variable written
    label: str
    group: str
    attr: str | None = None          # Config attribute it feeds (to show the effective value and detect overrides)
    secret: bool = False
    kind: str = "text"               # text | password | select | bool | email | number
    options: list[str] = field(default_factory=list)
    validate: Callable[[str], str | None] | None = None
    help: str = ""
    placeholder: str = ""

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("validate")
        return d


GROUPS = [
    ("stripe", "Stripe", "Payments and the webhook that fulfils orders instantly."),
    ("mailer", "Delivery mailer", "Sends purchased datasets, subscription updates and the free sample."),
    ("syndication", "Syndication & publishing", "Dev.to, Hashnode and GitHub (showcase, Pages site)."),
    ("tunnel", "Cloudflare Tunnel", "The permanent public hostname Stripe and the lead form reach."),
    ("compliance", "Compliance (CAN-SPAM)", "Required in every marketing email."),
    ("monitoring", "Monitoring", "Where reports go, and an outside check that emails you if the agent stops."),
    ("evolution", "Autonomous code evolution", "Lets the agent patch its own heuristics (parsers, tech vocabulary, copy) after "
                  "the full test suite and simulator pass in a sandbox. Every change is a local git commit you can revert."),
]

FIELDS: list[Field] = [
    Field("STRIPE_MODE", "Mode", "stripe", kind="select", options=["test", "live"],
          help="Live charges real cards. The key below must match."),
    Field("STRIPE_SECRET_KEY", "Secret key (STRIPE_API_KEY)", "stripe", "stripe_secret_key", True, "password",
          validate=_v_stripe_key, placeholder="sk_live_... or rk_live_..."),
    Field("STRIPE_WEBHOOK_SECRET", "Webhook signing secret", "stripe", "stripe_webhook_secret", True, "password",
          validate=_v_prefix("whsec_", "a webhook signing secret"), help="setup-autonomous creates the endpoint and fills this in."),
    Field("AUTOMONETIZE_EMAIL_BACKEND", "Mailer", "mailer", "email_backend", kind="select", options=["smtp", "sendgrid", "postmark"]),
    Field("SMTP_HOST", "SMTP host", "mailer", "smtp_host", validate=_v_host, placeholder="smtp.fastmail.com"),
    Field("SMTP_PORT", "SMTP port", "mailer", "smtp_port", kind="number", validate=_v_port, placeholder="587"),
    Field("SMTP_USER", "SMTP user", "mailer", "smtp_username"),
    Field("SMTP_PASSWORD", "SMTP password", "mailer", "smtp_password", True, "password"),
    Field("SENDGRID_API_KEY", "SendGrid API key", "mailer", "sendgrid_api_key", True, "password",
          validate=_v_prefix("SG.", "a SendGrid key")),
    Field("POSTMARK_SERVER_TOKEN", "Postmark server token", "mailer", "postmark_server_token", True, "password"),
    Field("AUTOMONETIZE_SENDER_NAME", "Sender name", "mailer", "sender_name"),
    Field("AUTOMONETIZE_SENDER_EMAIL", "Sender email", "mailer", "sender_email", kind="email", validate=_v_email),
    Field("DRY_RUN", "Dry run (log emails instead of sending)", "mailer", "dry_run", kind="bool", options=["true", "false"],
          validate=_v_choice("true", "false"), help="Turn off only after the preflight passes."),
    Field("DEVTO_API_KEY", "Dev.to API key", "syndication", "devto_api_key", True, "password"),
    Field("HASHNODE_TOKEN", "Hashnode token", "syndication", "hashnode_token", True, "password"),
    Field("AUTOMONETIZE_HASHNODE_PUBLICATION_ID", "Hashnode publication ID", "syndication", "hashnode_publication_id"),
    Field("GITHUB_TOKEN", "GitHub token", "syndication", "github_token", True, "password", validate=_v_github),
    Field("AUTOMONETIZE_GITHUB_PAGES_REPO", "GitHub Pages repo", "syndication", "github_pages_repo", validate=_v_repo,
          placeholder="you/datasets"),
    Field("AUTOMONETIZE_PAGES_BASE_URL", "Site URL", "syndication", "pages_base_url", validate=_v_https,
          placeholder="https://you.github.io/datasets"),
    Field("TUNNEL_HOSTNAME", "Tunnel hostname", "tunnel", None, validate=_v_tunnel, placeholder="hooks.yourdomain.com",
          help="Also sets PUBLIC_WEBHOOK_URL. Create the tunnel once with deploy/tunnel/setup_tunnel.sh."),
    Field("CAN_SPAM_POSTAL_ADDRESS", "Postal address", "compliance", "sender_postal_address", validate=_v_postal,
          placeholder="123 Main St, Springfield, IL 62701, USA"),
    Field("CAN_SPAM_UNSUBSCRIBE_EMAIL", "Unsubscribe mailbox", "compliance", "unsubscribe_email", kind="email",
          validate=_v_email),
    Field("OWNER_EMAIL", "Your email (reports and alerts)", "monitoring", "owner_email", kind="email", validate=_v_email,
          help="Empty = the sender email."),
    Field("HEALTHCHECK_URL", "Heartbeat URL", "monitoring", "heartbeat_url", validate=_v_https,
          placeholder="https://hc-ping.com/your-check-id",
          help="Free at healthchecks.io: it emails you if the agent stops (Mac off, asleep or offline)."),
    Field("ENABLE_AUTONOMOUS_CODE_EVOLUTION", "Enable autonomous code evolution", "evolution", "enable_autonomous_code_evolution",
          kind="bool", options=["false", "true"], validate=_v_choice("true", "false"),
          help="Off by default. Needs a clean git checkout; changes are committed locally, never pushed. "
               "Inspect with `automonetize evolution log`."),
]
BY_KEY = {f.key: f for f in FIELDS}


def hint(value: str) -> str:
    return f"•••• {value[-4:]}" if len(value) >= 12 else "••••"


def current_values(gctx) -> list[dict[str, Any]]:
    env = read_env_file(gctx.env_file)
    cfg = gctx.config
    out = []
    for f in FIELDS:
        raw = env.get(f.key, "")
        effective = getattr(cfg, f.attr) if f.attr else raw
        if f.key == "TUNNEL_HOSTNAME" and not raw and cfg.public_webhook_url:
            effective = urlsplit(cfg.public_webhook_url).hostname or ""
        if f.key == "STRIPE_MODE":
            effective = raw or stripe_mode(cfg.stripe_secret_key) or "test"
        if isinstance(effective, bool):
            effective = "true" if effective else "false"
        effective = "" if effective is None else str(effective)
        item = f.public()
        if f.secret:
            item.update(set=bool(effective), hint=hint(effective) if effective else "", value="")
        else:
            item.update(set=bool(effective), value=effective)
        out.append(item)
    return out


def validate(values: dict[str, str], existing: dict[str, str], cfg) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (updates, errors). Secrets left empty are unchanged."""
    updates: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key, raw in values.items():
        f = BY_KEY.get(key)
        if f is None:
            errors[key] = "unknown setting"
            continue
        if not isinstance(raw, str):
            errors[key] = "must be text"
            continue
        v = raw.strip()
        if CONTROL_CHARS.search(v):
            errors[key] = "line breaks and control characters aren't allowed"
            continue
        if len(v) > MAX_LEN:
            errors[key] = f"longer than {MAX_LEN} characters"
            continue
        if f.secret and not v:
            continue  # empty secret field = keep what's stored
        if v and f.validate:
            problem = f.validate(v)
            if problem:
                errors[key] = problem
                continue
        updates[key] = v
    mode = updates.get("STRIPE_MODE") or existing.get("STRIPE_MODE") or stripe_mode(cfg.stripe_secret_key) or "test"
    key = updates.get("STRIPE_SECRET_KEY") or (existing.get("STRIPE_SECRET_KEY") if "STRIPE_MODE" in updates else None)
    if key and "STRIPE_SECRET_KEY" not in errors and stripe_mode(key) and stripe_mode(key) != mode:
        errors["STRIPE_SECRET_KEY"] = f"this is a {stripe_mode(key)}-mode key but the mode is set to {mode}"
    if updates.get("TUNNEL_HOSTNAME"):
        updates["PUBLIC_WEBHOOK_URL"] = public_webhook_url(updates["TUNNEL_HOSTNAME"], cfg.webhook_path)
    return updates, errors


def override_warnings(saved: dict[str, str], cfg) -> list[str]:
    warnings = []
    for key, value in saved.items():
        f = BY_KEY.get(key)
        if not f or not f.attr or f.secret or not value:
            continue
        effective = getattr(cfg, f.attr)
        if isinstance(effective, bool):
            effective = "true" if effective else "false"
        if str(effective) != value:
            warnings.append(f"{key} is saved, but the agent uses {f.attr} = {effective!r} from automonetize.toml or an "
                            f"AUTOMONETIZE_{f.attr.upper()} variable. Remove that to use this value.")
    return warnings


async def run_checks(gctx) -> list[dict[str, Any]]:
    if gctx.preflight is None:
        return []
    checks = await asyncio.get_running_loop().run_in_executor(None, gctx.preflight, gctx.config)
    return [asdict(c) if hasattr(c, "__dataclass_fields__") else dict(c) for c in checks]


async def get_settings(request: web.Request) -> web.Response:
    gctx = ctx(request)
    gctx.reload_config()  # reflect edits made to .env outside the panel
    return web.json_response({"groups": [{"id": g, "title": t, "help": h} for g, t, h in GROUPS],
                              "fields": current_values(gctx), "env_file": str(gctx.env_file)})


async def post_settings(request: web.Request) -> web.Response:
    gctx = ctx(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    values = body.get("values") or {}
    clear = [k for k in body.get("clear") or [] if isinstance(k, str)]
    if not isinstance(values, dict):
        return web.json_response({"error": "values must be an object"}, status=400)
    existing = read_env_file(gctx.env_file)
    updates, errors = validate(values, existing, gctx.config)
    for key in clear:
        if key not in BY_KEY:
            errors[key] = "unknown setting"
        else:
            updates[key] = ""
            if key == "TUNNEL_HOSTNAME":
                updates["PUBLIC_WEBHOOK_URL"] = ""
    if errors:
        return web.json_response({"ok": False, "errors": errors}, status=400)
    changed = {k: v for k, v in updates.items() if existing.get(k, "") != v}
    if changed:
        await asyncio.get_running_loop().run_in_executor(None, update_env_file, gctx.env_file, changed)
    try:
        cfg = gctx.reload_config()
    except (ValueError, TypeError) as exc:  # e.g. a non-numeric value in a numeric field elsewhere
        return web.json_response({"ok": False, "errors": {"_": f"saved, but the config no longer loads: {exc}"}}, status=400)
    checks = await run_checks(gctx) if body.get("preflight", True) else []
    running = gctx.controller.supervisor_pid() is not None
    return web.json_response({
        "ok": True, "saved": sorted(k for k in changed if k in BY_KEY or k == "PUBLIC_WEBHOOK_URL"),
        "warnings": override_warnings({k: v for k, v in updates.items() if k in BY_KEY}, cfg),
        "checks": checks, "restart_required": bool(changed) and running,
    })


async def post_preflight(request: web.Request) -> web.Response:
    gctx = ctx(request)
    gctx.reload_config()
    checks = await run_checks(gctx)
    return web.json_response({"ok": not any(c.get("status") == "fail" for c in checks), "checks": checks})


def make_preflight(env_file) -> Callable[[Any], list[Any]]:
    """validate_env + live preflight: the same checks ``setup-autonomous`` starts with."""
    def run(config) -> list[Any]:
        from agent.connectivity import is_online
        from agent.setup_autonomous import preflight, validate_env
        from agent.state import StateStore
        from tools import build_toolkit
        from tools.circuit_breaker import CircuitBreaker

        state = StateStore(config.db_path)
        try:
            breaker = CircuitBreaker(max_actions_per_cycle=1000, max_api_calls_per_cycle=1000, max_consecutive_errors=1000)
            tools = build_toolkit(config, state, breaker)
            return validate_env(config, env_file) + preflight(config, state, tools.http,
                                                              lambda: is_online(config.network_check_hosts))
        finally:
            state.close()

    return run


def routes() -> list[web.RouteDef]:
    return [web.get("/api/settings", get_settings), web.post("/api/settings", post_settings),
            web.post("/api/preflight", post_preflight)]
