"""Phases 32-35: self-update, owner to-do, referral rewards, subscriber win-back."""

import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from agent import self_update
from agent.evolution.evolver import Check
from agent.self_update import SelfUpdate
from strategies.buyer_followup import BuyerFollowup
from strategies.owner_reports import OwnerReports
from strategies.owner_todo import as_text, todo
from strategies.referrals import Referrals, token_for
from strategies.winback import WinBack
from tests import test_business_ops
from tests.test_business_ops import STRIPE, ctx, dataset
from tests.test_distribution import FakeSMTP

kit = test_business_ops.kit  # the same live-mode toolkit fixture
OK = [Check("checks", [sys.executable, "-c", "print('ok')"], 60)]
FAIL = [Check("checks", [sys.executable, "-c", "import sys; sys.exit(1)"], 60)]


def body_of(msg):
    return msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()


# ------------------------------------------------------------------ Phase 32: self-update
def git(cwd, *args):
    out = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t.invalid", "-c", "commit.gpgsign=false",
                          "-c", "init.defaultBranch=main", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def repos(tmp_path, config, monkeypatch):
    """origin (bare) <- upstream work clone, and the agent's checkout tracking origin/main."""
    monkeypatch.delenv(self_update.NO_UPDATE_ENV, raising=False)
    origin, work, agent = tmp_path / "origin.git", tmp_path / "work", tmp_path / "agent"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "app.py").write_text("VERSION = 1\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "v1")
    git(work, "push", "-q", "origin", "HEAD:main")
    git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(agent))
    config.evolution_repo, config.auto_update, config.evolution_canary_minutes = str(agent), True, 60
    return work, agent


def publish(work, text, name="app.py"):
    (work / name).write_text(text)
    git(work, "add", ".")
    git(work, "commit", "-qm", f"update {name}")
    git(work, "push", "-q", "origin", "HEAD:main")


def test_a_verified_update_is_installed_and_reloads(repos, toolkit, state, clock):
    work, agent = repos
    reloads = []
    t = TaskContext_(toolkit)
    assert SelfUpdate(checks=OK, reload=reloads.append).run("self_update", t).summary == "up to date"
    publish(work, "VERSION = 2\n")
    clock.advance(hours=7)
    res = SelfUpdate(checks=OK, reload=reloads.append).run("self_update", t)
    assert res.metrics == {"behind": 1, "installed": True} and reloads
    assert (agent / "app.py").read_text() == "VERSION = 2\n"
    assert not list(Path(agent).glob("../automonetize-update-*"))
    assert "watching" in SelfUpdate(checks=OK).run("self_update", t).summary
    # A clean cycle after the window passes the canary.
    clock.advance(minutes=61)
    assert self_update.canary_tick(state, toolkit.config, cycle_done=int(state.get("iteration", 0)) + 1) == "passed"


def test_failing_checks_keep_the_old_code_and_skip_that_version(repos, toolkit, state, clock):
    work, agent = repos
    publish(work, "VERSION = 'broken'\n")
    res = SelfUpdate(checks=FAIL, reload=lambda r: None).run("self_update", TaskContext_(toolkit))
    assert res.metrics["installed"] is False and (agent / "app.py").read_text() == "VERSION = 1\n"
    assert "not installed" in state.recent_errors(1, kind="alert")[0]["message"]
    clock.advance(hours=7)
    assert "skipping" in SelfUpdate(checks=OK).run("self_update", TaskContext_(toolkit)).summary


def test_a_misbehaving_update_is_rolled_back(repos, toolkit, state, clock):
    work, agent = repos
    before = git(agent, "rev-parse", "HEAD")
    publish(work, "VERSION = 3\n")
    SelfUpdate(checks=OK, reload=lambda r: None).run("self_update", TaskContext_(toolkit))
    assert (agent / "app.py").read_text() == "VERSION = 3\n"
    state.log_error("engine", "cycle crashed: boom")
    reloads = []
    assert self_update.canary_tick(state, toolkit.config, reload=reloads.append) == "rolled_back"
    assert git(agent, "rev-parse", "HEAD") == before and reloads
    assert "rolled back" in state.recent_errors(1, kind="alert")[0]["message"]


