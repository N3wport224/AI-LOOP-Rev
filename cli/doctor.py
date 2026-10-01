"""`automonetize doctor` (health check + fixes) and `automonetize autostart` (start at login).

`doctor` checks, in plain words, everything that keeps the agent earning:

* running? starts after a reboot? engine paused, stopped or offline? last cycle recent?
* live payments and real email? anything actually on sale (with links)?
* email settings complete? marketing connected (public site, articles)?
* will the Mac stay awake? is the code up to date?
* problems in the last 24 hours.

``--fix`` applies the safe fixes itself: starts the agent, installs autostart, pulls a code
update (fast-forward only, when nothing is modified locally) and restarts. A pause or kill switch
you set is never undone for you; the report tells you how.
"""

from __future__ import annotations

import os
import platform
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from cli.go_live import BOLD, GREEN, RED, ROOT, YELLOW, say

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class Finding:
    name: str
    status: str                 # ok | warn | fail
    detail: str
    fix: str = ""               # what to do (shown)
    auto: Callable[[], str] | None = None  # applied with --fix; returns what happened


def sh(run: Runner, *cmd: str, timeout: float = 60) -> tuple[int, str]:
    try:
        p = run(list(cmd), cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


class Doctor:
    def __init__(self, config: Any, state: Any, controller: Any, run: Runner = subprocess.run, system: str | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config, self.state, self.ctl, self.run = config, state, controller, run
        self.system = system or platform.system()
        self.sleep = sleep

    # -- fixes ---------------------------------------------------------------------------------
    def start_agent(self) -> str:
        return self.ctl.start().get("message", "")

    def enable_autostart(self) -> str:
        if self.system != "Darwin":
            return "autostart via launchd is macOS-only (Linux: deploy/automonetize.service)"
        pid = self.ctl.supervisor_pid()
        if pid and not self.ctl.launchd_managed():
            os.kill(pid, signal.SIGTERM)  # hand over to launchd: never two supervisors at once
            for _ in range(120):
                if not self.ctl.supervisor_pid():
                    break
                self.sleep(0.5)
        code, out = sh(self.run, str(ROOT / "deploy" / "install_launchd.sh"), "--service", "agent", timeout=120)
        if code != 0:
            return f"install_launchd.sh failed: {out[-300:]}"
        for _ in range(60):
            if self.ctl.supervisor_pid():
                return "installed: the agent now starts at login and restarts if it stops"
            self.sleep(0.5)
        return "installed; the agent should be running within a minute (launchctl print gui/$(id -u)/com.automonetize.agent)"

    def update_code(self) -> str:
        code, dirty = sh(self.run, "git", "status", "--porcelain", "--untracked-files=no")
        if code != 0 or dirty:
            return "not updated: local changes in the folder (run `git status`)"
        code, out = sh(self.run, "git", "pull", "--ff-only", timeout=120)
        if code != 0:
            return f"git pull failed: {out[-300:]}"
        result = self.ctl.restart() if self.ctl.supervisor_pid() or self.ctl.launchd_managed() else self.ctl.start()
        return f"updated and restarted: {result.get('message', '')}"

    # -- checks -----------------------------------------------------------------------------------
    def checks(self, deep: bool = True) -> list[Finding]:
        from gui.control import engine_state
        from tools.catalog import live_products

        cfg, state = self.config, self.state
        out: list[Finding] = []
        pid = self.ctl.supervisor_pid()
        out.append(Finding("Agent running", "ok", f"pid {pid}") if pid else
                   Finding("Agent running", "fail", "the agent isn't running", "automonetize doctor --fix (or Start engine)",
                           self.start_agent))
        if self.system == "Darwin":
            out.append(Finding("Starts after a reboot", "ok", "launchd job installed") if self.ctl.launchd_managed() else
                       Finding("Starts after a reboot", "warn", "after a restart you'd have to start it by hand",
                               "automonetize autostart", self.enable_autostart))
        es, reason = engine_state(state, cfg)
        if es != "running" and pid:
            hint = {"paused": "automonetize resume", "stopped": "automonetize resume (clears the kill switch)",
                    "offline": "check the Mac's internet connection", "quarantined": "it heals itself; details: automonetize status"}
            out.append(Finding("Engine", "warn", f"{es}: {reason}", hint.get(es, "automonetize resume")))
        last = state.get("last_cycle_at")
        if pid and last:
            age = state.clock() - datetime.fromisoformat(last)
            if age > timedelta(seconds=2 * cfg.interval_seconds + 600):
                out.append(Finding("Last cycle", "warn", f"{int(age.total_seconds() // 3600)}h ago", "automonetize doctor --fix",
                                   lambda: self.ctl.restart().get("message", "")))
        live_key = str(cfg.stripe_secret_key or "").startswith(("sk_live_", "rk_live_"))
        out.append(Finding("Real payments", "ok", "live Stripe key, real email") if live_key and not cfg.dry_run else
                   Finding("Real payments", "warn", "test mode or dry run: nothing can be sold", "automonetize go-live"))
        products = live_products(state)
        out.append(Finding("On sale", "ok", "; ".join(f"{p['title']} ${p['price_cents'] / 100:.2f} {p['url']}" for p in products))
                   if products else Finding("On sale", "warn", "no live checkout yet", "automonetize go-live (then wait a cycle)"))
        try:
            from tools import build_toolkit
            from tools.circuit_breaker import CircuitBreaker

            problems = build_toolkit(cfg, state, CircuitBreaker(1000, 1000, 1000)).dispatcher.compliance_problems("delivery")
        except Exception as exc:  # noqa: BLE001
            problems = [repr(exc)]
        out.append(Finding("Email settings", "ok", "complete") if not problems else
                   Finding("Email settings", "fail", "; ".join(problems), "control panel → Settings → Delivery mailer"))
        marketing = [n for n, ok in (("public site", cfg.github_pages_repo and cfg.pages_base_url), ("Dev.to articles", cfg.devto_api_key))
                     if not ok]
        out.append(Finding("Marketing", "ok", f"site {cfg.pages_base_url}") if not marketing else
                   Finding("Marketing", "warn", f"not connected: {', '.join(marketing)}", "automonetize connect-marketing"))
        if self.system == "Darwin":
            code, pm = sh(self.run, "pmset", "-g")
            m = re.search(r"^\s*sleep\s+(\d+)", pm, re.M)
            if code == 0 and m and m.group(1) != "0":
                out.append(Finding("Mac stays awake", "warn", f"the Mac sleeps after {m.group(1)} min, pausing the agent",
                                   "sudo pmset -a sleep 0 disksleep 0 autorestart 1"))
            elif code == 0 and m:
                out.append(Finding("Mac stays awake", "ok", "system sleep off"))
        if deep:
            sh(self.run, "git", "fetch", "--quiet", timeout=60)
            code, behind = sh(self.run, "git", "rev-list", "--count", "HEAD..@{u}")
            if code == 0 and behind.strip().isdigit() and int(behind) > 0:
                out.append(Finding("Up to date", "warn", f"{behind.strip()} update(s) available", "automonetize doctor --fix",
                                   self.update_code))
            elif code == 0:
                out.append(Finding("Up to date", "ok", "latest version"))
        from strategies.finance import describe

        fin = state.get("stripe_finance")
        if fin:
            out.append(Finding("Money", "warn" if fin.get("error") else "ok", describe(fin).replace("Stripe balance: ", "")))
        from strategies.freshness_guard import stale_titles
        from strategies.storefront_health import KEY as HEALTH, describe as describe_health

        health = state.get(HEALTH)
        if health:
            bad = any(not p["ok"] for p in health.get("products", []))
            out.append(Finding("Checkout & downloads", "warn" if bad else "ok", describe_health(health),
                               "open the product in Stripe / on your site and fix what's listed" if bad else ""))
        stale = stale_titles(state)
        if stale:
            out.append(Finding("Fresh data", "warn", f"no new job postings lately for: {', '.join(stale)} (not promoted)",
                               "usually a job board changed; the agent keeps retrying and self-evolution can adapt parsers"))
        from strategies.release_gate import held

        for niche, hold in held(state).items():
            out.append(Finding("New versions", "warn", f"{niche}: update held since {hold['since'][:10]} ({hold['reason']}); "
                               "buyers get the previous version", "nothing to do unless it persists; see the Logs tab"))
        if not cfg.heartbeat_url:
            out.append(Finding("Heartbeat", "warn", "nobody is told if the Mac or the agent stops",
                               "automonetize heartbeat (free, 2 minutes)"))
        else:
            beat = state.get("last_heartbeat_at")
            fresh = beat and state.clock() - datetime.fromisoformat(beat) < timedelta(seconds=2 * cfg.interval_seconds + 600)
            out.append(Finding("Heartbeat", "ok" if fresh else "warn",
                               f"last ping {beat[:16]}" if beat else "no ping sent yet",
                               "" if fresh else "check the URL with: automonetize heartbeat <url>"))
        disk = state.get("disk") or {}
        if disk:
            low = disk["free_gb"] < float(cfg.disk_warn_gb)
            out.append(Finding("Disk space", "warn" if low else "ok", f"{disk['free_gb']:.1f} GB free of {disk['total_gb']:.0f} GB",
                               "empty the Trash and delete large downloads" if low else ""))
        ops = state.get("ops") or {}
        down = [s for s, h in (ops.get("sources") or {}).items() if not h["ok"]]
        if ops.get("sources"):
            out.append(Finding("Job sources", "warn" if down else "ok",
                               f"no postings lately from: {', '.join(down)}" if down else f"{len(ops['sources'])} answering",
                               "nothing to do unless all fail; self-evolution can adapt parsers" if down else ""))
        if ops.get("clock_skew_s") is not None:
            off = abs(float(ops["clock_skew_s"])) > 120
            out.append(Finding("Clock", "warn" if off else "ok", f"{ops['clock_skew_s']:+.0f}s from internet time",
                               "System Settings → General → Date & Time → Set time automatically" if off else ""))
        if ops.get("battery_saving"):
            out.append(Finding("Power", "warn", "on battery: dataset and site builds wait for power", "plug the Mac in"))
        from agent.config_check import problems as config_problems

        for p in config_problems(cfg, ROOT / "automonetize.toml"):
            out.append(Finding("Settings", "warn", p, "fix it in automonetize.toml or control panel → Settings"))
        hook = state.get("webhook_health") or {}
        if hook:
            out.append(Finding("Stripe webhook", "warn" if hook.get("problems") else "ok",
                               "; ".join(hook.get("problems") or []) or (f"repaired: {hook['repaired']}" if hook.get("repaired")
                                                                         else f"healthy (checked {hook['at'][:16]})"),
                               "automonetize setup-autonomous --live" if hook.get("problems") else ""))
        pay = state.get("payment_guard") or {}
        if pay.get("tax_status") and pay["tax_status"] != "active":
            out.append(Finding("Sales tax", "warn", f"Stripe Tax is {pay['tax_status']}: no tax is collected",
                               "if you must collect sales tax/VAT where you or your buyers are, set up Stripe → Tax; "
                               "the agent then applies it to every link by itself"))
        site = state.get("site_audit") or {}
        if site.get("broken_links") or site.get("seo"):
            issues = (site.get("broken_links") or []) + (site.get("seo") or [])
            out.append(Finding("Website", "warn", f"{len(site.get('broken_links') or [])} broken link(s), "
                               f"{len(site.get('seo') or [])} SEO note(s); e.g. {issues[0]}", "usually fixed by the next update"))
        sec = state.get("security_audit") or {}
        for f in sec.get("findings", []):
            out.append(Finding(f"Security: {f['name']}", "fail" if f["status"] == "fail" else "warn", f["detail"], f["fix"]))
        from tools.contact_policy import paused

        pause = paused(state)
        if pause:
            out.append(Finding("Marketing email", "warn", f"paused until {pause['until'][:16]}: {pause['reason']}",
                               "it resumes by itself; bounced addresses are already suppressed"))
        from agent.self_update import info as update_info

        upd = update_info(state)
        if not cfg.auto_update:
            out.append(Finding("Self-update", "warn", "off: new versions are only installed by hand", "automonetize doctor --fix"))
        elif upd.get("status"):
            bad = upd["status"].startswith(("not updating", "update ")) and "upstream" not in upd["status"]
            out.append(Finding("Self-update", "warn" if bad else "ok", upd["status"][:200],
                               "automonetize doctor --fix" if bad else ""))
        pace = state.get("goal_pace")
        if pace:
            out.append(Finding("Goal pace", "ok" if (pace.get("progress") or 0) >= 1 else "warn",
                               f"${pace['net_per_day_cents'] / 100:,.2f}/day of ${pace['goal_cents'] / 100:,.2f}", pace["next_step"]))
        last_backup = state.get("last_backup_at")
        if last_backup and state.clock() - datetime.fromisoformat(last_backup) < timedelta(days=2):
            out.append(Finding("Backups", "ok", f"last backup {last_backup[:16]}"))
        else:
            out.append(Finding("Backups", "warn", "no backup in the last 2 days", "automonetize backup"))
        since = (state.clock() - timedelta(hours=24)).isoformat(timespec="seconds")
        rows = state._all("SELECT source, message FROM errors WHERE kind IN ('operational_failure', 'alert') AND created_at >= ? "
                          "ORDER BY id DESC LIMIT 3", (since,))
        out.append(Finding("Last 24 hours", "ok", "no problems") if not rows else
                   Finding("Last 24 hours", "warn", " | ".join(f"{r['source']}: {r['message'][:120]}" for r in rows),
                           "usually transient; see the Logs tab in the control panel"))
        return out


def controller(env: dict[str, str]):
    from agent.config import Config
    from agent.state import StateStore
    from gui.control import ServiceController

    config = Config.load(env=env)
    state = StateStore(config.db_path)
    return config, state, ServiceController(config, state, ROOT, ROOT / ".env")


def report(findings: list[Finding]) -> int:
    marks = {"ok": (GREEN, "✔"), "warn": (YELLOW, "!"), "fail": (RED, "✘")}
    for f in findings:
        colour, mark = marks[f.status]
        say(colour, f"{mark} {f.name}: {f.detail}")
        if f.status != "ok" and f.fix:
            print(f"    fix: {f.fix}")
    bad = sum(f.status != "ok" for f in findings)
    say(GREEN if not bad else YELLOW, "\nAll good." if not bad else f"\n{bad} thing(s) to look at.")
    return 1 if any(f.status == "fail" for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    from agent.setup_autonomous import load_env_into

    argv = sys.argv[1:] if argv is None else argv
    os.chdir(ROOT)
    config, state, ctl = controller(load_env_into(ROOT / ".env"))
    doc = Doctor(config, state, ctl)
    if argv[:1] == ["autostart"]:
        say(BOLD, "Making the agent start by itself after a restart...")
        msg = doc.enable_autostart()
        say(GREEN if msg.startswith("installed") else RED, msg)
        return 0 if msg.startswith("installed") else 1
    say(BOLD, "AutoMonetize health check")
    findings = doc.checks()
    if "--fix" in argv:
        fixes = [f for f in findings if f.status != "ok" and f.auto]
        for f in fixes:
            say(BOLD, f"→ fixing {f.name}...")
            print(f"    {f.auto()}")
        if fixes:
            say(BOLD, "\nAfter fixes:")
            config, state, ctl = controller(load_env_into(ROOT / ".env"))
            findings = Doctor(config, state, ctl).checks(deep=False)
    return report(findings)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
