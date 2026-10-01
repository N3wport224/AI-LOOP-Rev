"""Phases 65-69: site audit, changelog pages, customer export, lifetime value, uninstall."""

import csv
import io
from datetime import timedelta

import pytest

from tests import test_business_ops
from tests.test_business_ops import ctx, dataset
from tools.customers import customers, describe, export_csv, report
from tools.offer_pages import render_changelog
from tools.page_builder import ProductPage, meta_description, render_product_page
from tools.site_audit import audit_site, record

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def page(**kw):
    base = dict(niche="py", title="Py Intel", summary="s", price_cents=1900, currency="usd", checkout_url="https://buy.stripe.com/py",
                sample_columns=[], sample_rows=[])
    return ProductPage(**{**base, **kw})


# ------------------------------------------------------------------ Phase 65: site audit
GOOD = ('<html><head><title>T</title><meta name="description" content="' + "x" * 80 + '"></head><body><h1>H</h1>'
        '<a href="../">home</a><img src="og.png" alt="card"><a href="https://buy.stripe.com/x">buy</a></body></html>')


def test_audit_finds_broken_links_and_seo_gaps():
    out = {"index.html": GOOD.replace('href="../"', 'href="py/"').replace('src="og.png"', 'src="py/og.png"'), "py/index.html": GOOD, "py/og.png": b"png",
           "about/index.html": '<html><head></head><body><h1>a</h1><h1>b</h1><img src="x.png"><a href="../missing/">x</a>'
                               '<a href="https://me.github.io/d/gone.html">y</a></body></html>'}
    r = audit_site(out, "https://me.github.io/d")
    assert r["broken_links"] == ["about/index.html → ../missing/", "about/index.html → https://me.github.io/d/gone.html",
                                 "about/index.html → x.png"]
    seo = " | ".join(r["seo"])
    assert "about/index.html: no <title>" in seo and "no meta description" in seo and "2 <h1>" in seo and "without alt" in seo
    assert not any(s.startswith(("index.html", "py/")) for s in r["seo"])


def test_new_broken_links_alert_once(state):
    record(state, {"broken_links": ["a → b"], "seo": [], "heavy": []})
    record(state, {"broken_links": ["a → b"], "seo": [], "heavy": []})
    assert len(state.recent_errors(5, kind="alert")) == 1


def test_meta_descriptions_are_cut_at_a_word():
    text = "word " * 60
    cut = meta_description(text)
    assert len(cut) <= 158 and cut.endswith("…") and not cut.endswith(" …")
    assert meta_description("short one") == "short one"


def test_the_built_site_has_no_broken_links(kit, state, config):
    from strategies.inbound_syndicator import InboundSyndicator

    config.pages_base_url = "https://me.github.io/d"
    _, aid = dataset(kit, state, "python-remote")
    state.update_asset(aid, niche="python-remote")
    InboundSyndicator().run("build_site", ctx(kit))
    audit = state.get("site_audit")
    assert audit and audit["broken_links"] == [], audit["broken_links"]


# ------------------------------------------------------------------ Phase 66: changelog pages
def test_changelog_lists_versions_with_growth():
    p = page(versions=[{"version": 3, "date": "2026-09-30", "rows": 140}, {"version": 2, "date": "2026-09-23", "rows": 120},
                       {"version": 1, "date": "2026-09-16", "rows": 100}])
    html = render_changelog(p, "Tech Stack Intel")
    assert "+20" in html and "first release" in html and "Get v3: $19" in html
    assert 'href="changelog/"' in render_product_page(p) and 'href="changelog/"' not in render_product_page(page())


# ------------------------------------------------------------------ Phases 67-68: customers
def buy(state, aid, email, order_id, clock, days_ago, gross=1900, channel=None, status="delivered"):
    state.record_order("stripe", order_id, email, gross, None, aid, None, status=status, channel=channel,
                       occurred_at=(clock() - timedelta(days=days_ago)).isoformat(timespec="seconds"))


def test_customer_list_and_lifetime_value(kit, state, clock):
    _, aid = dataset(kit, state, "python-remote")
    buy(state, aid, "a@co.example", "cs_1", clock, 40, channel="devto")
    buy(state, aid, "a@co.example", "cs_2", clock, 5)
    buy(state, aid, "b@co.example", "cs_3", clock, 3, channel="x")
    buy(state, aid, "c@co.example", "cs_4", clock, 2, status="refunded")
    people = customers(state)
    assert [p["email"] for p in people] == ["a@co.example", "b@co.example"]  # refunded buyers aren't customers
    assert people[0]["purchases"] == 2 and people[0]["spent_cents"] == 3800 and people[0]["first_channel"] == "devto"
    r = report(state)
    assert r["customers"] == 2 and r["repeat_rate"] == 0.5 and r["avg_ltv_cents"] == 2850
    assert r["by_channel"]["devto"] == {"customers": 1, "ltv_cents": 3800}
    assert "50% bought more than once" in describe(r) and "most valuable channel: devto" in describe(r)
    path, n = export_csv(state, kit.files)
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert n == 2 and rows[0]["email"] == "a@co.example" and rows[0]["spent_usd"] == "38.00"


def test_monday_report_has_the_customer_line(kit, state, clock):
    from datetime import datetime, timezone

    from strategies.owner_reports import OwnerReports

    assert "Customers: none yet." in OwnerReports.digest_body(kit, datetime(2026, 10, 5, 9, tzinfo=timezone.utc))


# ------------------------------------------------------------------ Phase 69: uninstall
class Ctl:
    def __init__(self):
        self.killed = False

    def supervisor_pid(self):
        return None if self.killed else 123

    def launchd_managed(self):
        return not self.killed

    def kill(self):
        self.killed = True


def test_uninstall_keeps_data_and_can_deactivate_links(kit, state, config, monkeypatch, tmp_path, transport):
    import cli.uninstall as un
    from tests.test_business_ops import STRIPE

    dataset(kit, state, "python-remote")
    ctl = Ctl()
    monkeypatch.setattr("cli.doctor.controller", lambda env: (config, state, ctl))
    monkeypatch.setattr(un, "ROOT", tmp_path)
    monkeypatch.setattr("tools.build_toolkit", lambda *a, **k: kit)
    transport.add_json(f"{STRIPE}/payment_links/plink_python-remote", {"id": "plink_python-remote", "active": False})
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess_result()

    assert un.main([], confirm=lambda p: "no", run=run, system="Darwin") == 1 and not ctl.killed
    assert un.main(["--deactivate-links"], confirm=lambda p: "UNINSTALL", run=run, system="Darwin") == 0
    assert ctl.killed and calls and calls[0][-1] == "uninstall"
    assert state.get_asset(1)["status"] == "retired" and config.data_dir.exists()


def subprocess_result():
    import subprocess

    return subprocess.CompletedProcess([], 0, "removed com.automonetize.agent\n", "")


@pytest.mark.parametrize("args,expect", [(["customers", "--report"], "cmd_customers"), (["uninstall", "--yes"], "cmd_uninstall")])
def test_cli_entry_points(args, expect):
    from dashboard.cli import build_parser

    assert build_parser().parse_args(args).func.__name__ == expect
