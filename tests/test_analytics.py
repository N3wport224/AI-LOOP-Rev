import json
import re
from datetime import timedelta

import pytest
from rich.console import Console

from dashboard.analytics import compute, monthly_amount, parse_period, render
from tests.conftest import NOW
from tools.attribution import (
    LANDER_ATTRIBUTION_JS, add_utm, checkout_link, clean, decode_ref, encode_ref, parse_utm,
)
from tools.storefront import Order


# ------------------------------------------------------------------ UTM & client_reference_id
def test_utm_tagging_and_parsing():
    url = add_utm("https://me.github.io/python-remote/?ref=x", "devto", "syndication", "radar-w40")
    assert parse_utm(url) == {"source": "devto", "medium": "syndication", "campaign": "radar-w40"}
    assert "ref=x" in url
    # first touch wins: existing tags are not overwritten
    assert parse_utm(add_utm(url, "rss", "feed"))["source"] == "devto"
    assert parse_utm("https://x/") == {"source": "", "medium": "", "campaign": ""}
    assert add_utm("", "devto") == ""


def test_client_reference_roundtrip_and_stripe_constraints():
    ref = encode_ref("Dev.to", "Radar Python/Remote 2026-W40")
    assert ref == "am--dev_to--radar_python_remote_2026_w40"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,200}", ref)  # Stripe's allowed charset and length
    assert decode_ref(ref) == ("dev_to", "radar_python_remote_2026_w40")
    assert decode_ref(None) == ("direct", "") and decode_ref("someone-elses-ref") == ("direct", "")
    assert decode_ref(encode_ref("", "")) == ("direct", "")
    assert len(encode_ref("x" * 500, "y" * 500)) <= 200
    link = checkout_link("https://buy.stripe.com/abc", "github", "hn_4100")
    assert link == "https://buy.stripe.com/abc?client_reference_id=am--github--hn_4100"


def test_lander_script_mirrors_python_encoding():
    js = LANDER_ATTRIBUTION_JS
    assert "replace(/[^a-z0-9_]/g,'_').slice(0,60)" in js and "'am--'" in js and "a[data-checkout]" in js
    assert clean("Dev.to") == "dev_to"  # same transformation as the JS regex


@pytest.mark.parametrize("text,days", [("7d", 7), ("30d", 30), ("12w", 84), ("3m", 90), ("1y", 365)])
def test_parse_period(text, days):
    assert parse_period(text) == timedelta(days=days)


@pytest.mark.parametrize("bad", ["", "30", "0d", "-5d", "30x", "month"])
def test_parse_period_rejects(bad):
    with pytest.raises(ValueError):
        parse_period(bad)


def test_monthly_normalisation():
    assert monthly_amount(1000, "month") == 1000
    assert round(monthly_amount(300, "week")) == 1300
    assert round(monthly_amount(12000, "year")) == 1000


# ------------------------------------------------------------------ calculations
@pytest.fixture
def seeded(state, toolkit, make_hypothesis, clock):
    py = make_hypothesis("python-remote")
    ai = make_hypothesis("ai-infrastructure")
    a_py = state.add_asset(py["id"], "lead_directory", "Py", "p.zip", 1, 10, 900)
    a_ai = state.add_asset(ai["id"], "lead_directory", "AI", "a.zip", 1, 10, 1400)
    a_sub = state.add_asset(py["id"], "subscription", "Py sub", "p.zip", 1, 10, 1000)
    for aid, niche in ((a_py, "python-remote"), (a_ai, "ai-infrastructure"), (a_sub, "python-remote")):
        state.update_asset(aid, niche=niche)
    t = NOW - timedelta(days=2)
    orders = [
        Order("stripe", "cs_1", "a@x.com", 900, None, t.isoformat(), asset_id=a_py, channel="devto", campaign="w40"),
        Order("stripe", "cs_2", "b@x.com", 900, None, t.isoformat(), asset_id=a_py, channel="devto"),
        Order("stripe", "cs_3", "c@x.com", 1400, None, t.isoformat(), asset_id=a_ai, channel="rss"),
        Order("stripe", "in_1", "d@x.com", 1000, None, t.isoformat(), asset_id=a_sub, channel="github", kind="subscription", status="subscription"),
        Order("stripe", "cs_old", "e@x.com", 900, None, (NOW - timedelta(days=45)).isoformat(), asset_id=a_py, channel="devto"),
    ]
    toolkit.revenue.record_orders(orders, 2.9, 30)
    toolkit.revenue.record_manual(500, verified=True, occurred_at=t.isoformat(), external_id="bank-1")
    for i in range(4):
        state.upsert_checkout_session(f"s_dev_{i}", "plink", None, "complete" if i < 2 else "expired", None, None, 900, channel="devto")
    state.upsert_checkout_session("s_rss", "plink", None, "complete", None, None, 1400, channel="rss")
    state.upsert_checkout_session("s_dir", "plink", None, "open", None, None, 900)
    # subscribers: 2 active before the period, one cancels inside it; one new weekly subscriber
    clock.now = NOW - timedelta(days=60)
    state.upsert_subscriber("stripe", "sub_a", niche="python-remote", price_cents=1000, interval="month")
    state.upsert_subscriber("stripe", "sub_b", niche="python-remote", price_cents=1000, interval="month")
    clock.now = NOW - timedelta(days=10)
    state.upsert_subscriber("stripe", "sub_b", status="canceled")
    state.upsert_subscriber("stripe", "sub_c", niche="python-remote", price_cents=300, interval="week")
    clock.now = NOW
    return {"py": py, "ai": ai}


