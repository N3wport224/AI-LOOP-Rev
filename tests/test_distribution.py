import json

import pytest

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import TaskContext
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.distribution_engine import DistributionEngine
from strategies.tech_stack_intel import TechStackIntel
from tests.conftest import NOW
from tools import build_toolkit
from tools.dispatcher import PostmarkBackend, SendGridBackend
from tools.http_client import Response
from tools.inbox import is_unsubscribe, poll_unsubscribes


class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=30):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, u, p):
        self.user = u

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise ConnectionError("smtp down")
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp():
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    return FakeSMTP


@pytest.fixture
def kit(config, state, breaker, transport, smtp):
    return build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=smtp)


def go_live(config):
    config.dry_run = False
    config.smtp_host = "smtp.example.com"
    config.sender_postal_address = "1 Main St, Springfield, IL 62701, USA"


def approved(state, n=1, domain="co.com", hid=1):
    ids = []
    for i in range(n):
        oid = state.stage_outreach(hid, f"lead{i}{domain}", "email", f"hr{i}@{domain}", f"Subject {i}", "Hello there.", 0.9)
        state.set_outreach_status(oid, "approved")
        ids.append(oid)
    return ids


def audit(config):
    path = config.data_dir / "dispatched_audit.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def pipeline(kit, hyp):
    for strat, task in ((LeadAggregator(), "aggregate_leads"), (TechStackIntel(), "build_intel"), (DigitalAssetPackager(), "package_asset")):
        strat.run(task, TaskContext(kit, hyp, {}))


# ------------------------------------------------------------------ dispatcher: dry run
def test_dry_run_is_default_and_audits_without_sending(kit, state, config, smtp):
    assert config.dry_run is True
    ids = approved(state, 2)
    state.stage_outreach(1, "pending", "email", "p@co.com", "s", "b", 0.9)  # not approved: never touched
    rep = kit.dispatcher.dispatch_approved()
    assert (rep.sent, rep.dry_run) == (0, 2)
    assert smtp.sent == []
    records = audit(config)
    assert [r["mode"] for r in records] == ["dry_run", "dry_run"] and {r["to"] for r in records} == {"hr0@co.com", "hr1@co.com"}
    assert "List-Unsubscribe" in records[0]["headers"]
    assert all(state.list_outreach("approved")[i]["dry_run_at"] for i in range(2))
    # second pass doesn't re-log the same drafts
    assert kit.dispatcher.dispatch_approved().dry_run == 0 and len(audit(config)) == 2
    assert {r["status"] for r in state.list_outreach()} == {"approved", "pending_review"}
    assert ids


def test_env_dry_run_flag(tmp_path):
    from agent.config import Config

    assert Config.load(None, env={"DRY_RUN": "false"}).dry_run is False
    assert Config.load(None, env={}).dry_run is True


# ------------------------------------------------------------------ dispatcher: live
def test_live_requires_can_spam_fields(kit, state, config, smtp):
    config.dry_run = False
    config.smtp_host = "smtp.example.com"
    approved(state)
    rep = kit.dispatcher.dispatch_approved()
    assert rep.sent == 0 and any("postal_address" in r for r in rep.reasons)
    assert smtp.sent == []


def test_live_send_has_compliance_headers_and_footer(kit, state, config, smtp):
    go_live(config)
    config.unsubscribe_url = "https://me.example/unsub"
    approved(state)
    rep = kit.dispatcher.dispatch_approved()
    assert rep.sent == 1
    msg = smtp.sent[0]
    assert msg["To"] == "hr0@co.com" and msg["Subject"] == "Subject 0"
    assert msg["List-Unsubscribe"] == "<mailto:sam@example.com?subject=unsubscribe>, <https://me.example/unsub>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    body = msg.get_content()
    assert "1 Main St, Springfield" in body and 'Reply "unsubscribe"' in body
    row = state.list_outreach("sent")[0]
    assert row["message_id"] and row["sent_at"]
    assert audit(config)[-1]["mode"] == "live" and audit(config)[-1]["result"] == "sent"


def test_warmup_cap_and_ramp(kit, state, config, smtp, clock):
    go_live(config)
    approved(state, 8)
    rep = kit.dispatcher.dispatch_approved()
    assert (rep.sent, rep.deferred, rep.limit) == (5, 3, 5)
    assert kit.dispatcher.dispatch_approved().sent == 0  # cap is per day
    clock.advance(days=1)
    assert kit.dispatcher.dispatch_approved().sent == 3
    clock.advance(days=14)
    assert kit.dispatcher.daily_limit() == 5 + 5 * 2
    config.dispatch_max_per_day = 12
    assert kit.dispatcher.daily_limit() == 12


