from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config  # noqa: E402
from agent.state import StateStore  # noqa: E402
from tools import build_toolkit  # noqa: E402
from tools.circuit_breaker import CircuitBreaker  # noqa: E402
from tools.http_client import Response  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def pytest_configure(config: Any) -> None:
    config.addinivalue_line("markers", "evolved_data: run against the live self-evolved heuristics, not the built-in ones")


@pytest.fixture(autouse=True)
def pristine_evolved_data(request: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests run against the built-in heuristics: whatever the agent has learned (agent/evolution)
    must not change what the existing assertions mean, or every evolution would fail the suite.
    The live evolved data has its own contract tests (``@pytest.mark.evolved_data``)."""
    if request.node.get_closest_marker("evolved_data"):
        return
    import strategies.b2b_lead_aggregator as agg
    import strategies.tech_stack_intel as intel
    from tools import copy_bandit

    monkeypatch.setattr(agg, "FIELD_ALIASES", {})
    if "other" in intel.FINGERPRINTS:
        learned = set(intel.FINGERPRINTS["other"])
        monkeypatch.delitem(intel.FINGERPRINTS, "other")
        monkeypatch.setattr(intel, "_COMPILED", {k: v for k, v in intel._COMPILED.items() if k != "other"})
        monkeypatch.setattr(intel, "_FULL_COMPILED", {k: v for k, v in intel._FULL_COMPILED.items() if k not in learned})
    evolved = {name for name, arm in copy_bandit.SLOTS["headline"].items() if arm.get("evolved")}
    if evolved:
        monkeypatch.setitem(copy_bandit.SLOTS, "headline", {k: v for k, v in copy_bandit.SLOTS["headline"].items() if k not in evolved})
        monkeypatch.setattr(copy_bandit, "CODE_TO_VARIANT", {slot: {c: n for c, n in codes.items() if n not in evolved}
                                                            for slot, codes in copy_bandit.CODE_TO_VARIANT.items()})


class FrozenClock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeTransport:
    """Routes requests by URL prefix. A route is a Response, a list consumed in order, or a callable."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []

    def add(self, prefix: str, response: Any) -> None:
        self.routes[prefix] = response

    def add_json(self, prefix: str, payload: Any, status: int = 200) -> None:
        self.add(prefix, Response(status, prefix, json.dumps(payload).encode(), {}))

    def calls_to(self, prefix: str, method: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["url"].startswith(prefix) and (method is None or c["method"] == method)]

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> Response:
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "timeout": timeout, "body": body})
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                route = self.routes[prefix]
                if isinstance(route, list):
                    item = route.pop(0) if len(route) > 1 else route[0]
                else:
                    item = route
                if callable(item) and not isinstance(item, Response):
                    item = item(method, url, headers)
                if isinstance(item, BaseException):
                    raise item
                return item
        return Response(404, url, b"not found", {})


def remoteok_payload() -> list[dict[str, Any]]:
    return [
        {"legal": "API terms notice"},
        {
            "id": "101", "epoch": int((NOW - timedelta(days=2)).timestamp()), "company": "Acme Inc",
            "position": "Senior Python Engineer", "tags": ["python", "django", "aws"], "location": "Remote - US",
            "url": "https://remoteok.com/remote-jobs/101", "apply_url": "https://acme.example/careers/1",
            "description": "<p>Build APIs with Django &amp; Postgres.</p>", "salary_min": 120000, "salary_max": 160000,
        },
        {
            "id": "102", "epoch": int((NOW - timedelta(days=1)).timestamp()), "company": "Beta LLC",
            "position": "Backend Developer (Python/FastAPI)", "tags": ["python", "fastapi"], "location": "Worldwide",
            "url": "https://remoteok.com/remote-jobs/102", "apply_url": "https://beta.example/jobs",
            "description": "FastAPI, Redis, Kubernetes", "salary_min": 0, "salary_max": 0,
        },
        {
            "id": "103", "epoch": int((NOW - timedelta(days=3)).timestamp()), "company": "Gamma",
            "position": "React Frontend Engineer", "tags": ["react", "typescript"], "location": "Remote",
            "url": "https://remoteok.com/remote-jobs/103", "description": "React + TypeScript",
        },
        {"id": "104", "company": "", "position": "No Company Role", "url": "https://remoteok.com/x"},
    ]


def arbeitnow_payload() -> dict[str, Any]:
    return {
        "data": [
            {
                "slug": "delta-python", "company_name": "Delta GmbH", "title": "Python Developer",
                "description": "Flask and Postgres", "remote": True, "url": "https://www.arbeitnow.com/view/delta-python",
                "tags": ["Python", "Flask"], "job_types": ["full time"], "location": "Berlin",
                "created_at": int((NOW - timedelta(days=4)).timestamp()),
            },
            {
                # duplicate of Acme on another board: same company/title/location after normalisation
                "slug": "acme-dup", "company_name": "ACME, Inc.", "title": "Senior Python Engineer",
                "description": "Django", "remote": True, "url": "https://www.arbeitnow.com/view/acme-dup",
                "tags": ["python"], "job_types": [], "location": "Remote - US",
                "created_at": int((NOW - timedelta(days=2)).timestamp()),
            },
            {
                "slug": "bad-url", "company_name": "Epsilon", "title": "Python Engineer",
                "url": "not-a-url", "tags": ["python"], "location": "Paris", "created_at": 0,
            },
        ]
    }


