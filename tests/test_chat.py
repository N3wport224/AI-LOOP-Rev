"""Phases 305-309: Slack and Discord notifications."""

import json

import pytest

from tools import chat
from tools.notify import push

SLACK = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnop"
DISCORD = "https://discord.com/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz_ABC"


@pytest.mark.parametrize("url,kind", [
    (SLACK, "slack"), (DISCORD, "discord"), ("https://hooks.slack.com.evil.example/services/x", ""),
    ("http://hooks.slack.com/services/T000/B000/abcdefghijk", ""), ("https://example.com/api/webhooks/1/2", ""), ("", ""),
])
def test_only_slack_and_discord(url, kind):
    assert chat.service(url) == kind


def test_every_notification_goes_to_the_channel_without_emails(toolkit, state, config, transport):
    config.chat_webhook_url, config.ntfy_topic = SLACK, ""
    transport.add("https://hooks.slack.com/", lambda m, url, h: __import__("tools.http_client", fromlist=["R"]).Response(200, url, b"ok", {}))
    assert push(toolkit.http, config, "New sale: $9", "Rust dataset bought by jo@co.example", state=state)
    sent = json.loads(transport.calls_to("https://hooks.slack.com/")[-1]["body"])
    assert sent == {"text": "New sale: $9\nRust dataset bought by [email]"}


def test_discord_payload_and_no_mentions(toolkit, state, config, transport):
    from tools.http_client import Response

    config.chat_webhook_url = DISCORD
    transport.add("https://discord.com/", Response(204, "u", b"", {}))
    transport.add("https://discord.com/api/webhooks/", Response(200, "u", b"", {}))
    assert chat.post(toolkit.http, config, "Alert", "@everyone check this", state)
    body = json.loads(transport.calls_to("https://discord.com/")[-1]["body"])
    assert body["content"] == "Alert\n@everyone check this" and body["allowed_mentions"] == {"parse": []}


def test_never_a_flood(toolkit, state, config, transport, clock, monkeypatch):
    from tools.http_client import Response

    monkeypatch.setattr(chat, "PER_HOUR", 2)
    config.chat_webhook_url = SLACK
    transport.add("https://hooks.slack.com/", Response(200, "u", b"ok", {}))
    results = [chat.post(toolkit.http, config, "t", str(i), state) for i in range(5)]
    assert results == [True, True, False, False, False]
    clock.advance(hours=1)
    assert chat.post(toolkit.http, config, "t", "later", state)
    last = json.loads(transport.calls_to("https://hooks.slack.com/")[-1]["body"])["text"]
    assert last.endswith("(3 more notification(s) were held back in the last hour)")


def test_failures_never_raise(toolkit, state, config, transport):
    config.chat_webhook_url = SLACK
    transport.add("https://hooks.slack.com/", RuntimeError("down"))
    assert chat.post(toolkit.http, config, "t", "m", state) is False


def test_chat_command(tmp_path, monkeypatch, config, state, transport, capsys):
    from cli import growth
    from tools.http_client import Response

    monkeypatch.setattr(growth, "ROOT", tmp_path)
    monkeypatch.setattr(growth, "_setup", lambda: (config, state, None))
    monkeypatch.setattr("cli.go_live.restart", lambda env: None)
    transport.add("https://hooks.slack.com/", Response(200, "u", b"ok", {}))
    assert growth.chat_main(["https://example.com/hook"], transport=transport) == 2
    assert growth.chat_main([SLACK], transport=transport) == 0
    assert "CHAT_WEBHOOK_URL=" in (tmp_path / ".env").read_text()
    out = capsys.readouterr().out
    assert "Slack: test message sent" in out and SLACK not in out


def test_features_and_doctor_never_show_the_address(config, state):
    from cli.doctor import Doctor
    from tests.test_autopilot import Ctl, runner

    config.chat_webhook_url = SLACK
    findings = {f.name: f for f in Doctor(config, state, Ctl(pid=1), run=runner({}), system="Linux").checks(deep=False)}
    assert findings["Chat notifications"].detail == "Slack webhook set"
    assert all(SLACK not in f"{f.detail} {f.fix}" for f in findings.values())
