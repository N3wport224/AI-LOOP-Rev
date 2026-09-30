"""Landing-page copy bandit: allocation math, convergence, deprecation, telemetry and the lander script."""

import json
import random
import re
import shutil
import subprocess
from datetime import timedelta

import pytest

from tests.conftest import NOW
from tools.copy_bandit import (
    CONTROL, SLOTS, epsilon_greedy, handle_beacon, page_arms, parse_variant_ref, posterior_mean, record_event,
    record_purchase_from_ref, render_copy, report, should_deprecate, stats, thompson, tune, variant_ref, winner,
)


def s(views, conv):
    return {"view": views, "click": 0, "signup": conv, "purchase": 0}


# ------------------------------------------------------------------ allocation math
def test_epsilon_greedy_gives_the_winner_80_percent():
    arms = {"migration_urgency": s(1000, 50), "hiring_stack": s(1000, 20), "verified_leads": s(1000, 10)}
    alloc = epsilon_greedy("headline", arms, list(arms), 0.2)
    assert alloc == {"migration_urgency": 0.8, "hiring_stack": 0.1, "verified_leads": 0.1}
    assert epsilon_greedy("headline", arms, ["hiring_stack", "verified_leads"], 0.2) == {"hiring_stack": 0.8, "verified_leads": 0.2}
    assert epsilon_greedy("headline", arms, ["verified_leads"], 0.2) == {"verified_leads": 1.0}


def test_winner_uses_the_posterior_and_ties_go_to_the_control():
    empty = {v: s(0, 0) for v in SLOTS["cta"]}
    assert winner("cta", empty, list(empty)) == CONTROL["cta"] == "instant_feed"
    # an untested arm starts at the pooled rate, so it can't "win" on no evidence
    arms = {"free_sample": s(400, 4), "instant_feed": s(500, 40), "developer_api": s(0, 0)}
    assert winner("cta", arms, list(arms)) == "instant_feed"
    # one lucky conversion doesn't beat 500 views at 8% either
    arms["developer_api"] = s(1, 1)
    assert posterior_mean(arms["developer_api"], 0.02) == pytest.approx((0.02 * 50 + 1) / 51)
    assert winner("cta", arms, list(arms)) == "instant_feed"
    arms["developer_api"] = s(300, 45)  # 15% over real traffic does
    assert winner("cta", arms, list(arms)) == "developer_api"


def test_thompson_puts_traffic_where_the_evidence_is():
    arms = {"migration_urgency": s(2000, 120), "hiring_stack": s(2000, 60), "verified_leads": s(2000, 58)}
    alloc = thompson("headline", arms, list(arms), seed="d1")
    assert alloc["migration_urgency"] > 0.97 and sum(alloc.values()) == pytest.approx(1.0)
    assert thompson("headline", arms, list(arms), seed="d1") == alloc  # seeded: stable within a day
    close = {"migration_urgency": s(50, 3), "hiring_stack": s(50, 3), "verified_leads": s(50, 2)}
    spread = thompson("headline", close, list(close), seed="d1")
    assert max(spread.values()) < 0.6  # little evidence: keeps exploring


def test_deprecation_is_two_standard_errors_below_the_control():
    control = s(3000, 150)  # 5%
    # z = (p - .05) / sqrt(.05·.95/3000 + p(1-p)/1000): 3.5% → z = -2.13, 3.7% → z = -1.81
    assert should_deprecate(s(1000, 35), control, 200, 2.0)[0] is True
    assert should_deprecate(s(1000, 37), control, 200, 2.0)[0] is False
    assert should_deprecate(s(150, 0), control, 200, 2.0) == (False, 0.0)     # not enough views yet
    assert should_deprecate(s(1000, 0), s(100, 5), 200, 2.0) == (False, 0.0)  # nor on the control
    retire, z = should_deprecate(s(1000, 35), control, 200, 2.0)
    assert z == pytest.approx((0.035 - 0.05) / (0.05 * 0.95 / 3000 + 0.035 * 0.965 / 1000) ** 0.5, abs=1e-3)


def test_variant_refs_round_trip():
    for h in SLOTS["headline"]:
        for c in SLOTS["cta"]:
            tag = variant_ref(h, c)
            assert parse_variant_ref(f"am--devto--w40--{tag}") == {"headline": h, "cta": c}
    assert parse_variant_ref("am--devto--w40") is None and parse_variant_ref("am--x--y--hz_cs") is None


