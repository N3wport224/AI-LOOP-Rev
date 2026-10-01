"""Phases 385-389: checks and docs for the newer settings."""

from agent.config_check import problems
from tests import test_gui

gui = test_gui.gui


def test_factory_and_chat_settings_are_checked(config):
    config.factory_types = ["slice", "salaries"]
    config.factory_interval_seconds = 30
    config.factory_max_live = 3
    config.chat_webhook_url = "https://example.com/hook"
    found = "\n".join(problems(config))
    assert "factory_types: unknown type(s) salaries" in found and "factory_interval_seconds = 30" in found
    assert "factory_max_live = 3" in found and "chat_webhook_url isn't a Slack" in found
    config.factory_types, config.factory_interval_seconds, config.factory_max_live = ["slice"], 600, 500
    config.chat_webhook_url = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnop"
    assert not [p for p in problems(config) if "factory" in p or "chat" in p]


def test_doctor_shows_the_product_types(config, state):
    from cli.doctor import Doctor
    from tests.test_autopilot import Ctl, runner

    config.factory_types = ["slice", "salary"]
    findings = {f.name: f for f in Doctor(config, state, Ctl(pid=1), run=runner({}), system="Linux").checks(deep=False)}
    assert findings["Product types"].detail.startswith("2 of ") and "not: top" in findings["Product types"].detail


def test_panel_takes_a_chat_webhook(gui):
    async def scenario(client):
        csrf = await test_gui.login(client)
        bad = await client.post("/api/settings", json={"values": {"CHAT_WEBHOOK_URL": "https://example.com/x"}},
                                headers={"X-CSRF-Token": csrf})
        assert bad.status == 400 and "Slack" in (await bad.json())["errors"]["CHAT_WEBHOOK_URL"]
        d = await (await client.get("/api/settings")).json()
        assert any(f["key"] == "CHAT_WEBHOOK_URL" for f in d["fields"])

    test_gui.run(gui, scenario)
