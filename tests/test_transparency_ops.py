"""Phases 90-94: key-age reminders, settings log, Sent tab, export-all, features overview."""

import json
import zipfile
from datetime import timedelta

from strategies.owner_todo import todo
from tests import test_gui
from tools import key_age, settings_log
from tools.export_all import export_all
from tools.features import FEATURES, overview

gui = test_gui.gui  # the control-panel fixture


# ------------------------------------------------------------------ Phase 90: key age
def test_key_ages_track_fingerprints_not_keys(state, config, clock):
    config.stripe_secret_key, config.github_token = "sk_live_secret_value", "ghp_token"
    ages = key_age.update(state, config)
    stored = json.dumps(state.get("key_ages"))
    assert "sk_live_secret_value" not in stored and "ghp_token" not in stored and len(ages["stripe_secret_key"]["fp"]) == 12
    clock.advance(days=181)
    key_age.update(state, config)
    over = {k["attr"] for k in key_age.overdue(state)}
    assert over == {"stripe_secret_key"}  # the GitHub token gets a year
    assert any(i["title"] == "Roll your Stripe key" for i in todo(state, config))
    config.stripe_secret_key = "sk_live_rolled"
    key_age.update(state, config)
    assert not key_age.overdue(state)  # a new key starts its own clock


# ------------------------------------------------------------------ Phase 91: settings log
def test_settings_changes_are_logged_without_secrets(state, config):
    assert settings_log.record(state, config) == []  # the first snapshot is just a baseline
    config.dry_run = not config.dry_run
    config.owner_email, config.smtp_password = "boss@me.example", "new-app-password"
    changes = settings_log.record(state, config)
    flipped = f"dry_run: {json.dumps(not config.dry_run)} → {json.dumps(config.dry_run)}"
    assert flipped in changes and "smtp_password: set" in changes
    assert any(c.startswith("owner_email:") for c in changes)
    dump = json.dumps(state.get("settings_history")) + json.dumps(state.get("settings_snapshot"))
    assert "new-app-password" not in dump
    assert settings_log.record(state, config) == []


# ------------------------------------------------------------------ Phase 92: Sent tab
def test_sent_tab_lists_emails_newest_first(gui, config):
    log = config.data_dir / "dispatched_audit.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(json.dumps({"ts": f"2026-09-30T1{i}:00:00", "kind": "delivery", "to": f"a{i}@co.example",
                                         "subject": f"S{i}", "mode": "live", "result": "sent", "body": "hello"}) for i in range(3))
                   + "\nnot json\n")

    async def scenario(client):
        await test_gui.login(client)
        data = await (await client.get("/api/sent")).json()
        assert [e["subject"] for e in data["emails"]] == ["S2", "S1", "S0"] and data["emails"][0]["body"] == "hello"

    test_gui.run(gui, scenario)


# ------------------------------------------------------------------ Phase 93: export-all
def test_export_contains_every_table_but_not_the_command_code(state, config, clock):
    state.record_revenue("stripe", "cs_1", 1900, 85, 1815, True)
    state.set("owner_command_code", "ABC123")
    state.set("goal_streak", 3)
    path, counts = export_all(state, config.data_dir, clock())
    zf = zipfile.ZipFile(path)
    assert "tables/revenue.csv" in zf.namelist() and counts["revenue"] == 1
    store = json.loads(zf.read("settings_store.json"))
    assert store["goal_streak"] == 3 and "owner_command_code" not in store
    assert oct(path.stat().st_mode)[-3:] == "600"


# ------------------------------------------------------------------ Phase 94: features overview
def test_features_say_on_off_or_waiting(state, config):
    config.sender_postal_address, config.winback, config.heartbeat_url = "", False, ""
    by = {f["name"]: f for f in overview(config, state)}
    assert len(by) == len(FEATURES)
    assert by["Win-back"]["status"] == "off" and by["Win-back"]["detail"] == "winback = false"
    assert by["Buyer follow-up"]["status"] == "waiting" and "postal address" in by["Buyer follow-up"]["detail"]
    assert by["Heartbeat"]["status"] == "waiting"
    assert by["Security self-audit"]["status"] == "on"


def test_cli_entry_points():
    from dashboard.cli import build_parser

    p = build_parser()
    for cmd, fn in (("settings-log", "cmd_settings_log"), ("export-all", "cmd_export_all"), ("features", "cmd_features")):
        assert p.parse_args([cmd]).func.__name__ == fn
    assert timedelta(0) == timedelta(0)
