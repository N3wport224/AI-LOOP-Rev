import json

import pytest
from rich.console import Console

from agent.config import Config
from agent.engine import Engine
from dashboard.cli import main
from dashboard.render import fmt_duration, render_dashboard
from dashboard.snapshot import collect_snapshot


def _console():
    return Console(record=True, width=140, force_terminal=False)


def test_snapshot_and_render_after_pipeline(config, state, toolkit):
    engine = Engine(config, state=state, toolkit=toolkit, sleep=lambda s: None)
    engine.run_cycle()
    state.record_revenue("gumroad", "s1", 900, 140, 760, True)
    state.log_error("http_client", "HTTP 503 for https://x")
    snap = collect_snapshot(state, config)
    assert snap["iteration"] == 1
    assert snap["hypothesis"]["key"] == "lead_directory:python-remote:g1"
    assert snap["leads_total"] == 6 and snap["assets_total"] == 1  # pooled leads, all niches
    assert snap["revenue_today"]["net_cents"] == 760
    assert snap["breaker"]["status"] == "OK"

    console = _console()
    console.print(render_dashboard(snap))
    out = console.export_text()
    for expected in (
        "Active Objective", "Reach $10.00/day", "lead_directory:python-remote:g1", "Iteration", "Uptime",
        "Staged Assets / Leads", "Python Remote Tech Stack Intel v1", "Daily Revenue vs Target", "$7.60",
        "$10.00 target", "Circuit breaker", "OK", "HTTP 503", "Recent Actions", state.recent_actions(1)[0]["name"],  # the latest
    ):
        assert expected in out, expected


def test_render_shows_emergency_stop(config, state, toolkit):
    engine = Engine(config, state=state, toolkit=toolkit, sleep=lambda s: None)
    engine.emergency_stop("too many errors")
    console = _console()
    console.print(render_dashboard(collect_snapshot(state, config)))
    out = console.export_text()
    assert "STOPPED" in out and "too many errors" in out


def test_render_empty_state(config, state):
    console = _console()
    console.print(render_dashboard(collect_snapshot(state, config)))
    assert "none active" in console.export_text()


def test_fmt_duration():
    assert fmt_duration(3725) == "01:02:05"
    assert fmt_duration(90061) == "1d 01:01:01"


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in list(__import__("os").environ):
        if k.startswith("AUTOMONETIZE_") or k == "GUMROAD_ACCESS_TOKEN":
            monkeypatch.delenv(k)
    path = tmp_path / "automonetize.toml"
    assert main(["--config", str(path), "init"], console=_console()) == 0
    return path


def test_cli_init_status_revenue(cfg_file, capsys):
    c = _console()
    assert main(["-c", str(cfg_file), "revenue", "add", "9.00", "--verified", "--external-id", "tx1"], console=c) == 0
    assert main(["-c", str(cfg_file), "revenue", "add", "9.00", "--verified", "--external-id", "tx1"], console=c) == 1
    assert main(["-c", str(cfg_file), "revenue", "add", "$3", "--note", "maybe"], console=c) == 0
    assert main(["-c", str(cfg_file), "revenue", "report", "--days", "2", "--ledger"], console=c) == 0
    out = c.export_text()
    assert "$9.00" in out and "tx1" in out
    capsys.readouterr()
    assert main(["-c", str(cfg_file), "status", "--json"], console=c) == 0
    snap = json.loads(capsys.readouterr().out)
    assert snap["revenue_today"]["net_cents"] == 900
    assert snap["revenue_today"]["unverified_cents"] == 300


def test_cli_stop_resume(cfg_file):
    c = _console()
    assert main(["-c", str(cfg_file), "stop", "--reason", "maintenance"], console=c) == 0
    assert main(["-c", str(cfg_file), "run", "--once", "--headless"], console=c) == 3
    assert "refusing to start" in c.export_text()
    assert main(["-c", str(cfg_file), "resume"], console=c) == 0
    assert not (cfg_file.parent / "data" / "EMERGENCY_STOP").exists()