def test_suppression_and_blocked_tlds(kit, state, config, smtp):
    go_live(config)
    approved(state, 1, domain="firma.de")
    approved(state, 1, domain="ok.com")
    state.suppress("hr0@ok.com", "test")
    rep = kit.dispatcher.dispatch_approved()
    assert rep.sent == 0
    assert {r["status"] for r in state.list_outreach()} == {"blocked", "suppressed"}
    assert any("blocked TLD .de" in r for r in rep.reasons)


def test_send_failures_stop_run_and_eventually_fail_row(kit, state, config, smtp):
    go_live(config)
    smtp.fail = True
    approved(state, 5)
    rep = kit.dispatcher.dispatch_approved(max_failures=2)
    assert rep.failed == 2 and "stopped" in rep.reasons[-1]
    for _ in range(3):
        kit.dispatcher.dispatch_approved(max_failures=10)
    assert state.list_outreach("failed")


def test_api_backend_payloads():
    from tools.dispatcher import Attachment, Email

    msg = Email("to@x.com", "Subj", "Body", "delivery", {"List-Unsubscribe": "<mailto:u@x>"}, [Attachment("a.zip", b"PK")])
    sg = SendGridBackend.payload(msg, "me@x.com", "Me")
    assert sg["personalizations"][0]["to"] == [{"email": "to@x.com"}]
    assert sg["from"] == {"email": "me@x.com", "name": "Me"} and sg["attachments"][0]["content"] == "UEs="
    pm = PostmarkBackend.payload(msg, "me@x.com", "Me")
    assert pm["MessageStream"] == "outbound" and pm["Attachments"][0]["Name"] == "a.zip"
    assert pm["Headers"] == [{"Name": "List-Unsubscribe", "Value": "<mailto:u@x>"}]
    msg.kind = "outreach"
    assert PostmarkBackend.payload(msg, "me@x.com", "")["MessageStream"] == "broadcast"


def test_sendgrid_backend_over_http(config, state, breaker, transport):
    config.dry_run, config.outreach_email_backend, config.sendgrid_api_key = False, "sendgrid", "SG.key"
    config.sender_postal_address = "1 Main St"
    transport.add("https://api.sendgrid.com/v3/mail/send", Response(202, "u", b"", {"x-message-id": "sg-1"}))
    kit = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None)
    approved(state)
    assert kit.dispatcher.dispatch_approved().sent == 1
    call = transport.calls_to("https://api.sendgrid.com")[0]
    assert call["headers"]["Authorization"] == "Bearer SG.key"
    assert json.loads(call["body"])["headers"]["List-Unsubscribe"].startswith("<mailto:")
    assert state.list_outreach("sent")[0]["message_id"] == "sg-1"


# ------------------------------------------------------------------ inbox
def test_unsubscribe_detection_ignores_quoted_text():
    assert is_unsubscribe("Re: hi", "Please unsubscribe me")
    assert is_unsubscribe("Unsubscribe", "")
    assert is_unsubscribe("Re: hi", "No thanks.\n\nOn Mon, Sam wrote:\n> Reply no thanks")
    assert not is_unsubscribe("Re: hi", "Sounds great, let's talk.\n\nOn Mon, Sam wrote:\n> Reply \"unsubscribe\" and...")


class FakeIMAP:
    def __init__(self, host):
        self.flags = {}
        raw = {
            b"1": b"From: HR <hr0@co.com>\r\nSubject: Re: hello\r\n\r\nunsubscribe please\r\n",
            b"2": b"From: cto@co.com\r\nSubject: Re: hello\r\n\r\nInterested! Call me.\r\n",
        }
        self.raw = raw

    def login(self, u, p):
        pass

    def select(self, box):
        return "OK", [b"2"]

    def search(self, charset, crit):
        return "OK", [b"1 2"]

    def fetch(self, num, what):
        return "OK", [(b"1 (RFC822)", self.raw[num])]

    def store(self, num, op, flag):
        self.flags[num] = op

    def logout(self):
        pass


def test_poll_unsubscribes(state):
    approved(state, 1)
    out = poll_unsubscribes(state, "imap.x", "u", "p", imap_factory=FakeIMAP)
    assert out == ["hr0@co.com"]
    assert state.is_suppressed("hr0@co.com") and not state.is_suppressed("cto@co.com")
    assert state.list_outreach()[0]["status"] == "suppressed"


