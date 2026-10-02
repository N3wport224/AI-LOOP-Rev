"""Phases 390-394: newer stores in export and erase, retention, inventory."""

from strategies import buyer_experience as bx
from strategies import opt_out as oo
from strategies.customer_requests import KEY as REQUESTS
from strategies.posting_hygiene import DEAD
from strategies.source_efficiency import CACHE_DIR, VALIDATORS
from tools import data_retention as dr
from tools.privacy import export, forget


def seed(state):
    oo.record(state, "Acme", "jo@acme.example", "please")
    state.set(bx.WATCH, [{"email": "jo@acme.example", "wanted": {"tech": "rust"}, "at": state.now()},
                         {"email": "other@x.example", "wanted": {"tech": "go"}, "at": state.now()}])
    state.set(REQUESTS, [{"at": state.now(), "email": "jo@acme.example", "text": "elixir please", "topics": []}])


def test_export_and_forget_cover_the_newer_stores(state, config):
    seed(state)
    data = export(state, config, "Jo@Acme.example")
    assert data["opt_out_requests"][0]["company"] == "Acme" and data["ready_notices"][0]["wanted"] == {"tech": "rust"}
    assert data["dataset_requests"][0]["text"] == "elixir please"
    done = forget(state, config, "jo@acme.example")
    assert done["opt-out requests anonymised"] == 1 and done["ready notices deleted"] == 1
    assert done["dataset requests anonymised"] == 1
    assert oo.pending(state)[0]["email"].endswith("@invalid") and oo.pending(state)[0]["company"] == "Acme"
    assert [w["email"] for w in state.get(bx.WATCH)] == ["other@x.example"]
    assert "jo@acme.example" not in str(state.get(REQUESTS))


def test_retention(state, toolkit, clock):
    seed(state)
    entry = oo.pending(state)[0]
    oo.decide(toolkit, entry["id"], approve=False)
    toolkit.files.write_bytes(f"{CACHE_DIR}/k1.bin", b"x")
    state.set(VALIDATORS, {"k1": {"etag": "a", "used_at": state.now()}})
    state.set(DEAD, ["gone-key"])
    assert dr.prune(state, toolkit.files) == {"dead-posting marks": 1}
    clock.advance(days=dr.OPTOUT_KEEP_DAYS + 1)
    out = dr.prune(state, toolkit.files)
    assert out == {"old opt-out addresses": 1, "unused saved responses": 1}
    assert not toolkit.files.exists(f"{CACHE_DIR}/k1.bin") and state.get(VALIDATORS) == {}


def test_inventory(state, config):
    seed(state)
    rows = {r["store"]: r for r in dr.inventory(state, config)}
    assert rows["Opt-out requests"]["records"] == 1 and rows["\"It's ready\" requests"]["records"] == 2
    assert rows["Suppression list"]["records"] == 0
    assert dr.describe(dr.inventory(state, config))[0].startswith("Orders (buyer email): 0")


def test_cli_parses():
    from dashboard.cli import build_parser

    args = build_parser().parse_args(["privacy", "inventory"])
    assert args.action == "inventory" and args.email == ""


def test_forget_redacts_an_address_logged_in_mixed_case(state, config):
    from tools.privacy import note_request

    state.log_error("support", "reply from Jo@Acme.example about an order")
    state.log_action(1, None, "deliver", "ok", "sent to JO@ACME.EXAMPLE")
    done = forget(state, config, "jo@acme.example")
    assert done["log lines redacted"] == 2
    logs = str([dict(r) for r in state._all("SELECT message FROM errors")] + [dict(r) for r in state._all("SELECT detail FROM actions")])
    assert "acme.example" not in logs.lower() and "[deleted]" in logs
    assert note_request(state, "Pat@Co.example", "delete my data") and not note_request(state, "pat@co.example", "again")


def test_export_and_forget_cover_download_links(state, config):
    from tools import download_links

    config.public_webhook_url = "https://t.example/webhooks/stripe"
    download_links.issue(state, config, "assets/py/py-v1.zip", "Jo@Acme.example")
    assert export(state, config, "jo@acme.example")["download_links"][0]["path"] == "assets/py/py-v1.zip"
    assert forget(state, config, "jo@acme.example")["download links anonymised"] == 1
    assert "acme" not in str([dict(r) for r in state._all("SELECT email FROM download_tokens")])