def test_local_commits_are_merged_and_conflicts_stop_the_update(repos, toolkit, state, clock):
    work, agent = repos
    (agent / "local.py").write_text("X = 1\n")
    git(agent, "add", ".")
    git(agent, "commit", "-qm", "local evolution")
    publish(work, "VERSION = 4\n")
    assert SelfUpdate(checks=OK, reload=lambda r: None).run("self_update", TaskContext_(toolkit)).metrics["installed"]
    assert (agent / "local.py").exists() and (agent / "app.py").read_text() == "VERSION = 4\n"
    state.set("self_update", {"checked_at": None})
    (agent / "app.py").write_text("VERSION = 'mine'\n")
    git(agent, "commit", "-qam", "local edit")
    publish(work, "VERSION = 5\n")
    res = SelfUpdate(checks=OK, reload=lambda r: None).run("self_update", TaskContext_(toolkit))
    assert res.metrics["installed"] is False and "conflicts" in res.summary


def test_never_over_uncommitted_changes_or_in_a_sandbox(repos, toolkit, state, monkeypatch):
    work, agent = repos
    (agent / "app.py").write_text("VERSION = 'editing'\n")
    publish(work, "VERSION = 6\n")
    assert "uncommitted" in SelfUpdate(checks=OK).run("self_update", TaskContext_(toolkit)).summary
    monkeypatch.setenv(self_update.NO_UPDATE_ENV, "1")
    assert SelfUpdate(checks=OK).run("self_update", TaskContext_(toolkit)).summary == "self-update off"


def TaskContext_(toolkit):
    from strategies.base import TaskContext

    return TaskContext(toolkit, {"id": 1, "params": {"niche": "python-remote"}, "iterations": 0}, {})


# ------------------------------------------------------------------ Phase 33: owner to-do
def test_todo_lists_only_what_is_missing(kit, state, config):
    config.sender_postal_address, config.heartbeat_url = "", ""
    titles = [i["title"] for i in todo(state, config)]
    assert titles == ["Add your postal address", "Connect marketing", "Turn on the heartbeat"]
    config.dry_run = True
    assert todo(state, config)[0]["how"] == "automonetize go-live"
    config.dry_run, config.sender_postal_address = False, "1 Main St, Springfield, IL 62701, USA"
    config.github_pages_repo, config.pages_base_url, config.heartbeat_url = "me/me.github.io", "https://me.github.io", "https://hc-ping.com/x"
    from strategies.distribution import mark_directory

    assert todo(state, config)[0]["title"].startswith("Submit the catalog to")  # once the site is live (Phase 187)
    mark_directory(state, "datarade")
    mark_directory(state, "kaggle")
    assert todo(state, config) == [] and "Nothing for you to do" in as_text([])


def test_todo_surfaces_customers_waiting(kit, state, config, clock):
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_m", "m@co.example", 1900, None, aid, None, status="needs_manual_delivery")
    state.record_revenue("stripe", "dispute:dp_1", -1900, 1500, -3400, True)
    titles = [i["title"] for i in todo(state, config)]
    assert titles[:2] == ["Answer 1 dispute(s) in Stripe", "Deliver 1 order(s) by hand"]
    state.record_revenue("stripe", "dispute_won:dp_1", 1900, 0, 1900, True)
    assert "Answer 1 dispute(s) in Stripe" not in [i["title"] for i in todo(state, config)]
    assert OwnerReports.digest_body(kit, clock()).startswith("Only you can do these")


# ------------------------------------------------------------------ Phase 34: referral rewards
def test_followups_carry_a_personal_referral_link(kit, state, clock):
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "a@co.example", 1900, None, aid, None, status="delivered")
    state._exec("UPDATE orders SET delivered_at = ?", ((clock() - timedelta(days=4)).isoformat(timespec="seconds"),))
    BuyerFollowup().run("follow_up_buyers", ctx(kit))
    body = body_of(FakeSMTP.sent[-1])
    token = token_for(state, "a@co.example")
    assert f"client_reference_id=am--ref--{token}" in body and "next updated version free" in body


def test_a_referred_sale_earns_the_newest_version(kit, state, clock):
    _, aid = dataset(kit, state, "python-remote")
    token = token_for(state, "a@co.example")
    when = (clock() - timedelta(days=8)).isoformat(timespec="seconds")
    state.record_order("stripe", "cs_f", "friend@co.example", 1900, None, aid, None, occurred_at=when, status="delivered",
                       channel="ref", campaign=token)
    state.record_order("stripe", "cs_s", "a@co.example", 1900, None, aid, None, occurred_at=when, status="delivered",
                       channel="ref", campaign=token)  # self-referral
    assert Referrals().run("reward_referrals", ctx(kit)).metrics["rewarded"] == 1
    msg = FakeSMTP.sent[-1]
    assert msg["To"] == "a@co.example" and "free update" in msg["Subject"]
    assert [p.get_filename() for p in msg.iter_attachments()] == ["python-remote-v1.zip"]
    assert Referrals().run("reward_referrals", ctx(kit)).metrics["rewarded"] == 0  # once