# ------------------------------------------------------------------ convergence (simulation)
TRUE = {"headline": {"migration_urgency": 0.05, "hiring_stack": 0.03, "verified_leads": 0.012},
        "cta": {"free_sample": 0.04, "instant_feed": 0.025, "developer_api": 0.02}}


def simulate(state, config, clock, days=40, visitors=600, seed=7):
    rng = random.Random(seed)
    earned = 0
    for _ in range(days):
        policy = tune(state, config, clock())
        counts: dict = {}
        for _ in range(visitors):
            pick = {}
            for slot, alloc in policy["allocation"].items():
                r, acc = rng.random(), 0.0
                pick[slot] = list(alloc)[-1]
                for v, p in alloc.items():
                    acc += p
                    if r < acc:
                        pick[slot] = v
                        break
            for slot, v in pick.items():
                counts[(slot, v, "view")] = counts.get((slot, v, "view"), 0) + 1
            # a visitor converts with the product of the arms' lifts over a base rate (independent slots)
            p = TRUE["headline"][pick["headline"]] * TRUE["cta"][pick["cta"]] / 0.03
            if rng.random() < p:
                earned += 1
                for slot, v in pick.items():
                    counts[(slot, v, "signup")] = counts.get((slot, v, "signup"), 0) + 1
        for (slot, v, e), n in counts.items():  # one write per arm and event per day
            record_event(state, slot, v, e, clock(), n)
        clock.advance(days=1)
    return tune(state, config, clock()), earned


def test_epsilon_greedy_converges_and_retires_the_loser(state, config, clock):
    config.copy_bandit_min_views = 300
    policy, earned = simulate(state, config, clock)
    assert policy["winners"] == {"headline": "migration_urgency", "cta": "free_sample"}
    assert policy["allocation"]["headline"] == {"migration_urgency": 0.8, "hiring_stack": 0.2}  # control keeps exploring
    assert policy["status"]["headline"]["verified_leads"]["state"] == "deprecated"
    assert "z = " in policy["status"]["headline"]["verified_leads"]["reason"]
    assert policy["status"]["headline"]["migration_urgency"]["state"] == "active"  # the winner is never retired
    # beats serving the controls to everyone (expected 600 × 40 × 0.03 × 0.025/0.03 = 600 conversions)
    assert earned > 600 * 1.3
    rows = {(r["slot"], r["variant"]): r for r in report(state, clock())}
    assert rows[("headline", "migration_urgency")]["conversion_rate"] > rows[("headline", "hiring_stack")]["conversion_rate"]


def test_thompson_converges_too(state, config, clock):
    config.copy_bandit_algorithm = "thompson"
    policy, _ = simulate(state, config, clock, days=30)
    assert policy["winners"]["headline"] == "migration_urgency" and policy["allocation"]["headline"]["migration_urgency"] > 0.8


def test_policy_version_only_moves_on_material_change(state, config, clock):
    v1 = tune(state, config, clock())["version"]
    assert tune(state, config, clock())["version"] == v1  # nothing changed
    record_event(state, "cta", "free_sample", "view", clock(), 100)
    record_event(state, "cta", "free_sample", "signup", clock(), 30)
    record_event(state, "cta", "instant_feed", "view", clock(), 100)
    assert tune(state, config, clock())["version"] == v1 + 1  # the winner flipped


def test_stats_window_is_30_days(state, clock):
    record_event(state, "cta", "free_sample", "view", clock() - timedelta(days=40), 500)
    record_event(state, "cta", "free_sample", "view", clock(), 5)
    assert stats(state, clock())["cta"]["free_sample"]["view"] == 5
    with pytest.raises(ValueError):
        record_event(state, "cta", "made_up", "view", clock())


# ------------------------------------------------------------------ telemetry
def test_beacons_are_validated_and_counted_once_a_day(state, clock):
    body = json.dumps({"e": "view", "h": "migration_urgency", "c": "free_sample", "v": "visitor123", "p": "python-remote"}).encode()
    assert handle_beacon(state, body, clock()) == (204, "recorded")
    assert handle_beacon(state, body, clock()) == (204, "duplicate")
    clock.advance(days=1)
    assert handle_beacon(state, body, clock()) == (204, "recorded")
    assert stats(state, clock())["headline"]["migration_urgency"]["view"] == 2
    for bad in [b"not json", b"[]", json.dumps({"e": "signup", "h": "migration_urgency", "c": "free_sample", "v": "visitor123", "p": "x"}).encode(),
                json.dumps({"e": "view", "h": "evil", "c": "free_sample", "v": "visitor123", "p": "x"}).encode(),
                json.dumps({"e": "view", "h": "hiring_stack", "c": "free_sample", "v": "<script>", "p": "x"}).encode(),
                b"x" * 2000]:
        assert handle_beacon(state, bad, clock())[0] in (400, 413)


