"""Phases 116-118: config docs, data retention, accessibility checks in the site audit."""

from agent.housekeeping import prune_logs
from cli import growth
from tools.config_docs import as_markdown, as_text, settings
from tools.site_audit import audit_site


# ------------------------------------------------------------------ Phase 116: config docs
def test_config_docs_come_from_the_code(monkeypatch, capsys):
    by = {r["name"]: r for r in settings()}
    assert by["battery_saver"]["default"] is True and "plugged in" in by["battery_saver"]["help"]
    assert by["interval_seconds"]["section"] == "Loop"
    assert "Wall-clock floor" in by["min_hypothesis_days"]["help"]  # a comment block above the field
    assert "[Loop]" in as_text(list(by.values())) and "| `lead_keep_days` | `365` |" in as_markdown(list(by.values()))
    growth.config_docs_main(["--markdown"])
    assert capsys.readouterr().out.startswith("| Setting | Default | What it does |")


# ------------------------------------------------------------------ Phase 117: retention
def test_old_postings_and_unconfirmed_signups_are_deleted(state, config, clock):
    state.upsert_lead("old", "py", {"company": "Old"})
    state._exec("INSERT INTO subscribers (provider, subscription_id, email, tier, subscription_status, started_at, updated_at) "
                "VALUES ('lead_magnet', 'lm_1', 'never@co.example', 'free', 'pending', ?, ?)", (state.now(), state.now()))
    state._exec("INSERT INTO subscribers (provider, subscription_id, email, tier, subscription_status, started_at, updated_at) "
                "VALUES ('lead_magnet', 'lm_2', 'yes@co.example', 'free', 'active', ?, ?)", (state.now(), state.now()))
    sid = state._one("SELECT id FROM subscribers WHERE subscription_id = 'lm_1'")["id"]
    state.record_subscription_delivery(sid, "confirm-1", "sent")
    clock.advance(days=31)
    state.upsert_lead("new", "py", {"company": "New"})
    pruned = prune_logs(state, config)
    assert pruned["unconfirmed sign-ups"] == 1 and pruned["old job postings"] == 0
    emails = {r["email"] for r in state._all("SELECT email FROM subscribers")}
    assert emails == {"yes@co.example"} and not state.list_subscription_deliveries(sid)
    clock.advance(days=340)  # "old" last seen 371 days ago, "new" 340
    assert prune_logs(state, config)["old job postings"] == 1
    assert [r["company"] for r in state.leads_for_niche("py")] == ["New"]


# ------------------------------------------------------------------ Phase 118: accessibility
def test_accessibility_problems_are_found():
    bad = ('<html><head><title>T</title><meta name="description" content="' + "d" * 60 + '"></head><body><h1>H</h1>'
           '<input type="email" name="e"><input type="hidden" name="h"><a href="x/"><span></span></a>'
           '<button></button></body></html>')
    good = ('<html lang="en"><head><title>T</title><meta name="description" content="' + "d" * 60 + '"></head><body><h1>H</h1>'
            '<label for="e">Email</label><input type="email" id="e"><label>Site <input name="w"></label>'
            '<input aria-label="Search" name="q"><a href="x/"><img src="i.png" alt="Home"></a><a href="x/">Text</a>'
            '<button aria-label="Close"></button></body></html>')
    files = {"x/index.html": good, "i.png": b""}
    report = audit_site({"bad.html": bad, **files})
    assert report["a11y"] == ["bad.html: no lang on <html>", "bad.html: 1 form field(s) without a label",
                              "bad.html: 1 link(s) with no text", "bad.html: 1 button(s) with no text"]
    assert audit_site({"good.html": good, **files})["a11y"] == []
