"""``automonetize audit`` (Phase 144): every self-check in one report.

Runs, without contacting anything outside the Mac:

* the security self-audit (report only: nothing is changed by this command);
* settings (``agent/config_check.py``) and installed libraries (``tools/deps.py``);
* the tunnel config against the routes the agent serves;
* the last site audit (broken links, SEO, accessibility);
* the last backup restore test, resting tasks, muted alerts and emails the lint held back.

Each finding is ``fail``, ``warn`` or ``ok``; the command exits 1 if anything failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def run_audit(config: Any, state: Any, root: Path = ROOT, run: Any = None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(area: str, status: str, detail: str) -> None:
        out.append({"area": area, "status": status, "detail": detail})

    from agent.security_audit import audit

    kwargs = {"run": run} if run else {}
    for f in audit(config, root, fix=False, **kwargs):
        add(f"security: {f['name']}", f["status"], f["detail"])
    from agent.config_check import problems

    found = problems(config, root / "automonetize.toml")
    for p in found:
        add("settings", "warn", p)
    if not found:
        add("settings", "ok", "no problems")
    from tools.deps import check, describe

    outdated = check()
    add("libraries", "fail" if outdated else "ok", describe(outdated) or "all requirements installed")
    if config.lead_capture_base:
        from agent.tunnel import missing_paths

        gone = missing_paths()
        add("tunnel routes", "warn" if gone else "ok", f"not routed: {', '.join(gone)}" if gone else "all public routes routed")
    site = state.get("site_audit") or {}
    if site:
        broken = site.get("broken_links") or []
        add("website", "fail" if broken else "ok", f"{len(broken)} broken link(s), {len(site.get('seo') or [])} SEO and "
                                                   f"{len(site.get('a11y') or [])} accessibility note(s)")
    verified = state.get("backup_verified") or {}
    if verified:
        add("backup restore test", "ok" if verified.get("ok") else "fail", str(verified.get("detail", "")))
    from agent.task_cooldown import resting
    from tools.alert_mute import active

    for task, until in resting(state).items():
        add("tasks", "warn", f"{task} resting until {until[:16]}")
    for source, until in active(state).items():
        add("alerts", "warn", f"{source} muted until {until[:16]}")
    held = state._all("SELECT message FROM errors WHERE source = 'email_lint' ORDER BY id DESC LIMIT 3")
    for h in held:
        add("email lint", "warn", h["message"][:200])
    order = {"fail": 0, "warn": 1, "ok": 2}
    return sorted(out, key=lambda f: order.get(f["status"], 3))
