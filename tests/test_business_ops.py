"""Phases 16-19: Stripe balance/payouts, support autopilot, all-datasets bundle, backups."""

import io
import json
import zipfile

import pytest

from agent.backup import Backups, list_backups, make_backup, prune, restore_files
from strategies.base import TaskContext
from strategies.bundle_engine import BundleEngine, bundle_price
from strategies.finance import Finance, describe
from strategies.support_desk import SupportDesk, classify
from tests.test_distribution import FakeSMTP, go_live
from tools import build_toolkit
from tools.catalog import live_products
from tools.inbox import imap_settings, scan_mail

STRIPE = "https://api.stripe.com/v1"


@pytest.fixture
def kit(config, state, breaker, transport):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    go_live(config)
    config.stripe_secret_key, config.sender_email, config.sender_name = "rk_live_x", "me@gmail.com", "Me"
    config.smtp_host, config.smtp_username, config.smtp_password = "smtp.gmail.com", "me@gmail.com", "app pass"
    transport.add_json(f"{STRIPE}/products", {"id": "prod_b"})
    transport.add_json(f"{STRIPE}/prices", {"id": "price_b"})
    links = iter(["plink_b1", "plink_b2", "plink_b3"])
    transport.add(f"{STRIPE}/payment_links", lambda method, url, headers: __import__("tools.http_client", fromlist=["Response"]).Response(
        200, url, json.dumps({"id": next(links) if url.endswith("payment_links") else "x", "url": "https://buy.stripe.com/b"}).encode(), {}))
    return build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)


def dataset(kit, state, niche, rows=50, price=1900, link=None):
    hid = state.create_hypothesis(f"lead_directory:{niche}:g1", "lead_directory", f"{niche} data", {"niche": niche})
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        zf.writestr("leads.csv", f"company\n{niche}-co\n")
    rel = f"assets/{niche}/{niche}-v1.zip"
    kit.files.write_bytes(rel, data.getvalue())
    aid = state.add_asset(hid, "lead_directory", f"{niche} Tech Stack Intel", rel, 1, rows, price, product_ref=f"plink_{niche}")
    state.update_asset(aid, provider="stripe", status="published", checkout_url=link or f"https://buy.stripe.com/{niche}")
    return hid, aid


def ctx(kit):
    return TaskContext(kit, {"id": 1, "params": {"niche": "python-remote"}, "iterations": 0}, {})


# ------------------------------------------------------------------ Phase 16: money
def test_balance_and_payouts_are_read_and_described(kit, state, transport):
    transport.add_json(f"{STRIPE}/balance", {"livemode": True, "available": [{"amount": 1200, "currency": "usd"}],
                                             "pending": [{"amount": 1840, "currency": "usd"}, {"amount": 99, "currency": "eur"}]})
    transport.add_json(f"{STRIPE}/payouts", {"data": [{"amount": 3000, "status": "paid", "arrival_date": 1790000000},
                                                      {"amount": 500, "status": "failed", "arrival_date": 1780000000}]})
    res = Finance().run("sync_finance", ctx(kit))
    fin = state.get("stripe_finance")
    assert fin["available_cents"] == 1200 and fin["pending_cents"] == 1840 and fin["paid_out_cents"] == 3000
    assert fin["last_payout"]["amount_cents"] == 3000 and "ready to pay out" in res.summary
    assert "$12.00 ready" in describe(fin) and "last payout $30.00" in describe(fin)


def test_balance_errors_are_reported_not_raised(kit, state, transport):
    transport.add_json(f"{STRIPE}/balance", {"error": {"message": "permission"}}, status=403)
    res = Finance().run("sync_finance", ctx(kit))
    assert res.ok and "couldn't read" in describe(state.get("stripe_finance"))
    assert "first payout usually arrives" in describe({"available_cents": 0, "pending_cents": 1900, "last_payout": None})


# ------------------------------------------------------------------ Phase 18: bundle
def test_bundle_needs_two_niches_and_sells_them_together(kit, state, transport):
    dataset(kit, state, "python-remote")
    assert "need 2" in BundleEngine().run("publish_bundle", ctx(kit)).summary
    dataset(kit, state, "ml-ai", rows=30)
    res = BundleEngine().run("publish_bundle", ctx(kit))
    assert res.metrics["published"]
    bundle = next(a for a in state.list_assets() if a["kind"] == "bundle")
    assert bundle["price_cents"] == bundle_price([{"price_cents": 1900}, {"price_cents": 1900}], 0.4) == 2300
    assert bundle["product_ref"] == "plink_b1" and bundle["status"] == "published"
    with zipfile.ZipFile(kit.files.resolve(bundle["path"])) as zf:
        assert sorted(zf.namelist()) == ["README.txt", "ml-ai/ml-ai-v1.zip", "python-remote/python-remote-v1.zip"]
    assert any(p["kind"] == "bundle" for p in live_products(state))
    assert BundleEngine().run("publish_bundle", ctx(kit)).metrics["published"] is False  # nothing changed