def test_rewards_wait_for_the_refund_window_and_the_monthly_gap(kit, state, clock):
    _, aid = dataset(kit, state, "python-remote")
    token = token_for(state, "a@co.example")
    for i, days in enumerate((2, 9, 9)):
        state.record_order("stripe", f"cs_{i}", f"f{i}@co.example", 1900, None, aid, None, status="delivered", channel="ref",
                           campaign=token, occurred_at=(clock() - timedelta(days=days)).isoformat(timespec="seconds"))
    assert Referrals().run("reward_referrals", ctx(kit)).metrics["rewarded"] == 1  # one of the 9-day-old ones
    clock.advance(days=31)
    assert Referrals().run("reward_referrals", ctx(kit)).metrics["rewarded"] == 1  # the next one, a month later


# ------------------------------------------------------------------ Phase 35: win-back
def subscription(state, kit, transport, niche="python-remote"):
    hid, _ = dataset(kit, state, niche)
    aid = state.add_asset(hid, "subscription", f"{niche} Weekly", f"assets/{niche}/{niche}-v1.zip", 1, 50, 1000,
                          product_ref="plink_sub")
    state.update_asset(aid, niche=niche, provider="stripe", status="published", checkout_url="https://buy.stripe.com/sub")
    transport.add_json(f"{STRIPE}/payment_links/plink_sub/line_items", {"data": [{"price": {"product": "prod_sub"}}]})
    transport.add_json(f"{STRIPE}/coupons", {"id": "co_1"})
    transport.add_json(f"{STRIPE}/promotion_codes", {"id": "promo_1"})


def canceled(state, clock, sub_id, email, days_ago, niche="python-remote"):
    sid, _ = state.upsert_subscriber("stripe", sub_id, email=email, niche=niche, price_cents=1000, status="canceled")
    state._exec("UPDATE subscribers SET canceled_at = ? WHERE id = ?",
                ((clock() - timedelta(days=days_ago)).isoformat(timespec="seconds"), sid))


def test_canceled_subscribers_get_one_discounted_invitation(kit, state, transport, clock):
    subscription(state, kit, transport)
    canceled(state, clock, "sub_1", "gone@co.example", 10)
    canceled(state, clock, "sub_2", "recent@co.example", 3)       # too soon
    canceled(state, clock, "sub_3", "old@co.example", 45)         # too long ago
    canceled(state, clock, "sub_4", "back@co.example", 12)
    state.upsert_subscriber("stripe", "sub_5", email="back@co.example", niche="python-remote", price_cents=1000, status="active")
    assert WinBack().run("win_back", ctx(kit)).metrics["sent"] == 1
    msg = FakeSMTP.sent[-1]
    body = body_of(msg)
    assert msg["To"] == "gone@co.example" and "$5 instead of $10" in body and "prefilled_promo_code=BACKPYTH" in body
    assert "only time I'll ask" in body and "1 Main St" in body
    promo = parse_qs(transport.calls_to(f"{STRIPE}/promotion_codes", "POST")[-1]["body"].decode())
    assert promo["max_redemptions"] == ["1"]
    assert WinBack().run("win_back", ctx(kit)).metrics["sent"] == 0  # once


def test_winback_respects_suppression_dry_run_and_the_switch(kit, state, transport, clock, config):
    subscription(state, kit, transport)
    canceled(state, clock, "sub_1", "gone@co.example", 10)
    state.suppress("gone@co.example", "t")
    assert WinBack().run("win_back", ctx(kit)).metrics["sent"] == 0
    canceled(state, clock, "sub_2", "other@co.example", 10)
    config.dry_run = True
    assert WinBack().run("win_back", ctx(kit)).metrics["sent"] == 1 and not transport.calls_to(f"{STRIPE}/coupons")
    config.winback = False
    assert WinBack().run("win_back", ctx(kit)).summary == "win-back off"


@pytest.mark.parametrize("task", ["self_update", "reward_referrals", "win_back"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10


def test_cli_and_gui_expose_the_todo():
    from dashboard.cli import build_parser

    assert build_parser().parse_args(["todo"]).func.__name__ == "cmd_todo"
    html = (Path(__file__).parents[1] / "gui/static/index.html").read_text()
    assert 'id="todo-list"' in html
