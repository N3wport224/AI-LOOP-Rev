"""``automonetize test-full-loop``: the whole revenue loop, end to end, in under a minute.

What's real: the aiohttp listener (the same app ``supervise`` runs) on a random localhost port,
HTTP over real sockets, Stripe-signed webhooks checked with the real verifier, the real
pipeline (ingest → intent extraction → packaging → publishing → site build), fulfilment and
email composition, the Developer API queried with ``curl``, and the PDF dossier.

What's simulated, so the run is free and safe:

* **Stripe**: a local fake answers the API calls (products, prices, Payment Links, Checkout,
  billing portal). No request reaches Stripe and nothing is charged.
* **Job sources**: 5 realistic postings with cloud-migration intent, served in the Remote OK
  API format.
* **Email**: captured in memory and inspected, never sent.
* **Data**: a throwaway sandbox directory. The run doesn't touch your real database, revenue
  ledger or subscribers. Firing fake purchases at the production listener would record fake
  revenue and could email real people, so this runs its own instance of the same code instead.

Exit code 0 when every step passes.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

NICHE = "cloud-migrations"
BUYER = "buyer@loop.example"
SUBSCRIBER = "subscriber@loop.example"
DEVELOPER = "developer@loop.example"
EXECUTIVE = "vp-sales@loop.example"


# ----------------------------------------------------------------------------- simulated world
def seed_postings(now: datetime) -> list[dict[str, Any]]:
    """Five realistic postings (Remote OK API format), each with a cloud/stack migration signal."""
    def post(i: int, company: str, domain: str, title: str, tags: list[str], desc: str, days: int = 1) -> dict[str, Any]:
        return {"id": f"loop-{i}", "epoch": int((now - timedelta(days=days)).timestamp()), "company": company, "position": title,
                "tags": tags, "location": "Remote - US", "url": f"https://remoteok.com/remote-jobs/loop-{i}",
                "apply_url": f"https://{domain}/careers/{i}", "description": desc, "salary_min": 150000, "salary_max": 190000}
    return [
        {"legal": "Remote OK API terms (simulated)"},
        post(1, "Northwind Logistics", "northwind-logistics.example", "Senior Platform Engineer (Kubernetes, AWS)",
             ["kubernetes", "aws", "terraform", "python"],
             "<p>We are migrating from our on-prem data center to AWS this year and need help urgently: EKS, Terraform, "
             "Python. We're also working toward SOC 2 Type II. Start ASAP.</p>"),
        post(2, "Brightline Health", "brightline-health.example", "Staff Data Engineer, Platform Migration",
             ["python", "postgres", "airflow", "aws"],
             "<p>Lead our legacy Oracle to Postgres migration on AWS. HIPAA experience required. Airflow, dbt, Kubernetes.</p>"),
        post(3, "Quanta Retail", "quanta-retail.example", "DevOps Engineer (first DevOps hire)",
             ["devops", "aws", "terraform", "docker"],
             "<p>You'll be our first DevOps hire. We're moving from Heroku to AWS ECS with Terraform and Datadog.</p>", days=2),
        post(4, "Helio Payments", "helio-payments.example", "Cloud Security Engineer",
             ["security", "gcp", "kubernetes", "go"],
             "<p>Help us achieve PCI DSS and SOC 2 while we move from VMware to GCP. Kubernetes, Go, zero trust.</p>", days=3),
        post(5, "Orbit Analytics", "orbit-analytics.example", "Head of Infrastructure",
             ["kubernetes", "aws", "terraform", "python"],
             "<p>Founding infrastructure leader: own our Kubernetes migration on AWS, Terraform everywhere, Python services.</p>"),
    ]


class CaptureSMTP:
    """Stands in for smtplib.SMTP: every message is kept for inspection, nothing leaves the Mac."""

    sent: list[Any] = []
    lock = threading.Lock()

    def __init__(self, host: str, port: int, timeout: float = 30):
        self.host, self.port = host, port

    def __enter__(self) -> "CaptureSMTP":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def ehlo(self) -> None: ...

    def starttls(self) -> None: ...

    def login(self, user: str, password: str) -> None: ...

    def send_message(self, msg: Any) -> None:
        with CaptureSMTP.lock:
            CaptureSMTP.sent.append(msg)


class FakeStripe:
    """Answers the Stripe API calls the agent makes, plus the job-board request."""

    def __init__(self, postings: list[dict[str, Any]]):
        self.postings = postings
        self.n = 0
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def _id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_loop{self.n}"

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        from tools.http_client import Response

        def ok(data: Any) -> Response:
            return Response(200, url, json.dumps(data).encode(), {"content-type": "application/json"})

        self.calls.append((method, url.split("?")[0]))
        path = urllib.parse.urlsplit(url).path
        if url.startswith("https://remoteok.com/api"):
            return ok(self.postings)
        if not url.startswith("https://api.stripe.com/"):
            return Response(404, url, b"not found", {})
        form = dict(urllib.parse.parse_qsl((body or b"").decode()))
        if method == "POST" and path == "/v1/products":
            return ok({"id": self._id("prod")})
        if method == "POST" and path == "/v1/prices":
            return ok({"id": self._id("price")})
        if method == "POST" and path == "/v1/payment_links":
            pid = self._id("plink")
            return ok({"id": pid, "url": f"https://buy.stripe.com/test_{pid}"})
        if method == "POST" and path == "/v1/checkout/sessions":
            sid = self._id("cs_test")
            meta = {k[len("metadata["):-1]: v for k, v in form.items() if k.startswith("metadata[")}
            self.sessions[sid] = {"metadata": meta, "client_reference_id": form.get("client_reference_id"),
                                  "customer_email": form.get("customer_email")}
            return ok({"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}"})
        if method == "POST" and path == "/v1/billing_portal/sessions":
            return ok({"id": self._id("bps"), "url": f"https://billing.stripe.com/p/session/test_{form.get('customer', '')}"})
        if method == "GET" and path.startswith("/v1/subscriptions/"):
            return ok({"id": path.rsplit("/", 1)[1], "status": "active", "metadata": {}})
        if method == "GET" and path in ("/v1/checkout/sessions", "/v1/invoices"):
            return ok({"has_more": False, "data": []})
        if method == "GET" and path == "/v1/balance":
            return ok({"livemode": False})
        return Response(404, url, b"{}", {})


# ----------------------------------------------------------------------------- runner
@dataclass
class Step:
    name: str
    ok: bool = False
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Loop:
    tools: Any = None
    stripe: FakeStripe | None = None
    server: Any = None
    stop: threading.Event = field(default_factory=threading.Event)
    base: str = ""
    secret: str = ""
    niche_hyp: dict[str, Any] | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    use_curl: bool = True

    # -- HTTP helpers -------------------------------------------------------------------
    def http(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with opener.open(req, timeout=15) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def webhook(self, etype: str, obj: dict[str, Any]) -> dict[str, Any]:
        from tools.storefront.webhook_listener import sign_payload

        payload = json.dumps({"id": f"evt_loop_{secrets.token_hex(6)}", "object": "event", "type": etype,
                              "created": int(time.time()), "data": {"object": obj}}).encode()
        status, _, body = self.http("POST", "/webhook", payload, {"Content-Type": "application/json",
                                                                  "Stripe-Signature": sign_payload(payload, self.secret)})
        data = json.loads(body or b"{}")
        if status != 200 or data.get("status") not in ("processed", None) and not data.get("duplicate"):
            raise AssertionError(f"webhook {etype} → {status} {data}")
        return data

    def api(self, path: str, key: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        if self.use_curl and shutil.which("curl"):
            dump = Path(self.tools.config.data_dir) / "curl.headers"
            proc = subprocess.run(["curl", "-sS", "--noproxy", "*", "-D", str(dump), "-H", f"Authorization: Bearer {key}",
                                   self.base + path], capture_output=True, text=True, timeout=15, check=True)
            lines = dump.read_text().splitlines()
            status = int(lines[0].split()[1])
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if v}
            return status, json.loads(proc.stdout), headers
        status, headers, body = self.http("GET", path, headers={"Authorization": f"Bearer {key}"})
        return status, json.loads(body), {k.lower(): v for k, v in headers.items()}

    def wait(self, predicate: Callable[[], Any], what: str, timeout: float = 10.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def mails_to(self, address: str) -> list[Any]:
        with CaptureSMTP.lock:
            return [m for m in CaptureSMTP.sent if m["To"] == address]

    def session(self, **kw: Any) -> dict[str, Any]:
        base = {"id": f"cs_loop_{secrets.token_hex(5)}", "object": "checkout.session", "status": "complete", "payment_status": "paid",
                "created": int(time.time()), "mode": "payment"}
        return {**base, **kw}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _attachments(msg: Any) -> dict[str, bytes]:
    return {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}


# ----------------------------------------------------------------------------- the steps
def setup(loop: Loop, sandbox: Path) -> str:
    from agent.config import Config
    from agent.power import NullBackend, PowerManager
    from agent.state import StateStore
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker
    from tools.storefront.webhook_listener import WebhookServer

    CaptureSMTP.sent = []
    loop.secret = "whsec_" + secrets.token_hex(16)
    cfg = Config(
        data_dir=sandbox / "data", network_check_hosts=[], respect_robots_txt=False, http_backoff_seconds=0.0,
        http_rate_per_minute=100000, lead_sources=["remoteok"], min_leads_for_asset=3, intel_max_url_checks=0,
        niches=[{"name": NICHE, "keywords": ["kubernetes", "aws", "terraform", "devops", "platform", "data engineer", "security",
                                             "infrastructure", "migration"]}],
        price_tiers=[[0, 1400]], price_matrix=[1400], subscription_price_cents=1000, api_price_cents=2900, dossier_price_cents=4900,
        stripe_secret_key="sk_test_loop_" + secrets.token_hex(8), stripe_webhook_secret=loop.secret, webhook_port=0,
        public_webhook_url="https://loop.sandbox.invalid/webhook", pages_base_url="https://loop.sandbox.invalid/site",
        dry_run=False, email_backend="smtp", smtp_host="smtp.sandbox.invalid", sender_email="launch@loop.example",
        sender_name="AutoMonetize Loop Test", sender_postal_address="1 Test Street, Springfield, IL 62701, USA",
        unsubscribe_email="unsubscribe@loop.example", seo_min_companies=2, seo_min_migrations=2, indexnow_enabled=False,
        source_discovery_enabled=False, max_active_niches=1, copy_bandit_enabled=True, auto_update=False,
        backup_dir=str(sandbox / "backups"),
    )
    cfg.ensure_dirs()
    loop.stripe = FakeStripe(seed_postings(datetime.now(timezone.utc)))
    state = StateStore(cfg.db_path)
    breaker = CircuitBreaker(max_actions_per_cycle=1000, max_api_calls_per_cycle=10000, max_consecutive_errors=1000)
    loop.tools = build_toolkit(cfg, state, breaker, transport=loop.stripe, sleep=lambda s: None, smtp_factory=CaptureSMTP)
    hid = state.create_hypothesis(f"lead_directory:{NICHE}:g1", "lead_directory", "loop test: cloud migrations",
                                  {"niche": NICHE, "keywords": cfg.niches[0]["keywords"], "generation": 1})
    loop.niche_hyp = state.get_hypothesis(hid)
    loop.server = WebhookServer(loop.tools, host="127.0.0.1", port=0, power=PowerManager(NullBackend()))
    threading.Thread(target=loop.server.run, args=(loop.stop,), name="loop-listener", daemon=True).start()
    if not loop.server.started.wait(10):
        raise AssertionError("listener didn't start")
    loop.base = f"http://127.0.0.1:{loop.server.bound_port}"
    status, _, body = loop.http("GET", "/healthz")
    assert status == 200, f"/healthz → {status}"
    return f"sandbox {sandbox} · listener {loop.base} · Stripe, job boards and email simulated"


def ingest(loop: Loop) -> str:
    from strategies.b2b_lead_aggregator import LeadAggregator
    from strategies.base import TaskContext

    res = LeadAggregator().run("aggregate_leads", TaskContext(loop.tools, loop.niche_hyp, {}))
    assert res.metrics["valid"] == 5, res.summary
    assert res.metrics["total"] >= 5, res.summary
    return f"5 postings fetched through the aggregator, {res.metrics['total']} leads in {NICHE}"


def intel(loop: Loop) -> str:
    from strategies.base import TaskContext
    from strategies.tech_stack_intel import TechStackIntel
    from tools.dossier_builder import eligible

    TechStackIntel().run("build_intel", TaskContext(loop.tools, loop.niche_hyp, {}))
    records = loop.tools.files.read_json(f"exports/intel/{NICHE}/tech_radar.json")
    tags = [r["intent_tag"] for r in records if r.get("intent_tag")]
    assert len(records) == 5 and len(tags) == 5, f"{len(records)} records, tags {tags}"
    assert any("Cloud Migration" in t for t in tags), tags
    paths = [r["migration_path"] for r in records if r.get("migration_path")]
    top = max(records, key=lambda r: (max(r["urgency_score"], r["intent_score"]), r["intent_score"]))
    assert eligible(top, loop.tools.config.dossier_min_score), f"no dossier-eligible company (top {top['company']})"
    loop.facts["dossier_company"] = top
    return f"{len(tags)} intent tags (e.g. '{top['intent_tag']}'), paths: {', '.join(sorted(set(paths)))}"


def package(loop: Loop) -> str:
    from strategies.base import TaskContext
    from strategies.digital_asset_packager import DigitalAssetPackager

    res = DigitalAssetPackager().run("package_asset", TaskContext(loop.tools, loop.niche_hyp, {}))
    names = zipfile.ZipFile(io.BytesIO(loop.tools.files.read_bytes(res.metrics["zip"]))).namelist()
    assert any(n.endswith("EXECUTIVE_TECH_RADAR.md") for n in names), names
    radar = loop.tools.files.read_text(f"exports/intel/{NICHE}/EXECUTIVE_TECH_RADAR.md")
    assert "## Commercial buying intent" in radar
    return f"{res.metrics['zip']} ({len(names)} files, Executive Tech Radar included)"


def publish(loop: Loop) -> str:
    from api.auth import api_asset
    from strategies.base import TaskContext
    from strategies.distribution_engine import DistributionEngine
    from strategies.dossier_engine import DossierEngine, dossier_asset
    from strategies.subscription_engine import SubscriptionEngine, subscription_asset

    ctx = TaskContext(loop.tools, loop.niche_hyp, {})
    DistributionEngine().run("publish_listing", ctx)
    SubscriptionEngine().run("publish_subscription", ctx)
    SubscriptionEngine().run("publish_api_tier", ctx)
    DossierEngine().run("publish_dossier_tier", ctx)
    state = loop.tools.state
    dataset = state.latest_asset(loop.niche_hyp["id"], "lead_directory")
    sub, api, dossier = subscription_asset(state, NICHE), api_asset(state), dossier_asset(state)
    assert dataset and dataset["status"] == "published" and dataset["price_cents"] == 1400, dataset
    assert sub and api and dossier, (sub, api, dossier)
    loop.facts.update(dataset=dataset, sub=sub, api=api, dossier=dossier)
    return "$14.00 dataset, $10.00/month updates, $29.00/month API, $49.00 dossier: live on (simulated) Stripe"


def site(loop: Loop) -> str:
    from strategies.base import TaskContext
    from strategies.inbound_syndicator import InboundSyndicator

    res = InboundSyndicator().run("build_site", TaskContext(loop.tools, loop.niche_hyp, {}))
    files = loop.tools.files
    intel_pages = sorted(p.name for p in files.resolve("site/intel").glob("*.html") if p.name != "index.html")
    assert intel_pages, "no matrix pages"
    for name in intel_pages + ["../" + NICHE + "/index.html"]:
        html = (files.resolve("site/intel") / name).read_text()
        block = re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', html, re.S)
        assert block, f"{name}: no JSON-LD"
        data = json.loads(block.group(1))
        assert data["@type"] in ("Dataset", "Product") and data.get("name"), f"{name}: bad JSON-LD {data.get('@type')}"
    root = ET.fromstring(files.read_text("site/sitemap.xml").split("\n", 1)[1])
    locs = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert all(any(loc.endswith(p) for loc in locs) for p in intel_pages), "sitemap misses matrix pages"
    return f"{res.metrics['pages']} lander + {len(intel_pages)} matrix pages, JSON-LD valid, sitemap lists {len(locs)} URLs"


def factory(loop: Loop) -> str:
    """Phase 244: the product factory makes a product from the pooled postings and it goes on sale."""
    from datetime import timedelta

    from strategies import product_factory as pf
    from strategies.b2b_lead_aggregator import POOL_NICHE
    from strategies.base import TaskContext
    from strategies.inbound_syndicator import InboundSyndicator

    tools = loop.tools
    state = tools.state
    when = (state.clock() - timedelta(days=2)).isoformat(timespec="seconds")
    for i in range(24):
        state.upsert_lead(f"factory-loop-{i}", POOL_NICHE, {
            "company": f"Rustacean Co {i % 9}", "title": "Senior Rust Engineer", "location": "Berlin, Germany", "remote": False,
            "stack": ["rust", "aws"], "seniority": "senior", "url": f"https://jobs.example/rust/{i}", "posted_at": when,
            "source": "remoteok"})
    tools.config.product_factory = True
    out = pf.tick(tools, force=True)
    made = out.get("made")
    assert made and out.get("status") == "live", out.get("why") or out.get("status")
    InboundSyndicator().run("build_site", TaskContext(tools, loop.niche_hyp, {}))
    page = tools.files.read_text(f"site/{made['slug']}/index.html")
    asset = state.get_asset(made["asset_id"])
    assert asset["checkout_url"] and asset["checkout_url"] in page, "the product page has no checkout"
    assert tools.files.exists("site/catalog/index.html") and made["slug"] in tools.files.read_text("site/search.json")
    return f"{made['title']}: {made['rows']} rows, ${made['price_cents'] / 100:.2f}, on the site with a checkout"


def buy_dataset(loop: Loop) -> str:
    ds = loop.facts["dataset"]
    loop.webhook("checkout.session.completed", loop.session(payment_link=ds["product_ref"], amount_total=1400,
                                                            customer_details={"email": BUYER}, client_reference_id="am--devto--loop"))
    loop.wait(lambda: loop.tools.state.order_counts().get("delivered"), "dataset delivery")
    msg = loop.wait(lambda: loop.mails_to(BUYER), "delivery email")[-1]
    files = _attachments(msg)
    zipped = next(v for k, v in files.items() if k.endswith(".zip"))
    assert any(n.endswith("tech_radar.csv") for n in zipfile.ZipFile(io.BytesIO(zipped)).namelist())
    return f"order delivered, zip attached ({len(zipped) // 1024 + 1} KB) to {BUYER}"


def subscribe(loop: Loop) -> str:
    from strategies.subscription_engine import delivery_moment, weekly_package

    sub_id = "sub_loop_data"
    loop.webhook("checkout.session.completed", loop.session(mode="subscription", payment_link=loop.facts["sub"]["product_ref"],
                                                            subscription=sub_id, customer="cus_loop_data", amount_total=1000,
                                                            customer_details={"email": SUBSCRIBER}))
    loop.webhook("invoice.paid", {"id": "in_loop_data", "object": "invoice", "subscription": sub_id, "amount_paid": 1000,
                                  "status": "paid", "customer_email": SUBSCRIBER, "status_transitions": {"paid_at": int(time.time())}})
    state = loop.tools.state
    assert state.get_subscriber(sub_id)["subscription_status"] == "active"
    loop.wait(lambda: loop.mails_to(SUBSCRIBER), "welcome email")
    cfg = loop.tools.config
    now = state.clock()
    moment, period = delivery_moment(now, cfg.subscription_delivery_weekday, cfg.subscription_delivery_hour, cfg.subscription_timezone)
    if moment <= now:
        moment, period = delivery_moment(now + timedelta(days=7), cfg.subscription_delivery_weekday,
                                         cfg.subscription_delivery_hour, cfg.subscription_timezone)
    rel, count, _ = weekly_package(loop.tools, NICHE, moment - timedelta(days=7), period)
    assert loop.tools.files.exists(rel)
    return f"subscriber active, welcome sent, Monday digest {period} staged for {moment:%a %d %b %H:%M} ({count} changes)"


def api_tier(loop: Loop) -> str:
    sub_id = "sub_loop_api"
    loop.webhook("checkout.session.completed", loop.session(mode="subscription", payment_link=loop.facts["api"]["product_ref"],
                                                            subscription=sub_id, customer="cus_loop_api", amount_total=2900,
                                                            customer_details={"email": DEVELOPER}))
    loop.webhook("invoice.paid", {"id": "in_loop_api", "object": "invoice", "subscription": sub_id, "amount_paid": 2900,
                                  "status": "paid", "customer_email": DEVELOPER, "status_transitions": {"paid_at": int(time.time())}})
    msg = loop.wait(lambda: loop.mails_to(DEVELOPER), "API welcome email")[-1]
    key = re.search(r"am_live_[0-9a-f]{48}", msg.get_body(("plain",)).get_content()).group(0)
    loop.facts["api_key"] = key
    status, body, headers = loop.api("/v1/signals?tech=kubernetes&limit=3", key)
    assert status == 200 and body["data"], (status, body)
    domain = loop.facts["dossier_company"]["domain"]
    status, company, _ = loop.api(f"/v1/companies/{domain}", key)
    assert status == 200 and company["dossier_available"] and company["dossier_url"], company
    via = "curl" if loop.use_curl and shutil.which("curl") else "urllib"
    return (f"key emailed; {via} /v1/signals → 200 ({body['pagination']['total']} companies, remaining "
            f"{headers.get('x-ratelimit-remaining')}); /v1/companies/{domain} offers the dossier")


def dossier(loop: Loop) -> str:
    company = loop.facts["dossier_company"]
    from api.data import company_id

    cid = company_id(company["company"])
    status, headers, _ = loop.http("GET", f"/v1/dossiers/{cid}/buy?src=loop")
    location = headers.get("Location") or headers.get("location") or ""
    assert status == 303 and location.startswith("https://checkout.stripe.com/"), (status, location)
    sid = location.rsplit("/", 1)[1]
    meta = loop.stripe.sessions[sid]["metadata"]
    assert meta["company_id"] == cid and meta["kind"] == "dossier"
    loop.webhook("checkout.session.completed", loop.session(id=sid, amount_total=4900, metadata=meta,
                                                            client_reference_id=loop.stripe.sessions[sid]["client_reference_id"],
                                                            customer_details={"email": EXECUTIVE}))
    msg = loop.wait(lambda: loop.mails_to(EXECUTIVE), "dossier email", 15)[-1]
    pdf = next(v for k, v in _attachments(msg).items() if k.endswith(".pdf"))
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF"), "not a PDF"
    pages = int(re.search(rb"/Count (\d+)", pdf).group(1))
    return f"checkout 303 → Stripe, webhook → {company['company']} dossier PDF ({pages} page(s), {len(pdf) // 1024 + 1} KB) emailed"


def dunning(loop: Loop) -> str:
    key = loop.facts["api_key"]
    loop.webhook("invoice.payment_failed", {"id": "in_loop_fail", "object": "invoice", "subscription": "sub_loop_api",
                                            "amount_due": 2900, "status": "open"})
    status, _, headers = loop.api("/v1/me", key)
    assert status == 200 and headers.get("x-api-key-status") == "past_due" and headers.get("x-ratelimit-limit") == "50", headers
    reminder = loop.wait(lambda: [m for m in loop.mails_to(DEVELOPER) if "didn't go through" in m["Subject"]], "dunning email")[-1]
    assert "billing.stripe.com" in reminder.get_body(("plain",)).get_content()
    loop.webhook("invoice.paid", {"id": "in_loop_fail", "object": "invoice", "subscription": "sub_loop_api", "amount_paid": 2900,
                                  "status": "paid", "customer_email": DEVELOPER,
                                  "status_transitions": {"paid_at": int(time.time())}})  # Smart Retries succeeded
    _, _, headers = loop.api("/v1/me", key)
    assert headers.get("x-ratelimit-limit") == str(loop.tools.config.api_daily_quota) and "x-api-key-status" not in headers
    return "payment failure → key degraded to 50/day + billing-portal reminder; payment → full access restored"


def recovery(loop: Loop) -> str:
    before = len(loop.mails_to(BUYER))
    status, _, body = loop.http("POST", "/v1/orders/recover", json.dumps({"email": BUYER}).encode(), {"Content-Type": "application/json"})
    assert status == 202, (status, body)
    stranger_status, _, stranger = loop.http("POST", "/v1/orders/recover", json.dumps({"email": "nobody@loop.example"}).encode(),
                                             {"Content-Type": "application/json"})
    assert stranger_status == 202 and stranger == body, "the response must not reveal who is a customer"
    msg = loop.wait(lambda: loop.mails_to(BUYER)[before:], "recovery email")[-1]
    assert any(k.endswith(".zip") for k in _attachments(msg))
    return "202 (identical for strangers), purchase re-sent to the buyer only"


def revenue(loop: Loop) -> str:
    from dashboard.snapshot import collect_snapshot

    snap = collect_snapshot(loop.tools.state, loop.tools.config)
    today = snap["revenue_today"]
    assert today["target_met"] and today["net_cents"] >= today["target_cents"], today
    loop.facts["net"] = today["net_cents"]
    return (f"dashboard: ${today['net_cents'] / 100:.2f} net today vs ${today['target_cents'] / 100:.2f} goal "
            f"({today['net_cents'] / today['target_cents']:.0%}), MRR ${snap['distribution']['recurring']['mrr_cents'] / 100:.2f}")


STEPS: list[tuple[str, Callable[[Loop], str]]] = [
    ("Seed 5 postings and ingest leads", ingest),
    ("Extract stack and buying-intent signals", intel),
    ("Package the Executive Tech Radar", package),
    ("Publish checkouts (dataset, subscription, API, dossier)", publish),
    ("Build landers and matrix pages, validate schema", site),
    ("Product factory makes a product and lists it", factory),
    ("$14 dataset purchase → zip delivered", buy_dataset),
    ("$10/month subscription → Monday digest staged", subscribe),
    ("$29/month API → key issued, live curl query", api_tier),
    ("$49 dossier → PDF delivered", dossier),
    ("Failed payment → dunning and recovery", dunning),
    ("Self-service order recovery", recovery),
    ("Net revenue ≥ $10/day on the dashboard", revenue),
]


# ----------------------------------------------------------------------------- output
class Paint:
    def __init__(self, enabled: bool):
        self.on = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


def run(keep: bool = False, use_curl: bool = True, color: bool | None = None, out: Any = None, json_out: bool = False) -> int:
    out = out or sys.stdout
    paint = Paint(out.isatty() if color is None else color)
    sandbox = Path(tempfile.mkdtemp(prefix="automonetize-loop-"))
    loop = Loop(use_curl=use_curl)
    results: list[Step] = []
    started = time.monotonic()
    if not json_out:
        print(paint("1", "AutoMonetize full-loop test") + paint("2", "  (sandboxed: simulated Stripe, captured email, throwaway data)"),
              file=out)
    try:
        t0 = time.monotonic()
        try:
            detail = setup(loop, sandbox)
            results.append(Step("Start the listener in a sandbox", True, detail, time.monotonic() - t0))
        except Exception as exc:  # noqa: BLE001
            results.append(Step("Start the listener in a sandbox", False, f"{exc!r}", time.monotonic() - t0))
        for name, fn in STEPS:
            if results and not results[-1].ok:
                results.append(Step(name, False, "skipped (an earlier step failed)"))
                continue
            t0 = time.monotonic()
            try:
                detail = fn(loop)
                results.append(Step(name, True, detail, time.monotonic() - t0))
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                tb = traceback.extract_tb(exc.__traceback__)[-1]
                results.append(Step(name, False, f"{type(exc).__name__}: {exc} ({Path(tb.filename).name}:{tb.lineno})", time.monotonic() - t0))
            if not json_out:
                s = results[-1]
                mark = paint("32;1", "✔") if s.ok else paint("31;1", "✘")
                print(f" {mark} {s.name:<56} {paint('2', f'{s.seconds:5.1f}s')}\n   {paint('2' if s.ok else '31', s.detail)}", file=out)
    finally:
        loop.stop.set()
        time.sleep(0.3)
        if loop.tools is not None:
            loop.tools.state.close()
        if not keep:
            shutil.rmtree(sandbox, ignore_errors=True)
    total = time.monotonic() - started
    passed = sum(1 for s in results if s.ok)
    ok = passed == len(results)
    if json_out:
        print(json.dumps({"ok": ok, "seconds": round(total, 2), "net_cents": loop.facts.get("net"),
                          "steps": [s.__dict__ for s in results], "sandbox": str(sandbox) if keep else None}, indent=2), file=out)
    else:
        verdict = paint("42;30;1", " PASS ") if ok else paint("41;37;1", " FAIL ")
        net = f" · ${loop.facts['net'] / 100:.2f} simulated net revenue" if loop.facts.get("net") else ""
        print(f"\n{verdict} {passed}/{len(results)} steps in {total:.1f}s{net}", file=out)
        if keep:
            print(paint("2", f"sandbox kept at {sandbox}"), file=out)
        if ok:
            print(paint("2", "Everything works end to end. Next: automonetize gui → enter your keys → automonetize supervise"), file=out)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - `python -m cli.test_loop` (used by the evolution worktree checks)
    args = sys.argv[1:]
    sys.exit(run(keep="--keep" in args, use_curl="--no-curl" not in args, color=False if "--no-color" in args else None,
                 json_out="--json" in args))
