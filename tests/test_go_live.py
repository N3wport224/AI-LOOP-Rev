"""`automonetize go-live`: checks before anything changes, then save, restart, link."""

import pytest

from cli import go_live


@pytest.fixture
def checkout(tmp_path, monkeypatch, config, state):
    env_file = tmp_path / ".env"
    env_file.write_text("STRIPE_SECRET_KEY=rk_test_abc\nDRY_RUN=true\nSMTP_HOST=smtp.gmail.com\n")
    monkeypatch.setattr(go_live, "ROOT", tmp_path)
    monkeypatch.setattr("agent.setup_autonomous.load_env_into", lambda f, base=None: {
        **{k: v for k, v in (line.split("=", 1) for line in f.read_text().splitlines() if "=" in line)},
        "AUTOMONETIZE_DATA_DIR": str(config.data_dir)})
    calls = {}
    monkeypatch.setattr(go_live, "restart", lambda env: calls.setdefault("restart", env) is not None)
    monkeypatch.setattr(go_live, "wait_for_link", lambda env, minutes=10: 0)
    monkeypatch.setattr(go_live, "check_email", lambda env: True)
    return env_file, calls


def accounts(balance=(200, {"livemode": True}), account=(200, {"charges_enabled": True, "payouts_enabled": True})):
    return lambda key, path: balance if path == "balance" else account


@pytest.mark.parametrize("key,stripe,ok", [
    ("rk_test_123", accounts(), False),                                       # a test key is refused outright
    ("rk_live_123", accounts(balance=(401, {})), False),                      # rejected by Stripe
    ("rk_live_123", accounts(account=(200, {"charges_enabled": False})), False),  # account not activated
    ("rk_live_123", accounts(account=(403, {})), True),                       # can't read account: continue
    ("sk_live_123", accounts(), True),
])
def test_stripe_checks(monkeypatch, key, stripe, ok):
    monkeypatch.setattr(go_live, "stripe_get", stripe)
    assert go_live.check_stripe(key) is ok


def test_nothing_changes_when_a_check_fails(checkout, monkeypatch):
    env_file, calls = checkout
    before = env_file.read_text()
    monkeypatch.setattr("getpass.getpass", lambda prompt: "rk_test_nope")
    assert go_live.main([]) == 1
    monkeypatch.setattr("getpass.getpass", lambda prompt: "rk_live_ok")
    monkeypatch.setattr(go_live, "stripe_get", accounts())
    monkeypatch.setattr(go_live, "check_email", lambda env: False)
    assert go_live.main([]) == 1
    assert env_file.read_text() == before and "restart" not in calls


def test_switches_to_live_and_republishes_test_listings(checkout, monkeypatch, state, make_hypothesis):
    env_file, calls = checkout
    hyp = make_hypothesis()
    aid = state.add_asset(hyp["id"], "lead_directory", "T", "x.zip", 1, 10, 1400)
    state.update_asset(aid, provider="stripe", status="published", checkout_url="https://buy.stripe.com/test_abc")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "rk_live_ok")
    monkeypatch.setattr(go_live, "stripe_get", accounts())
    assert go_live.main([]) == 0
    text = env_file.read_text()
    assert "STRIPE_SECRET_KEY=rk_live_ok" in text and "DRY_RUN=false" in text and "STRIPE_MODE=live" in text
    asset = state.get_asset(aid)
    assert asset["status"] == "staged" and asset["checkout_url"] is None and "restart" in calls


def test_email_check_names_what_is_missing(config, capsys):
    env = {"AUTOMONETIZE_DATA_DIR": str(config.data_dir), "SMTP_HOST": "smtp.gmail.com"}
    assert go_live.check_email(env) is False
    assert "Email delivery isn't ready" in capsys.readouterr().out


def test_email_login_falls_back_to_the_other_port_and_unspaced_password(monkeypatch):
    attempts = []

    def fake_login(host, port, user, password):
        attempts.append((port, password))
        return None if (port, password) == (465, "abcdefghijklmnop") else "SMTPServerDisconnected: Connection unexpectedly closed"

    monkeypatch.setattr(go_live, "smtp_login", fake_login)
    assert go_live.smtp_candidates(587, "abcd efgh ijkl mnop") == [
        (587, "abcd efgh ijkl mnop"), (465, "abcd efgh ijkl mnop"), (587, "abcdefghijklmnop"), (465, "abcdefghijklmnop")]

    class Cfg:
        smtp_host, smtp_port, smtp_username, smtp_password = "smtp.gmail.com", 587, "me@gmail.com", "abcd efgh ijkl mnop"

    class Dispatcher:
        @staticmethod
        def compliance_problems(kind):
            return []

    monkeypatch.setattr("agent.config.Config.load", classmethod(lambda cls, path=None, env=None: Cfg()))
    Cfg.dry_run = True
    Cfg.ensure_dirs = lambda self: None
    Cfg.db_path = ":memory:"
    monkeypatch.setattr("tools.build_toolkit", lambda *a, **k: type("T", (), {"dispatcher": Dispatcher})())
    env = {}
    assert go_live.check_email(env) is True
    assert env == {"SMTP_PORT": "465", "SMTP_PASSWORD": "abcdefghijklmnop"} and len(attempts) == 4