def test_new_versions_refresh_the_zip_and_new_niches_replace_the_link(kit, state, transport):
    _, py = dataset(kit, state, "python-remote")
    dataset(kit, state, "ml-ai")
    BundleEngine().run("publish_bundle", ctx(kit))
    first = next(a for a in state.list_assets() if a["kind"] == "bundle")
    hid = state.get_asset(py)["hypothesis_id"]
    kit.files.write_bytes("assets/python-remote/python-remote-v2.zip", kit.files.read_bytes("assets/python-remote/python-remote-v1.zip"))
    v2 = state.add_asset(hid, "lead_directory", "python-remote v2", "assets/python-remote/python-remote-v2.zip", 2, 60, 1900)
    state.update_asset(v2, provider="stripe", status="published", checkout_url="https://buy.stripe.com/python-remote-v2")
    assert "refreshed" in BundleEngine().run("publish_bundle", ctx(kit)).summary
    with zipfile.ZipFile(kit.files.resolve(first["path"])) as zf:
        assert "python-remote/python-remote-v2.zip" in zf.namelist()
    dataset(kit, state, "fullstack-typescript")
    res = BundleEngine().run("publish_bundle", ctx(kit))
    assert res.metrics["published"]
    assert state.get_asset(first["id"])["status"] == "retired"
    assert any(c["url"].endswith("payment_links/plink_b1") for c in transport.calls)  # the old link was deactivated


def test_a_bundle_order_is_delivered_like_any_dataset(kit, state):
    from strategies.distribution_engine import fulfil_order

    dataset(kit, state, "python-remote")
    dataset(kit, state, "ml-ai")
    BundleEngine().run("publish_bundle", ctx(kit))
    bundle = next(a for a in state.list_assets() if a["kind"] == "bundle")
    state.record_order("stripe", "cs_bundle", "buyer@co.example", bundle["price_cents"], "plink_b1", bundle["id"], bundle["hypothesis_id"])
    assert fulfil_order(kit, state.get_order("stripe", "cs_bundle")) == "delivered"
    names = [p.get_filename() for p in FakeSMTP.sent[-1].iter_attachments()]
    assert names and names[0].startswith("all-datasets")


# ------------------------------------------------------------------ Phase 17: support desk
def test_imap_settings_follow_the_smtp_login(config):
    config.smtp_host, config.smtp_username, config.smtp_password = "smtp.gmail.com", "me@gmail.com", "pw"
    assert imap_settings(config) == ("imap.gmail.com", "me@gmail.com", "pw")
    config.smtp_host = "mail.example.com"
    assert imap_settings(config) == ("", "", "")
    config.imap_host, config.imap_username, config.imap_password = "imap.example.com", "u", "p"
    assert imap_settings(config) == ("imap.example.com", "u", "p")


@pytest.mark.parametrize("subject,body,resend,money", [
    ("Order", "Hi, I never received the file after paying.", True, False),
    ("Re: Your dataset", "Can you resend it? The download link doesn't work.", True, False),
    ("Refund", "This isn't what I expected, I'd like a refund.", False, True),
    ("Re: Your purchases", "Thanks! Quick question about the columns.\\n> didn't get it (quoted)", False, False),
    ("Help", "didn't get my data, and I want my money back", True, True),
])
def test_classification_reads_only_the_customers_words(subject, body, resend, money):
    assert classify(subject, body.replace("\\n", "\n")) == (resend, money)


def test_support_resends_and_escalates(kit, state):
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "buyer@co.example", 1900, "plink_python-remote", aid, None, status="delivered")
    inbox = [{"sender": "buyer@co.example", "subject": "Missing file", "message_id": "<1>", "body": "I didn't get the file"},
             {"sender": "buyer@co.example", "subject": "Refund please", "message_id": "<2>", "body": "please refund me"}]
    seen_filter = []

    def scan(host, user, pw, wanted, since):
        seen_filter.append((host, wanted("buyer@co.example"), wanted("friend@personal.example")))
        return inbox

    res = SupportDesk(scan=scan).run("answer_support", ctx(kit))
    assert seen_filter == [("imap.gmail.com", True, False)]  # only customers' mail is ever opened
    assert res.metrics == {"handled": 2, "resent": 1, "alerted": 1, "unsubscribed": 0, "quotes": 0}
    assert FakeSMTP.sent[-1]["To"] == "buyer@co.example" and FakeSMTP.sent[-1]["Subject"].startswith("Your purchases")
    alert = state.recent_errors(1, kind="alert")[0]
    assert "refund" in alert["message"] and "buyer@co.example" in alert["message"]
    assert SupportDesk(scan=scan).run("answer_support", ctx(kit)).metrics["handled"] == 0  # handled once


