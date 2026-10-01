"""Phases 315-319: downloads, rows, pages, structured data, parsers that survive junk."""

import io
import zipfile

from strategies.inbound_syndicator import InboundSyndicator
from tests import test_business_ops
from tests.test_business_ops import ctx
from tests.test_catalog_hygiene import made_product
from tests.test_ops_robustness import alerts
from tools import qa

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_a_healthy_catalog_and_site_pass(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/d"
    made_product(kit, state, clock, transport)
    InboundSyndicator().run("build_site", ctx(kit))
    results = qa.run_all(state, kit.files)
    assert results == {"downloads": [], "pages": [], "parsers": []}, results


def test_problems_are_found(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    state._exec("UPDATE factory_products SET rows = rows + 1 WHERE slug = ?", (made["slug"],))
    assert qa.check_downloads(state, kit.files) == [f"{made['slug']}: CSV has {made['rows']} rows, the product says {made['rows'] + 1}"]
    path = state.get_asset(made["asset_id"])["path"]
    data = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(path))) as src, zipfile.ZipFile(data, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, src.read(n) + (b"!" if n.endswith("README.md") else b""))
    kit.files.write_bytes(path, data.getvalue())
    assert qa.check_downloads(state, kit.files) == [f"{made['slug']}: download damaged (checksums)"]
    kit.files.write_text("site/bad/index.html", '<html><head><title>x</title></head><body><h1>a</h1><h1>b</h1>'
                                                 '<script type="application/ld+json">{"name": 1}</script></body></html>')
    problems = qa.check_pages(kit.files)
    assert "bad/index.html: missing lang, description, one h1" in problems and "bad/index.html: JSON-LD without @type" in problems


def test_parsers_survive_junk():
    assert qa.fuzz_parsers(rounds=400, seed=11) == []


def test_weekly_run_alerts_once(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    state._exec("UPDATE factory_products SET rows = 999 WHERE slug = ?", (made["slug"],))
    assert qa.weekly(state, kit.files) and qa.weekly(state, kit.files) == []
    assert sum("Quality check: 1 problem(s)" in a for a in alerts(state)) == 1


def test_cli(kit, state, monkeypatch, capsys):
    from cli import growth

    monkeypatch.setattr(growth, "_setup", lambda: (kit.config, state, kit.files))
    assert growth.qa_main([]) == 0
    assert "✔ downloads: all good" in capsys.readouterr().out