def test_cli_outreach_review_flow(cfg_file):
    config = Config.load(cfg_file)
    from agent.state import StateStore

    state = StateStore(config.db_path)
    a = state.stage_outreach(1, "l1", "email", "a@x.com", "Subj A", "Body A", 0.9)
    b = state.stage_outreach(1, "l2", "email", "b@x.com", "Subj B", "Body B", 0.9)
    c = _console()
    assert main(["-c", str(cfg_file), "outreach", "list", "--full"], console=c) == 0
    assert "Body A" in c.export_text()
    main(["-c", str(cfg_file), "outreach", "approve", str(a)], console=c)
    main(["-c", str(cfg_file), "outreach", "reject", str(b)], console=c)
    main(["-c", str(cfg_file), "outreach", "export"], console=c)
    exported = list((config.data_dir / "outbox").glob("approved-*.md"))
    assert len(exported) == 1
    text = exported[0].read_text()
    assert "Body A" in text and "Body B" not in text
    main(["-c", str(cfg_file), "outreach", "mark-sent", str(a)], console=c)
    assert state.outreach_counts() == {"sent": 1, "rejected": 1}


def test_cli_assets_link_and_hypotheses(cfg_file):
    config = Config.load(cfg_file)
    from agent.state import StateStore

    state = StateStore(config.db_path)
    hid = state.create_hypothesis("k", "s", "d", {"niche": "n"})
    aid = state.add_asset(hid, "lead_directory", "T", "p.zip", 1, 5, 900)
    c = _console()
    assert main(["-c", str(cfg_file), "assets", "link", str(aid), "prod_9"], console=c) == 0
    assert state.hypothesis_for_product("prod_9") == hid
    assert main(["-c", str(cfg_file), "assets", "list"], console=c) == 0
    assert main(["-c", str(cfg_file), "hypotheses"], console=c) == 0
    assert "prod_9" in c.export_text()


def test_config_env_overrides(tmp_path):
    env = {
        "AUTOMONETIZE_DATA_DIR": str(tmp_path / "d"),
        "AUTOMONETIZE_PIVOT_AFTER_ITERATIONS": "7",
        "AUTOMONETIZE_RESPECT_ROBOTS_TXT": "false",
        "AUTOMONETIZE_HTTP_RATE_PER_MINUTE": "5.5",
        "AUTOMONETIZE_LEAD_SOURCES": "remoteok, arbeitnow",
        "AUTOMONETIZE_NICHES": '[{"name": "x", "keywords": ["y"]}]',
        "GUMROAD_ACCESS_TOKEN": "secret",
    }
    cfg = Config.load(None, env=env)
    assert cfg.pivot_after_iterations == 7 and cfg.respect_robots_txt is False
    assert cfg.http_rate_per_minute == 5.5 and cfg.lead_sources == ["remoteok", "arbeitnow"]
    assert cfg.niches == [{"name": "x", "keywords": ["y"]}]
    assert cfg.gumroad_access_token == "secret"
    assert cfg.db_path == tmp_path / "d" / "agent_state.db"


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[automonetize]\nnot_a_key = 1\n")
    with pytest.raises(ValueError):
        Config.load(p, env={})


def test_render_shows_logged_text_as_is_and_the_recurring_row(config, state):
    state.log_error("support", "reply from [deleted] about [/bold] tags")
    state.log_action(1, None, "deliver", "ok", "sent to [deleted]")
    state.upsert_subscriber("stripe", "sub_1", email="a@b.example", price_cents=900, interval="month", status="active")
    console = _console()
    console.print(render_dashboard(collect_snapshot(state, config)))  # a stray closing tag used to raise MarkupError
    out = console.export_text()
    assert "reply from [deleted] about [/bold] tags" in out and "sent to [deleted]" in out
    assert "MRR $9.00" in out
