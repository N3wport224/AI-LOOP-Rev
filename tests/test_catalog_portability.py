"""Phases 340-344: export, checksums, import, check first, safe to open."""

import io
import json
import zipfile

import pytest

from strategies import catalog_portability as cp
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product, sell
from tools.file_io import SandboxedFileIO

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_export_has_the_catalog_and_no_customers(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    sell(state, made["asset_id"], 1)
    data = cp.export(state, kit.files)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    assert "catalog.json" in names and "SHA256SUMS" in names and state.get_asset(made["asset_id"])["path"] in names
    body = json.loads(zf.read("catalog.json"))
    assert body["products"][0]["slug"] == made["slug"] and body["products"][0]["asset"]["checkout_url"]
    assert b"@co.example" not in data  # no buyer emails anywhere


def test_import_into_a_fresh_install(kit, state, clock, transport, tmp_path):
    from agent.state import StateStore

    made = made_product(kit, state, clock, transport)
    data = cp.export(state, kit.files)
    fresh = StateStore(tmp_path / "fresh.db", clock=clock)
    files = SandboxedFileIO(tmp_path / "other-mac")
    assert cp.import_bundle(fresh, files, data, dry_run=True) == {"products": 1, "new": [made["slug"]], "skipped": 0}
    assert not files.exists(state.get_asset(made["asset_id"])["path"])  # --check writes nothing
    assert cp.import_bundle(fresh, files, data)["new"] == [made["slug"]]
    row = fresh._one("SELECT * FROM factory_products WHERE slug = ?", (made["slug"],))
    asset = fresh.get_asset(row["asset_id"])
    assert asset["checkout_url"] == state.get_asset(made["asset_id"])["checkout_url"] and files.exists(asset["path"])
    assert cp.import_bundle(fresh, files, data)["skipped"] == 1  # twice: nothing duplicated
    fresh.close()


def bundle(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for n, b in entries.items():
            zf.writestr(n, b)
    return out.getvalue()


def test_unsafe_or_damaged_bundles_are_refused():
    import hashlib

    good = {"catalog.json": json.dumps({"version": 1, "products": []}).encode()}
    sums = "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in good.items()).encode()
    assert cp.check(bundle({**good, "SHA256SUMS": sums}))["products"] == []
    with pytest.raises(ValueError, match="unexpected path"):
        cp.check(bundle({**good, "SHA256SUMS": sums, "../evil.py": b"x"}))
    with pytest.raises(ValueError, match="unexpected path"):
        cp.check(bundle({**good, "SHA256SUMS": sums, "assets/../../x": b"x"}))
    with pytest.raises(ValueError, match="checksum mismatch"):
        cp.check(bundle({"catalog.json": b'{"version": 1, "products": [1]}', "SHA256SUMS": sums}))
    with pytest.raises(ValueError, match="not a zip"):
        cp.check(b"nope")


def test_cli_parses():
    from dashboard.cli import build_parser

    args = build_parser().parse_args(["catalog-import", "x.zip", "--check"])
    assert args.file == "x.zip" and args.check