def hn_items() -> tuple[dict[str, Any], dict[str, Any]]:
    search = {"hits": [{"objectID": "900", "title": "Ask HN: Who is hiring? (September 2026)"}]}
    item = {
        "id": 900,
        "children": [
            {
                "id": 901, "created_at_i": int((NOW - timedelta(days=5)).timestamp()),
                "text": "Zeta Labs | Staff Python Engineer | NYC or REMOTE | https://zeta.example/jobs"
                "<p>We use Python, Django and Kafka. Email jobs@zeta.example</p>",
            },
            {"id": 902, "text": "Just a comment without the header format"},
            {
                "id": 903, "created_at_i": int((NOW - timedelta(days=5)).timestamp()),
                "text": "Eta | Rust Engineer | Remote<p>Rust and tokio.</p>",
            },
        ],
    }
    return search, item


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path / "data",
        respect_robots_txt=False,
        http_backoff_seconds=0.0,
        http_rate_per_minute=6000,
        min_leads_for_asset=2,
        pivot_after_iterations=3,
        max_consecutive_errors=3,
        sender_name="Sam Dev",
        sender_email="sam@example.com",
        sender_skills=["python", "django"],
        niches=[
            {"name": "python-remote", "keywords": ["python", "django", "fastapi", "flask"]},
            {"name": "rust-systems", "keywords": ["rust"]},
        ],
        network_check_hosts=[],
        min_hypothesis_days=0,  # most tests exercise the iteration rules; the age floor has its own tests
        # Phase 1-3 tests were written against this price grid; Phase 4 defaults are tested separately.
        price_tiers=[[0, 500], [25, 900], [75, 1500]],
        price_matrix=[500, 900, 1400, 1900],
        # Phase 7 features have their own suites; older tests keep the single-niche, no-discovery
        # world they were written for (and never touch real DNS).
        max_active_niches=1,
        source_discovery_enabled=False,
    )


@pytest.fixture
def state(config: Config, clock: FrozenClock) -> StateStore:
    config.ensure_dirs()
    s = StateStore(config.db_path, clock=clock)
    yield s
    s.close()


@pytest.fixture
def transport() -> FakeTransport:
    t = FakeTransport()
    t.add_json("https://remoteok.com/api", remoteok_payload())
    t.add_json("https://www.arbeitnow.com/api/job-board-api", arbeitnow_payload())
    search, item = hn_items()
    t.add_json("https://hn.algolia.com/api/v1/search_by_date", search)
    t.add_json("https://hn.algolia.com/api/v1/items/900", item)
    return t


@pytest.fixture
def breaker(config: Config) -> CircuitBreaker:
    return CircuitBreaker(
        max_actions_per_cycle=config.max_actions_per_cycle,
        max_api_calls_per_cycle=config.max_api_calls_per_cycle,
        max_consecutive_errors=config.max_consecutive_errors,
    )


@pytest.fixture
def toolkit(config, state, breaker, transport):
    return build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None)


@pytest.fixture
def make_hypothesis(state: StateStore) -> Callable[..., dict[str, Any]]:
    def _make(niche: str = "python-remote", keywords: list[str] | None = None, iterations: int = 0) -> dict[str, Any]:
        hid = state.create_hypothesis(
            f"lead_directory:{niche}:g1", "lead_directory", f"test {niche}",
            {"niche": niche, "keywords": keywords or ["python", "django", "fastapi", "flask"], "generation": 1},
        )
        for _ in range(iterations):
            state.increment_hypothesis_iterations(hid)
        return state.get_hypothesis(hid)

    return _make


@pytest.fixture
def live(config, state, breaker, transport, make_hypothesis, clock):
    """A published $9 dataset on Stripe; Payment Links are minted as plink_1, plink_2, ...

    Shared by test_pricing and test_audit_regressions."""
    from tests.test_distribution import pipeline

    stripe = "https://api.stripe.com/v1"
    config.stripe_secret_key = "sk_test"
    counter = {"n": 0}

    def link(method, url, headers):
        counter["n"] += 1
        return Response(200, url, json.dumps({"id": f"plink_{counter['n']}", "url": f"https://buy.stripe.com/l{counter['n']}"}).encode())

    transport.add_json(f"{stripe}/products", {"id": "prod_1"})
    transport.add_json(f"{stripe}/prices", {"id": "price_1"})
    transport.add(f"{stripe}/payment_links", link)
    transport.add_json(f"{stripe}/checkout/sessions", {"has_more": False, "data": []})
    kit = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None)
    hyp = make_hypothesis()
    pipeline(kit, hyp)
    a = state.latest_asset(hyp["id"], "lead_directory")
    state.update_asset(a["id"], price_cents=900, product_ref="plink_0", checkout_url="https://buy.stripe.com/l0",
                       provider="stripe", status="published")
    return kit, state.get_hypothesis(hyp["id"]), state.get_asset(a["id"])