def test_breakdowns_by_niche_tier_and_channel(state, config, seeded):
    r = compute(state, config, "30d")
    fee = lambda g: round(g * 0.029) + 30  # noqa: E731
    assert r["totals"]["orders"] == 5 and r["totals"]["gross_cents"] == 900 + 900 + 1400 + 1000 + 500
    assert r["by_niche"]["python-remote"] == {"orders": 3, "gross_cents": 2800, "net_cents": 2 * (900 - fee(900)) + 1000 - fee(1000)}
    assert r["by_niche"]["ai-infrastructure"]["orders"] == 1
    assert r["by_niche"]["unattributed"]["net_cents"] == 500  # manual entry
    assert set(r["by_tier"]) == {"$9.00 one-off", "$14.00 one-off", "subscription $10.00", "manual entry"}
    ch = r["by_channel"]
    assert ch["devto"]["orders"] == 2 and ch["devto"]["checkouts_started"] == 4 and ch["devto"]["conversion"] == 0.5
    assert ch["rss"]["conversion"] == 1.0 and ch["github"]["orders"] == 1 and ch["manual"]["orders"] == 1
    assert ch["direct"]["checkouts_started"] == 1 and ch["direct"]["conversion"] == 0.0 and ch["direct"]["orders"] == 0
    assert list(ch)[0] == "devto"  # sorted by net revenue


def test_goal_mrr_and_churn(state, config, seeded):
    r = compute(state, config, "30d")
    net = r["totals"]["net_cents"]
    assert r["net_per_day_cents"] == round(net / 30) and r["goal_cents"] == 1000
    assert r["days_goal_met"] == 1  # all revenue landed on one day and cleared $10
    s = r["subscriptions"]
    assert s["active"] == 2 and s["new"] == 1 and s["canceled"] == 1
    assert s["mrr_cents"] == 1000 + round(300 * 52 / 12)
    assert s["active_at_start"] == 2 and s["churn_rate"] == 0.5
    assert s["mrr_daily_cents"] == round(s["mrr_cents"] * 12 / 365)
    # the 45-day-old order only shows up in a longer window
    assert compute(state, config, "90d")["totals"]["orders"] == 6
    assert compute(state, config, "all")["totals"]["orders"] == 6


def test_empty_database(state, config):
    r = compute(state, config, "7d")
    assert r["totals"]["orders"] == 0 and r["subscriptions"]["churn_rate"] is None and r["by_channel"] == {}
    console = Console(record=True, width=140)
    console.print(render(r))
    assert "no verified revenue" in console.export_text()


def test_webhook_and_polling_attribution_reach_orders(state, toolkit, transport, config):
    from tests.test_webhook import SECRET, event, session
    from tools.storefront.stripe_pages_publisher import StripeStorefront
    from tools.storefront.webhook_listener import WebhookProcessor, sign_payload

    config.stripe_webhook_secret = SECRET
    p = event("checkout.session.completed", session(sid="cs_w", client_reference_id="am--hashnode--w40"))
    WebhookProcessor(toolkit).handle(p, sign_payload(p, SECRET))
    assert state.get_order("stripe", "cs_w")["channel"] == "hashnode"
    config.stripe_secret_key = "sk"
    transport.add_json("https://api.stripe.com/v1/checkout/sessions", {"has_more": False, "data": [
        {"id": "cs_p", "payment_status": "paid", "amount_total": 900, "created": int(NOW.timestamp()),
         "client_reference_id": "am--rss--feed", "customer_details": {"email": "x@y.com"}}]})
    orders = StripeStorefront(config, toolkit.http).fetch_orders(["plink_1"], NOW - timedelta(days=1))
    assert (orders[0].channel, orders[0].campaign) == ("rss", "feed")


def test_cli_analytics(tmp_path, monkeypatch, seeded, state, config):
    from dashboard.cli import main

    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[automonetize]\ndata_dir = "{config.data_dir}"\nnetwork_check_hosts = []\n')
    console = Console(record=True, width=160)
    assert main(["-c", str(cfg), "analytics", "--period=30d"], console=console) == 0
    out = console.export_text()
    for s in ("Acquisition channel", "Niche", "Pricing tier", "devto", "python-remote", "$9.00 one-off", "MRR", "churn 50.0%"):
        assert s in out, s
    assert main(["-c", str(cfg), "analytics", "--period=bogus"], console=console) == 2


def test_cli_analytics_json(tmp_path, monkeypatch, seeded, config, capsys):
    from dashboard.cli import main

    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[automonetize]\ndata_dir = "{config.data_dir}"\n')
    capsys.readouterr()
    main(["-c", str(cfg), "analytics", "--period", "7d", "--json"], console=Console(record=True))
    data = json.loads(capsys.readouterr().out)
    assert data["period"] == "7d" and "by_channel" in data and data["subscriptions"]["mrr_cents"] > 0