# ------------------------------------------------------------------ distribution tasks
def test_showcase_sample_is_sanitized_and_published_to_repo(kit, config, transport, make_hypothesis):
    hyp = make_hypothesis()
    pipeline(kit, hyp)
    config.github_token, config.github_showcase_repo = "ghp", "me/showcase"
    kit.github.token = "ghp"
    transport.add("https://api.github.com/repos/me/showcase/contents/", [
        Response(404, "u"), Response(201, "u", b'{"content": {"html_url": "https://github.com/me/showcase/blob/main/showcase/python-remote/README.md"}}'),
    ])
    res = DistributionEngine().run("publish_showcase", TaskContext(kit, hyp, {}))
    assert res.metrics["url"].endswith("showcase/python-remote/README.md")
    md = kit.files.read_text("showcase/python-remote/README.md")
    assert "free preview" in md and "@" not in md and "Complete dataset coming soon" in md
    table_rows = [l for l in md.splitlines() if l.startswith("| ") and "company" not in l]
    assert len(table_rows) == 4
    assert kit.state.latest_asset(hyp["id"], "lead_directory")["showcase_url"]


def test_showcase_gist_mode_reuses_gist(kit, config, transport, make_hypothesis):
    hyp = make_hypothesis()
    pipeline(kit, hyp)
    config.github_token, config.github_showcase_mode = "ghp", "gist"
    kit.github.token = "ghp"
    transport.add_json("https://api.github.com/gists", {"id": "g1", "html_url": "https://gist.github.com/g1"}, 201)
    DistributionEngine().run("publish_showcase", TaskContext(kit, hyp, {}))
    DistributionEngine().run("publish_showcase", TaskContext(kit, hyp, {}))
    assert transport.calls_to("https://api.github.com/gists", "POST")
    assert transport.calls_to("https://api.github.com/gists/g1", "PATCH")
    assert json.loads(transport.calls_to("https://api.github.com/gists", "POST")[0]["body"])["public"] is True


def test_publish_listing_refuses_undeliverable_stripe_checkout(kit, config, transport, make_hypothesis, state, breaker):
    hyp = make_hypothesis()
    pipeline(kit, hyp)
    config.stripe_secret_key = "sk_test"
    kit = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)
    res = DistributionEngine().run("publish_listing", TaskContext(kit, hyp, {}))
    assert res.metrics["blocked"] == "fulfilment"
    assert not transport.calls_to("https://api.stripe.com")


def test_publish_listing_goes_live_on_stripe_and_writes_lander(kit, config, transport, make_hypothesis, state, breaker):
    hyp = make_hypothesis()
    pipeline(kit, hyp)
    config.stripe_secret_key = "sk_test"
    go_live(config)
    transport.add_json("https://api.stripe.com/v1/products", {"id": "prod_1"})
    transport.add_json("https://api.stripe.com/v1/prices", {"id": "price_1"})
    transport.add_json("https://api.stripe.com/v1/payment_links", {"id": "plink_1", "url": "https://buy.stripe.com/t"})
    kit = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)
    res = DistributionEngine().run("publish_listing", TaskContext(kit, hyp, {}))
    assert res.metrics["published"] and res.metrics["checkout_url"] == "https://buy.stripe.com/t"
    asset = state.latest_asset(hyp["id"], "lead_directory")
    assert (asset["provider"], asset["product_ref"], asset["status"]) == ("stripe", "plink_1", "published")
    assert "https://buy.stripe.com/t" in kit.files.read_text("site/python-remote/index.html")
    again = DistributionEngine().run("publish_listing", TaskContext(kit, hyp, {}))
    assert "already live" in again.summary
    assert len(transport.calls_to("https://api.stripe.com/v1/payment_links", "POST")) == 1


def test_publish_listing_gumroad_fallback_stages(kit, make_hypothesis):
    hyp = make_hypothesis()
    pipeline(kit, hyp)
    res = DistributionEngine().run("publish_listing", TaskContext(kit, hyp, {}))
    assert not res.metrics["published"] and res.metrics["provider"] == "gumroad"
    assert kit.files.exists("site/python-remote/index.html")


