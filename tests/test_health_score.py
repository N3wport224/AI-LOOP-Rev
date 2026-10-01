"""Phase 400: one number for how the agent is doing."""

from agent import health_score as hs
from tests import test_gui

gui = test_gui.gui


def test_score_and_label():
    assert hs.score([]) == 100 and hs.score(["ok"] * 10) == 100
    assert hs.score(["ok", "warn"]) == 75 and hs.label(75) == "fine, a few things to look at"
    assert hs.score(["ok"] * 99 + ["fail"]) == hs.FAIL_CAP  # a failure can't hide behind green checks
    assert hs.label(95) == "healthy" and hs.label(40) == "needs you now"


def test_recorded_and_reported(state):
    out = hs.record(state, [{"status": "ok"}, {"status": "warn"}, {"status": "fail"}])
    assert out["score"] == 50 and out["fails"] == 1 and out["warns"] == 1
    assert hs.weekly_line(state).startswith("Health score: 50/100 (needs attention)")


def test_in_the_panel(gui):
    async def scenario(client):
        await test_gui.login(client)
        d = await (await client.get("/api/health")).json()
        assert 0 <= d["score"]["score"] <= 100 and d["score"]["label"]
        html = await (await client.get("/")).text()
        assert 'id="health-score"' in html

    test_gui.run(gui, scenario)


def test_doctor_prints_it(config, state, capsys):
    from cli.doctor import Finding, report

    report([Finding("A", "ok", "fine"), Finding("B", "warn", "hmm", "do x")])
    assert "Health score: 75/100" in capsys.readouterr().out
