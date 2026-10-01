"""Security self-audit: the agent checks its own setup daily and fixes what's safe to fix.

``audit_security`` (part of ``agent/``, not ``strategies/``, so self-evolution can't change it):

* **Fixed automatically** (and logged): ``.env`` readable by other users → mode 600; the data folder
  and backups → 700 / 600; ``.env`` missing from ``.gitignore`` → added.
* **Reported** (alert once, then on the doctor and the to-do list):
  * ``.env`` committed to git (keys in the history): remove it and roll the keys;
  * secrets written in ``automonetize.toml`` instead of ``.env``;
  * the control panel listening beyond this Mac (``gui_host`` not loopback);
  * live payments without a webhook signing secret;
  * a full Stripe secret key (``sk_live_``): a *restricted* key (``rk_live_``) with only the
    permissions below limits the damage if it ever leaks.

State: kv ``security_audit`` = {checked_at, findings}.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "security_audit"
RESTRICTED_KEY_PERMISSIONS = (
    "Write: Products, Prices, Payment Links, Coupons, Promotion Codes, Checkout Sessions, Customer portal, "
    "Webhook Endpoints. Read: Balance, Payouts, Charges & Refunds, Disputes, Subscriptions, Invoices, Customers."
)
SECRET_TOML = re.compile(r"^\s*(stripe_secret_key|smtp_password|imap_password|github_token|devto_api_key|sendgrid_api_key|"
                         r"postmark_server_token|gumroad_access_token|stripe_webhook_secret)\s*=\s*\"[^\"]+\"", re.M)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _loose(path: Path, allowed: int) -> bool:
    return path.exists() and bool(stat.S_IMODE(path.stat().st_mode) & ~allowed)


def audit(config: Any, root: Path, run: Callable[..., Any] = subprocess.run, fix: bool = True) -> list[dict[str, str]]:
    from agent.backup import backup_root

    out: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str, how: str = "") -> None:
        out.append({"name": name, "status": status, "detail": detail, "fix": how})

    env = root / ".env"
    if _loose(env, 0o600):
        if fix:
            os.chmod(env, 0o600)
            add(".env permissions", "fixed", "was readable by other users; now 600")
        else:
            add(".env permissions", "warn", "readable by other users", "chmod 600 .env")
    data = Path(config.data_dir)
    for p, mode in ((data, 0o700), *((f, 0o600) for f in data.glob("*.db"))):
        if _loose(p, mode):
            if fix:
                os.chmod(p, mode)
                add("data permissions", "fixed", f"{p.name} tightened to {oct(mode)[2:]}")
            else:
                add("data permissions", "warn", f"{p.name} is readable by other users", f"chmod {oct(mode)[2:]} {p}")
    backups = backup_root(config)
    if _loose(backups, 0o700) and fix:
        os.chmod(backups, 0o700)
        add("backup permissions", "fixed", "backup folder tightened to 700")
    if (root / ".git").exists():
        tracked = run(["git", "ls-files", "--error-unmatch", ".env"], cwd=str(root), capture_output=True, text=True, timeout=30)
        if tracked.returncode == 0:
            add(".env in git", "fail", "your keys are in the git history",
                "git rm --cached .env && git commit -m 'stop tracking .env', then roll every key in it")
        ignore = root / ".gitignore"
        lines = ignore.read_text().splitlines() if ignore.exists() else []
        if not any(ln.strip() in (".env", "/.env", ".env*") for ln in lines):
            if fix:
                ignore.write_text("\n".join([*lines, ".env"]) + "\n")
                add(".gitignore", "fixed", ".env added, so it can't be committed by accident")
            else:
                add(".gitignore", "warn", ".env isn't ignored", "echo .env >> .gitignore")
    toml = root / "automonetize.toml"
    if toml.exists() and SECRET_TOML.search(toml.read_text()):
        add("secrets in automonetize.toml", "warn", "a key or password is written in automonetize.toml",
            "move it to .env (control panel → Settings saves there) and delete the line")
    if str(config.gui_host) not in LOOPBACK:
        add("control panel exposure", "fail", f"listens on {config.gui_host}, reachable from other machines",
            "set gui_host = \"127.0.0.1\"")
    key = str(config.stripe_secret_key or "")
    if key.startswith(("sk_live_", "rk_live_")) and not config.stripe_webhook_secret:
        add("webhook secret", "warn", "live payments without a webhook signing secret", "automonetize setup-autonomous --live")
    if key.startswith("sk_live_"):
        add("Stripe key type", "warn", "a full secret key can do anything in your Stripe account if it leaks",
            "Stripe → Developers → API keys → Create restricted key with: " + RESTRICTED_KEY_PERMISSIONS
            + " Then `automonetize go-live` with the rk_live_ key and delete the old one")
    return out


class SecurityAudit(Strategy):
    name = "security_audit"
    tasks = ("audit_security",)

    def __init__(self, root: Path | None = None, run: Callable[..., Any] = subprocess.run):
        self.root, self._run = root, run

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from agent.evolution.hot_reload import repo_root

        cfg, state = ctx.tools.config, ctx.tools.state
        previous = state.get(KEY) or {}
        last = previous.get("checked_at")
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=23):
            return TaskResult(True, f"security checked {last[:16]}", {})
        try:
            from agent.self_update import NO_UPDATE_ENV

            # In a sandbox (tests, simulator, evolution checks) only report: never touch the real checkout.
            findings = audit(cfg, self.root or repo_root(cfg), run=self._run, fix=not os.environ.get(NO_UPDATE_ENV))
        except Exception as exc:  # noqa: BLE001 - an audit problem must not stop the agent
            state.log_error("security_audit", f"audit failed: {exc!r}")
            return TaskResult(True, f"audit failed: {exc!r}"[:200], {})
        from tools.key_age import update as update_key_ages

        update_key_ages(state, cfg)
        known = {f["name"] for f in previous.get("findings", []) if f["status"] == "fail"}
        for f in findings:
            if f["status"] == "fixed":
                state.log_action(int(state.get("iteration", 0)), None, "security", "ok", f"{f['name']}: {f['detail']}")
            elif f["status"] == "fail" and f["name"] not in known:
                state.log_error("security_audit", f"{f['name']}: {f['detail']}. Fix: {f['fix']}", kind="alert")
        state.set(KEY, {"checked_at": state.now(), "findings": [f for f in findings if f["status"] != "fixed"]})
        bad = [f for f in findings if f["status"] in ("warn", "fail")]
        return TaskResult(True, f"security: {len(bad)} open item(s), {len(findings) - len(bad)} fixed", {"open": len(bad)})
