"""Phases 375-379: how current each factory product is."""

import csv
import io
from datetime import timedelta

from strategies import factory_freshness as ff
from strategies.base import TaskContext
from strategies.catalog_insight import catalog_csv
from strategies.freshness_guard import FreshnessGuard, promotable
from strategies.inbound_syndicator import site_pages
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product
from tests.test_ops_robustness import alerts
from tools.page_builder import render_product_page

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_measure():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    files = {"leads.csv": b"company,posted_at\nA,2026-09-29T00:00:00+00:00\nB,2026-09-21T00:00:00+00:00\nC,2026-09-30\n"}
    assert ff.measure(files, now) == {"newest": "2026-09-30", "median_days": 2.0, "built": "2026-10-01"}
    assert ff.measure({"companies.csv": b"company,latest_posting\nA,2026-09-01\n"}, now)["median_days"] == 30.0
    assert ff.measure({"leads.csv": b"company\nA\n"}, now) == {}


def test_recorded_shown_and_exported(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)  # postings from 3 days ago
    row = state._one("SELECT newest_posting, median_age_days FROM factory_products WHERE slug = ?", (made["slug"],))
    assert row["newest_posting"] == (clock() - timedelta(days=3)).date().isoformat() and row["median_age_days"] == 3.0
    page = next(p for p in site_pages(kit) if p.slug == made["slug"])
    assert "half are from the last 3 days" in render_product_page(page)
    rows = list(csv.DictReader(io.StringIO(catalog_csv(state))))
    assert rows[0]["newest_posting"] == row["newest_posting"] and rows[0]["median_posting_age_days"] == "3.0"


def test_stale_factory_products_are_not_promoted(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    hyp = {"id": 1, "params": {"niche": "x"}, "iterations": 1}
    guard = FreshnessGuard()
    guard.run("guard_freshness", TaskContext(kit, hyp, {}))
    assert any(p["id"] == made["asset_id"] for p in promotable(state))
    clock.advance(days=ff.REFRESH_STALE_DAYS + 1)  # the weekly refresh never happened
    guard.run("guard_freshness", TaskContext(kit, hyp, {}))
    assert not any(p["id"] == made["asset_id"] for p in promotable(state))
    assert any("looks out of date (not refreshed since" in a for a in alerts(state))
    state._exec("UPDATE factory_products SET refreshed_at = ? WHERE slug = ?", (state.now(), made["slug"]))
    state._exec("UPDATE factory_products SET newest_posting = ? WHERE slug = ?", ((clock() - timedelta(days=2)).isoformat(), made["slug"]))
    guard.run("guard_freshness", TaskContext(kit, hyp, {}))
    assert any(p["id"] == made["asset_id"] for p in promotable(state))  # up to date again