def test_support_is_quiet_without_customers_or_inbox(kit, state, config):
    assert "no customers" in SupportDesk(scan=lambda *a: []).run("answer_support", ctx(kit)).summary
    config.smtp_host = "mail.example.com"
    assert "not connected" in SupportDesk(scan=lambda *a: []).run("answer_support", ctx(kit)).summary


def test_scan_mail_is_read_only_and_skips_strangers():
    class IMAP:
        def __init__(self, host):
            self.calls = []

        def login(self, u, p):
            pass

        def select(self, box, readonly=False):
            self.calls.append(("select", readonly))

        def search(self, charset, query):
            self.calls.append(("search", query))
            return "OK", [b"1 2"]

        def fetch(self, num, what):
            self.calls.append(("fetch", num, what))
            sender = b"buyer@co.example" if num == b"1" else b"friend@personal.example"
            raw = b"From: " + sender + b"\r\nSubject: hi\r\nMessage-ID: <" + num + b">\r\n\r\nI didn't get it\r\n"
            return "OK", [(b"1", raw)]

        def store(self, *a):
            raise AssertionError("must not change flags")

        def logout(self):
            pass

    holder = {}
    out = scan_mail("imap.gmail.com", "u", "p", lambda s: s == "buyer@co.example", "01-Oct-2026",
                    imap_factory=lambda h: holder.setdefault("c", IMAP(h)))
    calls = holder["c"].calls
    assert [m["sender"] for m in out] == ["buyer@co.example"]
    assert ("select", True) in calls and all("PEEK" in c[2] for c in calls if c[0] == "fetch")
    assert ("fetch", b"2", "(BODY.PEEK[])") not in calls  # a stranger's body is never downloaded


# ------------------------------------------------------------------ Phase 19: backups
def test_backup_list_prune_and_restore(config, state, tmp_path, clock):
    config.backup_dir = str(tmp_path / "backups")
    env, toml = tmp_path / ".env", tmp_path / "automonetize.toml"
    env.write_text("STRIPE_SECRET_KEY=rk_live_1\n")
    toml.write_text("[automonetize]\n")
    state.set("marker", "before")
    first = make_backup(config, env, toml, clock())
    assert (first / "agent_state.db").exists() and oct((first / ".env").stat().st_mode)[-3:] == "600"
    state.set("marker", "after")
    env.write_text("STRIPE_SECRET_KEY=rk_live_2\n")
    restored = restore_files(config, first.name, env, toml)
    assert "agent_state.db" in restored and env.read_text() == "STRIPE_SECRET_KEY=rk_live_1\n"
    from agent.state import StateStore

    assert StateStore(config.db_path).get("marker") == "before"
    clock.advance(days=20)
    make_backup(config, env, toml, clock())
    assert prune(config, clock()) == 1 and len(list_backups(config)) == 1
    with pytest.raises(FileNotFoundError):
        restore_files(config, "../etc", env, toml)


def test_daily_backup_task(config, state, tmp_path, clock, toolkit):
    config.backup_dir = str(tmp_path / "b")
    task = Backups(workdir=tmp_path)
    c = TaskContext(toolkit, {"id": 1, "params": {}}, {})
    assert "saved" in task.run("backup_data", c).summary
    assert "last backup" in task.run("backup_data", c).summary
    clock.advance(hours=24)
    assert "saved" in task.run("backup_data", c).summary and len(list_backups(config)) == 2
    config.backups_enabled = False
    assert "off" in task.run("backup_data", c).summary


def test_restore_command_backs_up_first_and_restarts(config, state, tmp_path, monkeypatch):
    from cli import backups as cli_backups

    config.backup_dir = str(tmp_path / "b")
    (tmp_path / ".env").write_text("X=1\n")
    monkeypatch.setattr(cli_backups, "ROOT", tmp_path)

    class Ctl:
        calls = []

        def supervisor_pid(self):
            return None

        def launchd_managed(self):
            return True

        def kill(self):
            self.calls.append("kill")

        def start(self):
            self.calls.append("start")
            return {"message": "started"}

    monkeypatch.setattr("cli.doctor.controller", lambda env: (config, state, Ctl()))
    assert cli_backups.main([]) == 0
    name = list_backups(config)[0]["name"]
    assert cli_backups.main(["restore", name], confirm=lambda p: "no") == 1 and Ctl.calls == []
    assert cli_backups.main(["restore", name], confirm=lambda p: "RESTORE") == 0
    assert Ctl.calls == ["kill", "start"] and any("pre-restore" in b["name"] for b in list_backups(config))


def test_the_plan_never_outgrows_the_action_cap(config, state, toolkit):
    from agent.engine import PLAN, Engine

    config.max_actions_per_cycle = 30  # an old automonetize.toml
    engine = Engine(config, state=state, sleep=lambda s: None)
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10
    for task in ("sync_finance", "answer_support", "publish_bundle", "backup_data", "report_owner"):
        assert task in engine.handlers