def test_purchases_and_signups_are_credited_to_the_variant(kit_bandit, state, clock):
    from strategies.lead_magnet import capture
    from tools.storefront.webhook_listener import WebhookProcessor, sign_payload

    assert capture(kit_bandit, "ada@example.com", "python-remote", copy_tag="hm_cs").status == "accepted"
    capture(kit_bandit, "ada@example.com", "python-remote", copy_tag="hm_cs")  # repeat signup: not double-counted
    proc = WebhookProcessor(kit_bandit, use_sdk=False)
    sess = {"id": "cs_b1", "object": "checkout.session", "payment_link": "plink_x", "amount_total": 900, "payment_status": "paid",
            "status": "complete", "customer_details": {"email": "b@example.com"}, "client_reference_id": "am--devto--w40--hv_cf"}
    payload = json.dumps({"id": "evt_b1", "object": "event", "type": "checkout.session.completed", "data": {"object": sess}}).encode()
    proc.handle(payload, sign_payload(payload, kit_bandit.config.stripe_webhook_secret))
    proc.handle(payload, sign_payload(payload, kit_bandit.config.stripe_webhook_secret))  # duplicate delivery
    st = stats(state, clock())
    assert st["headline"]["migration_urgency"]["signup"] == 1 and st["cta"]["free_sample"]["signup"] == 1
    assert st["headline"]["verified_leads"]["purchase"] == 1 and st["cta"]["instant_feed"]["purchase"] == 1
    assert not record_purchase_from_ref(state, "am--devto--w40", clock())


@pytest.fixture
def kit_bandit(config, state, breaker, transport):
    from tools import build_toolkit

    config.stripe_webhook_secret = "whsec_bandit"
    config.public_webhook_url = "https://hooks.example.com/webhook"
    return build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None)


