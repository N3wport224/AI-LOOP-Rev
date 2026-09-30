"""Phase 9: the $49 Executive Migration Dossier (content, PDF, checkout, delivery, upsells)."""

import json
import re
import zlib
from datetime import datetime, timezone

import pytest

from api.data import company_id
from strategies.base import TaskContext
from strategies.dossier_engine import DossierEngine, dossier_asset, offer_for, produce
from strategies.lead_magnet import dossier_links, pulse_signals, render_pulse
from tests.conftest import NOW
from tests.test_api import bearer, call, deliver
from tests import test_api
from tests.test_distribution import FakeSMTP, go_live
from tools import build_toolkit
from tools.dossier_builder import REQUIRED_SECTIONS, build, eligible, pitch_angles, to_markdown, to_pdf
from tools.pdf_writer import PDFDocument, width, wrap
from tools.storefront.webhook_listener import WebhookProcessor

STRIPE = "https://api.stripe.com/v1"
api_kit = test_api.kit  # shared fixture
HOT = company_id("Py0")  # intent 90: eligible
COOL = company_id("Py3")  # intent 75, urgency 43: not (> 75 is strict)


@pytest.fixture
def kit(api_kit, config, state, breaker, transport):
    go_live(config)
    config.stripe_secret_key = "sk_test"
    transport.add_json(f"{STRIPE}/products", {"id": "prod_d"})
    transport.add_json(f"{STRIPE}/prices", {"id": "price_d"})
    transport.add_json(f"{STRIPE}/checkout/sessions", {"id": "cs_d1", "url": "https://checkout.stripe.com/c/pay/cs_d1"})
    k = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)  # now with Stripe
    k.hyp = api_kit.hyp
    return k


def publish(kit):
    return DossierEngine().run("publish_dossier_tier", TaskContext(kit, kit.hyp, {}))


# ------------------------------------------------------------------ content
def test_eligibility_threshold():
    assert eligible({"urgency_score": 76}) and eligible({"intent_score": 90, "urgency_score": 0})
    assert not eligible({"urgency_score": 75, "intent_score": 75}) and not eligible({})
    assert eligible({"urgency_score": 60}, min_score=50)


def test_build_has_every_section_and_no_personal_data(kit):
    content, pdf, md = produce(kit, HOT)
    assert set(REQUIRED_SECTIONS) <= set(content) and all(content[s] for s in REQUIRED_SECTIONS if s != "legacy")
    assert content["company"] == "Py0" and content["company_id"] == HOT
    assert content["migration"]["path"] == "On-prem → AWS"
    assert content["hiring"]["departments"] and {"department", "open", "new_7d", "new_30d"} <= set(content["hiring"]["departments"][0])
    assert content["footprint"] and any("AWS" in v for v in content["footprint"].values())
    blob = json.dumps(content) + md
    assert "hr@x.example" not in blob  # roles, never people or contact details
    assert md.startswith("# Executive Migration Dossier: Py0")
    for heading in ("Why now", "Technology footprint", "Migration", "Hiring", "Pitch angles"):
        assert heading.lower() in md.lower()
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")


def test_pitch_angles_follow_the_signals():
    angles = pitch_angles({"migration_path": "Oracle → PostgreSQL", "commercial_signals": ["SOC 2 Compliance", "Head of Platform"],
                           "stack": ["Kubernetes"], "openings": 4})
    text = " ".join(angles)
    assert "Oracle-to-PostgreSQL" in text and "SOC 2" in text and "leadership" in text and "Kubernetes" in text and "4 open roles" in text
    assert pitch_angles({}) and len(pitch_angles({})) == 1


def test_build_department_growth_windows():
    company = {"company": "Acme", "company_id": "acme", "stack": ["Python"], "openings": 3, "intent_score": 80,
               "active_postings": [
                   {"title": "Senior Backend Engineer", "first_seen": "2026-09-28T00:00:00+00:00"},
                   {"title": "Head of Platform", "first_seen": "2026-09-10T00:00:00+00:00"},
                   {"title": "Backend Developer", "first_seen": "2026-06-01T00:00:00+00:00"}]}
    d = build(company, {}, NOW)
    depts = {x["department"]: x for x in d["hiring"]["departments"]}
    assert depts["Backend"]["open"] == 2 and depts["Backend"]["new_7d"] == 1 and depts["Backend"]["new_30d"] == 1
    assert d["hiring"]["leadership"] == ["Head of Platform"]