def test_deliver_orders_dry_run_then_live(kit, config, state, make_hypothesis, smtp):
    hyp = make_hypothesis()
    pipeline(kit, hyp)
    asset = state.latest_asset(hyp["id"], "lead_directory")
    state.record_order("stripe", "cs_1", "buyer@x.com", 900, "plink_1", asset["id"], hyp["id"])
    state.record_order("stripe", "cs_2", None, 900, None, None, None)
    res = DistributionEngine().run("deliver_orders", TaskContext(kit, hyp, {}))
    assert res.metrics["pending"] == 1 and res.metrics["manual"] == 1 and smtp.sent == []
    assert audit(config)[-1]["kind"] == "delivery" and audit(config)[-1]["attachments"][0]["bytes"] > 0
    go_live(config)
    res = DistributionEngine().run("deliver_orders", TaskContext(kit, hyp, {}))
    assert res.metrics["delivered"] == 1
    msg = smtp.sent[0]
    assert msg["To"] == "buyer@x.com" and "Your download" in msg["Subject"]
    atts = list(msg.iter_attachments())
    assert atts[0].get_filename() == "python-remote-intel-v1.zip"
    assert state.order_counts() == {"delivered": 1, "needs_manual_delivery": 1}


def test_dispatch_outreach_task_reports_mode(kit, state, make_hypothesis):
    hyp = make_hypothesis()
    approved(state, 2, hid=hyp["id"])
    res = DistributionEngine().run("dispatch_outreach", TaskContext(kit, hyp, {}))
    assert res.summary.startswith("dry-run") and res.metrics["dry_run"] == 2


def test_collect_metrics_uses_github_traffic(kit, config, transport, state, make_hypothesis):
    hyp = make_hypothesis()
    config.github_token, config.github_showcase_repo = "ghp", "me/showcase"
    kit.github.token = "ghp"
    transport.add_json("https://api.github.com/repos/me/showcase/traffic/popular/paths", [
        {"path": "/me/showcase/tree/main/showcase/python-remote", "count": 12, "uniques": 5},
        {"path": "/me/showcase/blob/main/showcase/python-remote/README.md", "count": 3, "uniques": 2},
        {"path": "/me/showcase/tree/main/showcase/other", "count": 50, "uniques": 9},
    ])
    state.record_order("stripe", "cs_1", "b@x.com", 900, "p", None, hyp["id"])
    res = DistributionEngine().run("collect_metrics", TaskContext(kit, hyp, {}))
    assert res.metrics["views"] == 15 and res.metrics["purchases"] == 1
    assert state.get("view_tracking") is True


def test_closed_loop_through_engine(config, state, breaker, transport, smtp):
    """Cycle 1 publishes a Stripe checkout; a sale appears; cycle 2 records verified revenue and emails the file."""
    from agent.engine import Engine

    go_live(config)
    config.stripe_secret_key = "sk_test"
    transport.add_json("https://api.stripe.com/v1/products", {"id": "prod_1"})
    transport.add_json("https://api.stripe.com/v1/prices", {"id": "price_1"})
    transport.add_json("https://api.stripe.com/v1/payment_links", {"id": "plink_1", "url": "https://buy.stripe.com/t"})
    transport.add_json("https://api.stripe.com/v1/checkout/sessions", {"has_more": False, "data": []})
    kit = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)
    engine = Engine(config, state=state, toolkit=kit, sleep=lambda s: None)

    first = engine.run_cycle()
    assert all(a["status"] == "ok" for a in first.actions), [a for a in first.actions if a["status"] != "ok"]
    asset = state.latest_asset(first.hypothesis_id, "lead_directory")
    assert asset["checkout_url"] == "https://buy.stripe.com/t"

    transport.add_json("https://api.stripe.com/v1/checkout/sessions", {"has_more": False, "data": [
        {"id": "cs_live_1", "payment_status": "paid", "amount_total": 1500, "created": int(NOW.timestamp()),
         "customer_details": {"email": "buyer@acme.example"}}]})
    engine.run_cycle()
    assert state.revenue_for_hypothesis(first.hypothesis_id) == 1500 - 74  # 2.9% + 30c
    assert kit.revenue.daily_summary()["target_met"]
    assert state.order_counts() == {"delivered": 1}
    delivery = [m for m in smtp.sent if m["To"] == "buyer@acme.example"]
    assert len(delivery) == 1 and list(delivery[0].iter_attachments())
    # only one one-off Payment Link was ever created (the other is the $10/month subscription offer)
    links = transport.calls_to("https://api.stripe.com/v1/payment_links", "POST")
    one_off = [c for c in links if b"subscription_data" not in c["body"]]
    assert len(one_off) == 1 and len(links) - len(one_off) == 1
