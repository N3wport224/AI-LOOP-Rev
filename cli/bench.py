"""`automonetize bench` (Phase 369): how fast the factory and the site are on this machine.

Runs in a throwaway sandbox (a temporary folder and database, nothing sent anywhere): seeds synthetic
postings, makes products back to back, then prints the timings. Use
it after a big change, or when doctor says factory runs are slow.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def run(products: int = 50, postings: int = 2000) -> dict[str, float]:
    from agent.config import Config
    from agent.state import StateStore
    from strategies import product_factory as pf
    from strategies.b2b_lead_aggregator import POOL_NICHE, TECH_KEYWORDS
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker
    from tools.http_client import Response

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(data_dir=Path(tmp) / "data", dry_run=True, link_checks=False, product_factory=True)
        state = StateStore(Path(tmp) / "bench.db")
        offline = lambda method, url, headers, body, timeout: Response(404, url, b"", {})  # noqa: E731 - no network
        tools = build_toolkit(cfg, state, CircuitBreaker(10**6, 10**6, 10**6), transport=offline, sleep=lambda s: None)
        techs = [t for t in TECH_KEYWORDS if t.isalpha()][:30]
        places = ["Berlin, Germany", "Austin, TX, United States", "Remote", "London, UK", "Toronto, Canada"]
        when = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        start = time.monotonic()
        for i in range(postings):
            tech = techs[i % len(techs)]
            state.upsert_lead(f"bench-{i}", POOL_NICHE, {
                "company": f"Company {i % 400}", "title": "Senior Engineer" if i % 3 else "Engineer",
                "location": places[i % len(places)], "remote": places[i % len(places)] == "Remote",
                "stack": [tech, techs[(i * 7) % len(techs)]], "seniority": "senior" if i % 3 else "mid",
                "url": f"https://jobs.example/{i}", "posted_at": when, "source": "remoteok"})
        seeded = time.monotonic() - start
        from strategies import catalog_reliability

        ticks, cap = [], catalog_reliability.STAGED_MAX
        catalog_reliability.STAGED_MAX = 10**9  # a sandbox can't open checkouts: let products wait without limit
        try:
            for _ in range(products):
                t0 = time.monotonic()
                out = pf.tick(tools, force=True)
                ticks.append(time.monotonic() - t0)
                if not out.get("made"):
                    break
        finally:
            catalog_reliability.STAGED_MAX = cap
            state.close()
    return {"postings": postings, "seed_s": round(seeded, 2), "products": len(ticks),
            "avg_tick_s": round(sum(ticks) / len(ticks), 3) if ticks else 0.0, "max_tick_s": round(max(ticks), 3) if ticks else 0.0}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="automonetize bench")
    p.add_argument("--products", type=int, default=50)
    p.add_argument("--postings", type=int, default=2000)
    args = p.parse_args(argv)
    r = run(args.products, args.postings)
    print(f"Seeded {r['postings']:,} postings in {r['seed_s']}s; made {r['products']} products: "
          f"{r['avg_tick_s']}s average, {r['max_tick_s']}s slowest per product (sandbox, nothing sent).")
    return 0