def test_beacon_endpoint_over_http(kit_bandit, state):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from agent.power import NullBackend, PowerManager
    from tools.storefront.webhook_listener import WebhookProcessor, build_app

    app = build_app(WebhookProcessor(kit_bandit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            body = json.dumps({"e": "click", "h": "hiring_stack", "c": "developer_api", "v": "abcdef12", "p": "python-remote"})
            r = await client.post("/t/e", data=body, headers={"Content-Type": "text/plain"})
            assert r.status == 204
            assert (await client.post("/t/e", data="{}")).status == 400
    asyncio.run(go())
    assert stats(state, NOW)["cta"]["developer_api"]["click"] == 1


# ------------------------------------------------------------------ page rendering
FACTS = {"label": "Python", "companies": 42, "hot": 7, "verified": 12, "price": "$9.00", "api_price": "$29.00",
         "checkout_url": "https://buy.stripe.com/py", "lead_form": True, "api_url": "https://buy.stripe.com/api"}


def test_copy_only_claims_what_the_data_supports():
    assert render_copy("headline", "verified_leads", FACTS) == "12 verified Python developer leads with live careers pages"
    assert render_copy("headline", "verified_leads", {**FACTS, "verified": 0}) is None
    assert render_copy("headline", "migration_urgency", {**FACTS, "hot": 0}) is None
    assert render_copy("cta", "developer_api", {**FACTS, "api_url": ""}) is None
    assert render_copy("cta", "free_sample", {**FACTS, "lead_form": False}) is None
    arms = page_arms({**FACTS, "api_url": ""}, {"allocation": {"cta": {"free_sample": 0.8, "instant_feed": 0.1, "developer_api": 0.1}}})
    assert set(arms["cta"]) == {"free_sample", "instant_feed"}
    assert arms["cta"]["free_sample"]["w"] == pytest.approx(0.8 / 0.9, abs=1e-6)  # renormalised without the missing arm
    dep = page_arms(FACTS, {"status": {"headline": {"verified_leads": {"state": "deprecated"}}}})
    assert "verified_leads" not in dep["headline"]


def lander(tmp_facts=FACTS, policy=None):
    from tools.page_builder import ProductPage, render_product_page

    page = ProductPage("python-remote", "Python Remote Tech Stack Intel", "Summary", 900, "usd", "https://buy.stripe.com/py",
                       ["company"], [{"company": "Acme"}], lead_capture_url="https://hooks.example.com/lead-magnet/capture")
    page.copy_arms = page_arms(tmp_facts, policy or {"allocation": {"headline": {"migration_urgency": 0.8, "hiring_stack": 0.1,
                                                                                  "verified_leads": 0.1}}})
    page.copy_version, page.telemetry_url = 3, "https://hooks.example.com/t/e"
    page.copy_targets = {"free_sample": "#lead", "instant_feed": "https://buy.stripe.com/py", "developer_api": "https://buy.stripe.com/api"}
    return page, render_product_page(page, "https://me.github.io/datasets")


def test_lander_embeds_the_policy_and_renders_the_winner_for_crawlers():
    page, html = lander()
    blob = json.loads(re.search(r'<script type="application/json" id="am-copy">(.*?)</script>', html, re.S).group(1))
    assert blob["version"] == 3 and blob["endpoint"] == "https://hooks.example.com/t/e" and blob["page"] == "python-remote"
    assert sum(a["w"] for a in blob["arms"]["headline"].values()) == pytest.approx(1.0)
    assert '<p class="hero" data-copy="headline">7 Python companies are migrating or hiring urgently right now</p>' in html
    assert "<h1>Python Remote Tech Stack Intel</h1>" in html  # the SEO title doesn't vary
    assert 'id="lead"' in html and 'data-copy="cta"' in html
    from tools.page_builder import ProductPage, render_product_page

    plain = render_product_page(ProductPage("x", "X", "s", 900, "usd", "https://buy.stripe.com/x", [], []))
    assert "am-copy" not in plain and "AMC" not in plain  # no tunnel, no bandit: static copy


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_lander_script_assigns_swaps_beacons_and_tags_checkouts():
    """Run the real inline scripts in Node against a tiny DOM stub."""
    from tools.page_builder import ATTRIBUTION_JS, COPY_APPLY_JS, COPY_TAG_JS

    page, html = lander()
    blob = re.search(r'<script type="application/json" id="am-copy">(.*?)</script>', html, re.S).group(1)
    harness = """
const blob = %s;
function el(tag){return {tag, attrs:{}, textContent:'', children:[], href:'',
  setAttribute(k,v){this.attrs[k]=v; if(k==='href') this.href=v;}, removeAttribute(k){delete this.attrs[k];},
  addEventListener(ev,fn){this['on'+ev]=fn;}, appendChild(c){this.children.push(c);}};}
const headline = el('p'), cta = el('a'); cta.href = 'https://buy.stripe.com/py'; cta.attrs['data-checkout']='';
const alt = el('a'); alt.href = 'https://buy.stripe.com/py'; alt.attrs['data-checkout']='';
const form = el('form'); const store = {}; const beacons = [];
global.localStorage = {getItem:k=>store[k]===undefined?null:store[k], setItem:(k,v)=>{store[k]=String(v);}};
Object.defineProperty(globalThis, 'navigator', {value: {sendBeacon:(url,b)=>{beacons.push([url,b.body]);return true;}}, configurable: true});
global.Blob = function(parts, opts){this.body = parts.join(''); this.type = opts.type;};
global.location = {search:'?utm_source=devto&utm_campaign=w40'};
Math.random = () => 0.05;  // lowest bucket: first arm in each slot
global.document = {
  getElementById: id => id==='am-copy' ? {textContent: JSON.stringify(blob)} : null,
  querySelector: sel => sel==='[data-copy=headline]' ? headline : sel==='[data-copy=cta]' ? cta : null,
  querySelectorAll: sel => sel==='form.lead' ? [form] : sel==='a[data-checkout]' ? [cta, alt].filter(a=>'data-checkout' in a.attrs) : [],
  createElement: tag => el(tag),
};
%s
%s
%s
cta.onclick && cta.onclick();
console.log(JSON.stringify({headline: headline.textContent, cta: cta.textContent, ctaHref: cta.href, altHref: alt.href,
  hidden: form.children.map(c=>[c.name,c.value]), beacons: beacons.map(b=>JSON.parse(b[1]).e), stored: store}));
""" % (blob, COPY_APPLY_JS, ATTRIBUTION_JS, COPY_TAG_JS)
    out = json.loads(subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True, timeout=30).stdout)
    assert out["headline"] == "7 Python companies are migrating or hiring urgently right now"
    assert out["cta"] == "Download the free sample" and out["ctaHref"] == "#lead"
    assert out["hidden"] == [["copy", "hm_cs"]]
    assert out["beacons"] == ["view", "click"]
    assert "client_reference_id=am--devto--w40--hm_cs" in out["altHref"]  # the sale is credited to devto AND the copy
    assert json.loads(out["stored"]["am_copy_3"]) == {"headline": "migration_urgency", "cta": "free_sample"}
