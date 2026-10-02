"""Phases 417-419: focus mode, trimming the catalog to the strongest products."""

from agent.config import FOCUS, Config
from strategies import product_factory as pf
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product

kit = test_business_ops.kit


def test_focus_mode_changes_the_defaults_but_not_your_own_settings(tmp_path):
    cfg = Config.load(env={})
    assert cfg.focus_mode and cfg.factory_max_live == 20 and cfg.factory_interval_seconds == 6 * 3600
    assert not cfg.api_enabled and not cfg.offer_lifetime and not cfg.copy_bandit_enabled and cfg.max_active_niches == 1
    assert cfg.offer_custom_request and cfg.product_factory and cfg.marketing_engine  # the core stays on
    mine = Config.load(env={"AUTOMONETIZE_FACTORY_MAX_LIVE": "50", "AUTOMONETIZE_API_ENABLED": "true"})
    assert mine.factory_max_live == 50 and mine.api_enabled  # what you set yourself wins
    toml = tmp_path / "a.toml"
    toml.write_text("[automonetize]\nfocus_mode = false\n")
    off = Config.load(toml, env={})
    assert off.factory_max_live == 500 and off.api_enabled and off.offer_lifetime  # everything back
    assert set(FOCUS) <= {f for f in Config.__dataclass_fields__}


def test_a_catalog_over_the_cap_keeps_its_strongest_products(kit, state, clock, transport, config):
    made = made_product(kit, state, clock, transport)
    for i in range(3):  # three more live products, smaller than the first
        state._exec("INSERT INTO factory_products (slug, asset_id, title, filters, rows, companies, keys_sample, status, "
                    "created_at, published_at) SELECT ?, asset_id, title, filters, ?, companies, keys_sample, 'live', created_at, "
                    "published_at FROM factory_products WHERE slug = ?", (f"small-{i}", 5 + i, made["slug"]))
    state._exec("UPDATE factory_products SET status = 'live' WHERE slug = ?", (made["slug"],))
    config.factory_max_live = 2
    retired = pf.trim_to_cap(kit)
    assert retired == ["small-0", "small-1"]  # the weakest go first
    live = {r["slug"] for r in state._all("SELECT slug FROM factory_products WHERE status = 'live'")}
    assert made["slug"] in live and len(live) == 2
    assert pf.trim_to_cap(kit) == []  # at the cap: nothing more
