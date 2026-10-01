"""Phase 119: the pieces that must agree with each other, checked on every test run.

Each of these drifted at least once while the agent grew: a public route the tunnel didn't
expose (download links), a CLI command missing from the README, a setting nobody explained.
"""

import re
from pathlib import Path

from agent import tunnel
from agent.engine import PLAN, Engine
from agent.power import NullBackend, PowerManager
from tests import test_business_ops
from tools.config_docs import settings
from tools.deps import check, requirements
from tools.storefront.webhook_listener import WebhookProcessor, build_app

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_every_planned_task_has_a_handler_and_every_handler_is_planned(config, state):
    eng = Engine(config, state=state, online_check=lambda: True)
    names = [t for t, _ in PLAN]
    assert len(names) == len(set(names)), "a task is planned twice"
    assert set(names) - set(eng.handlers) <= {"sync_revenue"}, "planned task without a handler"
    assert set(eng.handlers) <= set(names), "handler whose task is never planned"


def test_every_public_route_goes_through_the_tunnel(kit, config):
    config.api_enabled = True
    config.lead_magnet_enabled = True
    config.copy_bandit_enabled = True
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))
    rules = []
    for p in tunnel.EXPOSED_PATHS:
        clean = "/" + p.lstrip("/")
        rules.append(re.compile(f"^{re.escape(clean[:-1])}.*$" if clean.endswith("/*") else f"^{re.escape(clean)}$"))
    unrouted = []
    for resource in app.router.resources():
        path = resource.canonical
        sample = re.sub(r"\{[^}]+\}", "x", path)
        if not any(r.match(sample) for r in rules):
            unrouted.append(path)
    assert unrouted == [], f"served by the agent but not exposed by the tunnel: {unrouted}"


def test_every_cli_command_is_in_the_readme():
    from dashboard.cli import build_parser

    parser = build_parser()
    commands = next(a for a in parser._subparsers._group_actions).choices  # noqa: SLF001
    missing = [c for c in commands if f"automonetize {c}" not in README]
    assert missing == [], f"add these to the README: {missing}"


def test_every_setting_is_explained():
    rows = settings()
    assert len(rows) > 200
    unexplained = [r["name"] for r in rows if not r["help"] and f"`{r['name']}`" not in README and r["name"] not in README]
    assert unexplained == [], f"add a comment in agent/config.py for: {unexplained}"


def test_settings_docs_never_show_a_secret(config):
    from agent.config import Config

    defaults = {r["name"]: r["default"] for r in settings()}
    assert all(not str(defaults[f]).startswith(("sk_", "rk_", "ghp_")) for f in defaults)
    assert Config().stripe_secret_key == "" and "stripe_secret_key" in defaults


def test_requirements_are_installed():
    assert ("aiohttp", "3.9") in requirements()
    assert check() == [], "run pip install -e '.[dev]'"


def test_dependency_check_reports_old_and_missing(tmp_path):
    from importlib import metadata

    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\ndependencies = ["rich>=13", "aiohttp>=99", "nope-pkg>=1"]\n'
                  '[project.optional-dependencies]\ndev = ["pytest>=7"]\n')

    def installed(name):
        if name == "nope-pkg":
            raise metadata.PackageNotFoundError(name)
        return {"rich": "13.7.1", "aiohttp": "3.10.0", "pytest": "8.0"}[name]

    assert check(py, installed) == [{"package": "aiohttp", "need": ">=99", "have": "3.10.0"},
                                    {"package": "nope-pkg", "need": ">=1", "have": ""}]


def test_feature_switches_are_real_settings():
    from dataclasses import fields as dc_fields

    from agent.config import Config
    from tools.features import FEATURES

    names = {f.name for f in dc_fields(Config)}
    assert [flag for _, _, flag, _ in FEATURES if flag and flag not in names] == []
    assert len({name for _, name, _, _ in FEATURES}) == len(FEATURES)


def test_every_public_route_sends_the_baseline_security_headers(kit, config):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    config.api_enabled = config.lead_magnet_enabled = config.copy_bandit_enabled = True
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))
    probes = []
    for resource in app.router.resources():
        path = re.sub(r"\{[^}:]+(:[^}]+)?\}", "x", resource.canonical)
        for route in resource:
            if route.method in ("GET", "POST"):
                probes.append((route.method, path))

    async def go():
        async with TestClient(TestServer(app)) as client:
            missing = []
            for method, path in probes:
                resp = await client.request(method, path, data=b"")
                if resp.headers.get("X-Content-Type-Options") != "nosniff":
                    missing.append(f"{method} {path} -> {resp.status}")
            return missing

    assert len(probes) > 15
    assert asyncio.run(go()) == []


def test_every_task_is_in_the_readme_task_table():
    from agent.engine import PLAN

    table = README.split("## Every task in a cycle", 1)[1].split("\n## ", 1)[0]
    assert [t for t, _ in PLAN if f"`{t}`" not in table] == []


def test_every_env_variable_is_documented():
    """Phase 389: each variable the agent reads from .env is in .env.example (or explained in the README)."""
    from agent.config import _PLAIN_ENV

    example = (ROOT / ".env.example").read_text()
    missing = [k for k in _PLAIN_ENV if k not in example]
    assert missing == [], f"add to .env.example: {missing}"