# ------------------------------------------------------------------ PDF
def parse_pdf(pdf: bytes):
    """Minimal structural check: xref offsets point at their objects, trailer, page count."""
    startxref = int(re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", pdf).group(1))
    assert pdf[startxref:startxref + 4] == b"xref"
    m = re.match(rb"xref\s+0 (\d+)\s+", pdf[startxref:])
    count = int(m.group(1))
    entries = pdf[startxref + m.end():].split(b"trailer")[0].split(b"\n")
    entries = [e.strip() for e in entries if e.strip()]
    assert len(entries) == count
    for n, entry in enumerate(entries[1:], 1):
        offset = int(entry.split()[0])
        assert pdf[offset:].startswith(f"{n} 0 obj".encode()), n
    streams = [zlib.decompress(s) for s in re.findall(rb"/FlateDecode[^>]*>>\s*stream\r?\n(.*?)\r?\nendstream", pdf, re.S)]
    pages = int(re.search(rb"/Type /Pages[^>]*/Count (\d+)", pdf).group(1))
    return pages, b"".join(streams)


def test_pdf_writer_structure_escaping_and_pagination():
    doc = PDFDocument("Dossier (test) \\ one", "sub")
    doc.heading("Section")
    doc.paragraph("Text with (parens), a backslash \\ and unicode – “quotes” → arrow. " * 40)
    doc.bullets([f"item {i}" for i in range(80)])
    doc.table(["A", "B"], [[f"r{i}", "x" * 30] for i in range(40)], [100, 200])
    pdf = doc.to_bytes(datetime(2026, 9, 30, tzinfo=timezone.utc))
    pages, text = parse_pdf(pdf)
    assert pages >= 3
    assert b"\\(parens\\)" in text and b"\\\\" in text
    assert f"Page 1 of {pages}".encode() in text and f"Page {pages} of {pages}".encode() in text
    assert b"/BaseFont /Helvetica" in pdf and b"/WinAnsiEncoding" in pdf


def test_wrap_respects_width():
    lines = wrap("word " * 200, 10, 200)
    assert len(lines) > 5 and all(width(ln, 10) <= 200 for ln in lines)


def test_dossier_pdf_is_well_formed(kit):
    content, _, _ = produce(kit, HOT)
    pages, text = parse_pdf(to_pdf(content))
    assert pages >= 1 and b"Py0" in text and b"pitch angles" in text
    assert to_markdown(content).count("\n## ") >= 5


# ------------------------------------------------------------------ product, checkout, delivery
def test_publish_tier_once_with_one_off_price(kit, state, transport):
    res = publish(kit)
    assert res.metrics["published"]
    asset = dossier_asset(state)
    assert asset["price_cents"] == 4900 and asset["product_ref"] == "price:price_d"
    assert json.loads(asset["kind_meta"])["price_id"] == "price_d"
    price_call = transport.calls_to(f"{STRIPE}/prices", "POST")[-1]
    assert b"unit_amount=4900" in price_call["body"] and b"recurring" not in price_call["body"]
    assert publish(kit).metrics["published"] is False and len(transport.calls_to(f"{STRIPE}/prices", "POST")) == 1


def test_publish_waits_for_stripe_and_tunnel(kit, config):
    config.stripe_secret_key = None
    assert "Stripe" in publish(kit).summary
    config.stripe_secret_key, config.public_webhook_url = "sk_test", None
    assert publish(kit).metrics.get("blocked") == "tunnel"


def test_buy_redirects_to_stripe_and_webhook_delivers_pdf(kit, state, transport):
    publish(kit)

    async def s(client):
        r = await client.get(f"/v1/dossiers/{HOT}/buy?src=api", allow_redirects=False)
        assert r.status == 303 and r.headers["Location"] == "https://checkout.stripe.com/c/pay/cs_d1"
        r = await client.get(f"/v1/dossiers/{COOL}/buy", allow_redirects=False)
        assert r.status == 404
        r = await client.get("/v1/dossiers/not a company/buy", allow_redirects=False)
        assert r.status == 404
    call(kit, s)
    body = transport.calls_to(f"{STRIPE}/checkout/sessions", "POST")[0]["body"].decode()
    assert "mode=payment" in body and "price_d" in body and f"metadata%5Bcompany_id%5D={HOT}" in body
    assert "metadata%5Bkind%5D=dossier" in body

    FakeSMTP.sent.clear()
    proc = WebhookProcessor(kit, use_sdk=False)
    asset = dossier_asset(state)
    out = deliver(proc, "checkout.session.completed", {
        "id": "cs_d1", "object": "checkout.session", "mode": "payment", "status": "complete", "payment_status": "paid",
        "amount_total": 4900, "customer_details": {"email": "cto@buyer.example"},
        "metadata": {"kind": "dossier", "company_id": HOT, "asset_id": str(asset["id"])}}, "evt_d1")
    assert out.status == 200 and len(out.fulfil) == 1
    from strategies.distribution_engine import fulfil_order

    assert fulfil_order(kit, out.fulfil[0]) == "delivered"  # what the listener does right after responding
    order = state._one("SELECT * FROM orders WHERE order_id = 'cs_d1'")
    assert order["asset_id"] == asset["id"] and order["gross_cents"] == 4900 and order["status"] == "delivered"
    msg = FakeSMTP.sent[-1]
    files = {p.get_filename(): p.get_content() for p in msg.iter_attachments()}
    pdf = next(v for k, v in files.items() if k.endswith(".pdf"))
    assert msg["To"] == "cto@buyer.example" and "Py0" in msg["Subject"]
    assert pdf.startswith(b"%PDF-1.4") and any(k.endswith(".md") for k in files)
    assert kit.files.exists(json.loads(order["meta"])["file"] + ".pdf")  # kept for order recovery


def test_stripe_failure_is_a_503_not_a_500(kit, transport):
    publish(kit)
    transport.add(f"{STRIPE}/checkout/sessions", ConnectionError("stripe down"))

    async def s(client):
        r = await client.get(f"/v1/dossiers/{HOT}/buy", allow_redirects=False)
        assert r.status == 503
    call(kit, s)


# ------------------------------------------------------------------ upsells
def test_api_company_offers_the_dossier_when_eligible(kit, state, config):
    from api.auth import ApiKeys

    key, _ = ApiKeys(state, config).issue(None, "dev@x.example")

    async def before(client):
        r = await client.get(f"/v1/companies/{HOT}", headers=bearer(key))
        assert (await r.json())["dossier_available"] is False  # tier not live yet
    call(kit, before)
    publish(kit)

    async def s(client):
        hot = await (await client.get(f"/v1/companies/{HOT}", headers=bearer(key))).json()
        assert hot["dossier_available"] is True and hot["dossier_price_cents"] == 4900
        assert hot["dossier_url"] == f"https://hooks.example.com/v1/dossiers/{HOT}/buy?src=api"
        cool = await (await client.get(f"/v1/companies/{COOL}", headers=bearer(key))).json()
        assert cool["dossier_available"] is False and cool["dossier_url"] is None
    call(kit, s)
    assert offer_for(kit, {"company_id": "x", "intent_score": 99}, "api")["dossier_available"]


def test_pulse_button_only_above_intent_80(config, kit):
    from tests.test_api import radar

    records = radar("Py", 4, ["Python"])  # intent 90, 85, 80, 75
    sigs = pulse_signals(records, NOW.replace(year=2020), n=4)
    links = dossier_links(config, sigs, "reader@x.example", live=True)
    assert set(links) == {company_id("Py0"), company_id("Py1")}  # 80 is not > 80
    assert "email=reader%40x.example" in links[company_id("Py0")] and "src=pulse" in links[company_id("Py0")]
    assert dossier_links(config, sigs, "reader@x.example", live=False) == {}
    subject, text, html = render_pulse(config, "python-remote", "2026-W40", sigs, {}, "https://u.example", links)
    assert "Get the executive dossier on Py0: $49" in html and "Get the executive dossier on Py2" not in html
    assert "Executive dossier on Py1 ($49, instant PDF)" in text
