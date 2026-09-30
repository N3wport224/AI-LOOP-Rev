"""Phase 10: `automonetize test-full-loop` runs the whole business end to end in a sandbox."""

import io
import json
import time

from rich.console import Console

from cli.test_loop import STEPS, run


def test_full_loop_passes_quickly_and_prints_a_summary():
    out = io.StringIO()
    started = time.monotonic()
    assert run(color=False, out=out) == 0, out.getvalue()
    assert time.monotonic() - started < 60
    text = out.getvalue()
    assert "PASS" in text and f"{len(STEPS) + 1}/{len(STEPS) + 1} steps" in text and "\x1b[" not in text
    for name, _ in STEPS:
        assert name in text


def test_full_loop_json_without_curl_and_color():
    out = io.StringIO()
    assert run(use_curl=False, color=True, out=out, json_out=True) == 0
    result = json.loads(out.getvalue())
    assert result["ok"] and result["net_cents"] >= 1000 and all(s["ok"] for s in result["steps"])
    assert result["sandbox"] is None  # removed afterwards


def test_colorized_output_uses_ansi():
    out = io.StringIO()
    assert run(color=True, out=out) == 0
    assert "\x1b[" in out.getvalue()


def test_cli_command_is_registered(monkeypatch):
    from dashboard import cli

    seen = {}
    monkeypatch.setattr("cli.test_loop.run", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["test-full-loop", "--no-curl", "--no-color"], console=Console(record=True)) == 0
    assert seen == {"keep": False, "use_curl": False, "color": False, "json_out": False}
