"""Configuration: defaults < TOML file < environment variables."""

from __future__ import annotations

import os
import tomllib
import json
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

DEFAULT_NICHES: list[dict[str, Any]] = [
    {"name": "python-remote", "keywords": ["python", "django", "fastapi", "flask"]},
    {"name": "devops-sre", "keywords": ["devops", "sre", "kubernetes", "terraform", "platform engineer"]},
    {"name": "ml-ai", "keywords": ["machine learning", "ml engineer", "llm", "data scientist", "ai engineer"]},
    {"name": "frontend-react", "keywords": ["react", "frontend", "front-end", "typescript", "next.js"]},
    {"name": "rust-go-systems", "keywords": ["rust", "golang", "go developer", "systems engineer"]},
]

ENV_PREFIX = "AUTOMONETIZE_"


@dataclass
class Config:
    data_dir: Path = Path("data")
    db_filename: str = "agent_state.db"

    # Loop
    interval_seconds: int = 3600
    pivot_after_iterations: int = 24
    max_hypothesis_generations: int = 3

    # Circuit breakers
    max_actions_per_cycle: int = 8
    max_api_calls_per_cycle: int = 60
    max_consecutive_errors: int = 5
    task_retry_attempts: int = 3

    # HTTP
    http_rate_per_minute: float = 20.0
    http_timeout: float = 15.0
    http_backoff_seconds: float = 1.0
    respect_robots_txt: bool = True

    # Shell sandbox
    shell_allowlist: list[str] = field(default_factory=lambda: ["git", "python3", "ls", "cat", "echo", "zip"])
    shell_timeout: float = 30.0

    # Revenue
    daily_target_cents: int = 1000
    gumroad_access_token: str = ""
    platform_fee_pct: float = 10.0
    platform_fee_fixed_cents: int = 50

    # Strategies
    lead_sources: list[str] = field(default_factory=lambda: ["remoteok", "arbeitnow", "hn_hiring"])
    niches: list[dict[str, Any]] = field(default_factory=lambda: [dict(n) for n in DEFAULT_NICHES])
    min_leads_for_asset: int = 10
    asset_price_cents: int = 900
    storefront_url: str = ""

    # Outreach (drafts only, never sent automatically)
    sender_name: str = ""
    sender_email: str = ""
    sender_skills: list[str] = field(default_factory=list)
    outreach_offer: str = "short-term contract help"
    outreach_daily_cap: int = 20
    outreach_min_score: float = 0.6
    outreach_cooldown_days: int = 30

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "EMERGENCY_STOP"

    @property
    def workspace_dir(self) -> Path:
        return self.data_dir / "workspace"

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        values: dict[str, Any] = {}
        path = path or env.get(ENV_PREFIX + "CONFIG")
        if path is None and Path("automonetize.toml").exists():
            path = "automonetize.toml"
        if path:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
            values.update(raw.get("automonetize", raw))

        known = {f.name: f for f in fields(cls)}
        for name, f in known.items():
            key = ENV_PREFIX + name.upper()
            if key in env:
                values[name] = _coerce(env[key], None if f.default is MISSING else f.default, name)
        if "GUMROAD_ACCESS_TOKEN" in env and "gumroad_access_token" not in values:
            values["gumroad_access_token"] = env["GUMROAD_ACCESS_TOKEN"]

        unknown = set(values) - set(known)
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        if "data_dir" in values:
            values["data_dir"] = Path(values["data_dir"])
        return cls(**values)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.workspace_dir):
            d.mkdir(parents=True, exist_ok=True)


_LIST_FIELDS = {"shell_allowlist", "lead_sources", "sender_skills"}


def _coerce(raw: str, default: Any, name: str) -> Any:
    if name in _LIST_FIELDS:
        return [s.strip() for s in raw.split(",") if s.strip()]
    if name == "niches":
        return json.loads(raw)
    if name == "data_dir":
        return Path(raw)
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
