"""Regression tests for the line-by-line audit after Phase 400."""

from agent.config import Config


def test_list_settings_from_env_are_comma_separated():
    cfg = Config.load(env={"AUTOMONETIZE_FACTORY_TYPES": "micro, stack", "AUTOMONETIZE_SOURCE_SEED_FEEDS": "https://a/feed"})
    assert cfg.factory_types == ["micro", "stack"]
    assert cfg.source_seed_feeds == ["https://a/feed"]


def test_blank_number_line_keeps_the_default():
    cfg = Config.load(env={"SMTP_PORT": "", "AUTOMONETIZE_HTTP_TIMEOUT": " "})
    assert cfg.smtp_port == 587
    assert cfg.http_timeout == 15.0


def test_the_api_tier_and_dossiers_are_not_datasets_for_lifetime_passes_or_roundups():
    from agent.state import StateStore
    from strategies import revenue_models as rm

    s = StateStore(":memory:")
    for kind, path in (("api_subscription", "https://x.example/docs/api"), ("dossier", "dossiers/"),
                       ("lead_directory", "assets/py/py-v1.zip")):
        aid = s.add_asset(1, kind, kind, path, 1, 0, 2900, product_ref=f"plink_{kind}")
        s.update_asset(aid, niche=None if kind != "lead_directory" else "py", provider="stripe",
                       checkout_url=f"https://buy.stripe.com/{kind}", status="published")
    assert [a["kind"] for a in rm.live_products(s)] == ["lead_directory"]


def test_hidden_products_are_not_advertised_but_still_count_for_you():
    from agent.config import Config
    from agent.state import StateStore
    from strategies import content_engine, revenue_models as rm, sales_channels
    from strategies.product_controls import HIDDEN

    s = StateStore(":memory:")
    for slug in ("hiring-rust", "hiring-go"):
        aid = s.add_asset(1, "micro", slug, f"assets/{slug}/{slug}-v1.zip", 1, 30, 900, product_ref=f"plink_{slug}")
        s.update_asset(aid, niche=slug, provider="stripe", checkout_url=f"https://buy.stripe.com/{slug}", status="published")
    s.set(HIDDEN, ["hiring-go"])
    cfg = Config()
    assert [a["niche"] for a in rm.listed_products(s)] == ["hiring-rust"]
    assert {a["niche"] for a in rm.live_products(s)} == {"hiring-rust", "hiring-go"}  # lifetime passes still get it
    assert [p["title"] for p in sales_channels.catalog_json(s, cfg)] == ["hiring-rust"]
    assert "hiring-go" not in content_engine.products_feed(s, cfg, "Site")
    assert {r["niche"] for r in sales_channels.leaderboard(s)} == {"hiring-rust", "hiring-go"}


def test_the_crash_sweep_never_deletes_the_bundle_folder(tmp_path):
    import os
    import time

    from agent.state import StateStore
    from strategies import crash_safety
    from tools.file_io import SandboxedFileIO as FileStore

    s = StateStore(":memory:")
    files = FileStore(tmp_path)
    files.write_bytes("assets/bundles/all-datasets-a-b.zip", b"PK")
    files.write_bytes("assets/leftover/x.zip", b"PK")
    old = time.time() - 3 * 86400
    for d in ("assets/bundles", "assets/leftover"):
        os.utime(files.resolve(d), (old, old))
    aid = s.add_asset(1, "bundle", "All", "assets/bundles/all-datasets-a-b.zip", 1, 10, 1900)
    s.update_asset(aid, niche="bundle", status="published")
    assert crash_safety.orphan_folders(s, files) == ["leftover"]


def test_the_form_guard_forgets_quiet_networks_under_a_flood(monkeypatch):
    from tools import abuse_guard as ag

    class Clock:
        t = 0.0

        def __call__(self):
            return self.t

    monkeypatch.setattr(ag, "MAX_TRACKED", 50)
    clock = Clock()
    g = ag.Guard(None, clock)
    for i in range(500):
        clock.t = i * 10.0  # each network posts once and goes quiet
        assert g.check(f"10.1.{i // 250}.{i % 250}", {"text": "rust please"}, ("text",)) is None
    assert len(g._posts) <= 51


def test_the_chat_webhook_address_never_reaches_the_settings_log_or_evolution_checks(tmp_path, monkeypatch):
    from agent.evolution.evolver import sandbox_env
    from tools.settings_log import snapshot

    hook = "https://hooks.slack.com/services/T000/B000/abcdefghijkl"
    cfg = Config.load(env={"CHAT_WEBHOOK_URL": hook})
    assert hook not in str(snapshot(cfg))
    monkeypatch.setenv("CHAT_WEBHOOK_URL", hook)
    monkeypatch.setenv("NTFY_TOPIC", "my-private-topic")
    env = sandbox_env(tmp_path)
    assert "CHAT_WEBHOOK_URL" not in env and "NTFY_TOPIC" not in env


def test_webhook_and_heartbeat_addresses_are_redacted_from_logs():
    from tools.redact import redact

    text = ("HTTP 404 for https://hooks.slack.com/services/T0/B0/abcdefghijkl; "
            "https://discord.com/api/webhooks/123456/abcDEF_ghi-123 and https://hc-ping.com/1f2e3d4c-aaaa-bbbb")
    out = redact(text)
    assert "abcdefghijkl" not in out and "abcDEF" not in out and "1f2e3d4c" not in out and out.count("[redacted]") == 3


def test_the_api_failure_limiter_does_not_grow_with_every_client():
    from api.ratelimit import FailureLimiter

    t = [0.0]
    lim = FailureLimiter(3, clock=lambda: t[0])
    for i in range(1000):
        assert lim.blocked(f"10.2.{i // 250}.{i % 250}") == 0.0  # good clients: checked, never stored
    assert not lim._hits
    for i in range(25_000):
        t[0] = i * 1.0  # each network fails once and goes quiet
        lim.record(f"n{i}")
    assert len(lim._hits) <= 20_001
    for _ in range(3):
        lim.record("bad")
    assert lim.blocked("bad") > 0
