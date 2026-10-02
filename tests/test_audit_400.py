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
