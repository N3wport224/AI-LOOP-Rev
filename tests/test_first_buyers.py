"""Phases 422-428: the first-buyers notebook, channels, product downloads from the panel."""

import io
import zipfile

import pytest

from strategies import prospects as pr
from tests import test_business_ops, test_gui
from tests.test_catalog_hygiene import made_product

kit = test_business_ops.kit
gui = test_gui.gui


def test_the_notebook_shows_which_buyers_say_yes(state, clock):
    a = pr.add(state, "Ana", "LinkedIn", "dev agency", "Rust jobs")
    b = pr.add(state, "Ben", "r/freelance", "freelancer", "Rust jobs")
    pr.add(state, "Cy", "LinkedIn", "dev agency", "Go jobs")
    pr.update(state, a["id"], "bought")
    pr.update(state, b["id"], "not interested", note="wants contacts")
    s = pr.summary(state)
    agency = next(r for r in s["by_buyer"] if r["name"] == "dev agency")
    assert agency == {"name": "dev agency", "messaged": 2, "replied": 1, "interested": 1, "bought": 1}
    assert s["by_buyer"][0]["name"] == "dev agency" and s["total"] == 3
    with pytest.raises(ValueError):
        pr.add(state, "", "x", "dev agency")
    with pytest.raises(ValueError):
        pr.add(state, "Dee", "x", "astronaut")
    with pytest.raises(ValueError):
        pr.update(state, a["id"], "married")
    c = pr.add(state, "Eve", "HN", "freelancer")
    pr.update(state, c["id"], "interested")
    assert not pr.follow_up(state, pr.entries(state)[-1])
    clock.advance(days=pr.FOLLOW_UP_DAYS)
    assert pr.summary(state)["follow_up"] == 1 and "1 waiting for your follow-up" in pr.describe(pr.summary(state))[0]
    assert pr.remove(state, c["id"]) and not pr.remove(state, c["id"])


def test_the_panel_downloads_products_and_samples_and_keeps_the_notebook(gui, kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    asset = state.get_asset(made["asset_id"])

    async def scenario(client):
        csrf = await test_gui.login(client)
        p = next(i for i in (await (await client.get("/api/products/all")).json())["items"] if i["id"] == asset["id"])
        assert p["download"] and p["sample"]
        r = await client.get(f"/api/products/download?id={asset['id']}")
        assert r.status == 200 and "attachment" in r.headers["Content-Disposition"]
        names = zipfile.ZipFile(io.BytesIO(await r.read())).namelist()
        assert any(n.endswith("leads.csv") for n in names)
        r = await client.get(f"/api/products/sample?id={asset['id']}")
        text = await r.text()
        assert r.status == 200 and "free-sample.csv" in r.headers["Content-Disposition"] and text.startswith("company")
        assert len(text.strip().splitlines()) == 6  # the header and the 5 sample rows
        assert (await client.get("/api/products/download?id=999999")).status == 404
        assert (await client.get("/api/products/sample?id=abc")).status == 404
        assert (await client.post("/api/prospects", json={"name": "Ana", "buyer": "dev agency"})).status == 403  # CSRF
        h = {"X-CSRF-Token": csrf}
        r = await (await client.post("/api/prospects", json={"name": "Ana", "where": "LinkedIn", "buyer": "dev agency",
                                                             "product": made["title"]}, headers=h)).json()
        assert r["ok"]
        d = await (await client.get("/api/prospects")).json()
        pid = d["items"][0]["id"]
        assert d["summary"]["total"] == 1 and "dev agency" in d["buyers"] and isinstance(d["channels"], list)
        r = await (await client.post("/api/prospects", json={"action": "update", "id": pid, "status": "interested"},
                                     headers=h)).json()
        assert r["ok"] and r["message"] == "Ana: interested."
        bad = await client.post("/api/prospects", json={"name": "X", "buyer": "astronaut"}, headers=h)
        assert bad.status == 400
        html = await (await client.get("/")).text()
        assert 'id="prospect-form"' in html and 'id="channel-table"' in html

    test_gui.run(gui, scenario)
