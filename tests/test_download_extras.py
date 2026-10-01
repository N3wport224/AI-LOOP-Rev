"""Phases 270-274: SQLite, JSON Schema, licence, citation, checksums (and their integrity check)."""

import io
import json
import sqlite3
import zipfile

from strategies import download_extras as dx
from strategies import product_factory as pf
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product
from tests.test_ops_robustness import alerts

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def zipped(kit, state, made):
    return zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(state.get_asset(made["asset_id"])["path"])))


def test_every_download_carries_the_extras(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/d"
    made = made_product(kit, state, clock, transport)
    zf, slug = zipped(kit, state, made), made["slug"]
    names = {n.split("/", 1)[1] for n in zf.namelist()}
    assert set(dx.EXTRA_FILES) <= names
    # 270: the CSV as a typed SQLite table
    path = kit.files.resolve("tmp.sqlite")
    path.write_bytes(zf.read(f"{slug}/data.sqlite"))
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == made["rows"]
    assert conn.execute("SELECT company FROM leads LIMIT 1").fetchone()[0].startswith("Co ")
    conn.close()
    # 271
    schema = json.loads(zf.read(f"{slug}/schema.json"))
    assert schema["items"]["properties"]["company"]["type"] == "string" and "company" in schema["items"]["required"]
    # 272-273
    assert "may not resell" in zf.read(f"{slug}/LICENSE.txt").decode()
    cff = zf.read(f"{slug}/CITATION.cff").decode()
    assert 'type: dataset' in cff and 'url: "https://me.github.io/d/' in cff
    # 274
    sums = zf.read(f"{slug}/SHA256SUMS").decode()
    assert "  leads.csv\n" in sums and "SHA256SUMS" not in sums
    assert dx.verify_zip(kit.files.read_bytes(state.get_asset(made["asset_id"])["path"]))


def test_typed_columns():
    rows = [{"n": "1", "x": "1.5", "b": "True", "s": "a"}, {"n": "", "x": "2", "b": "False", "s": "b"}]
    assert [dx._kind([r[f] for r in rows]) for f in "nxbs"] == ["integer", "number", "boolean", "string"]
    schema = json.loads(dx.json_schema("T", rows, list("nxbs")))
    assert schema["items"]["properties"]["n"]["type"] == ["integer", "null"] and "n" not in schema["items"]["required"]


def test_a_corrupted_download_is_rebuilt(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    path = state.get_asset(made["asset_id"])["path"]
    data = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(path))) as src, zipfile.ZipFile(data, "w") as dst:
        for name in src.namelist():
            body = src.read(name)
            dst.writestr(name, body + b"tampered" if name.endswith("leads.csv") else body)
    kit.files.write_bytes(path, data.getvalue())
    assert not dx.verify_zip(data.getvalue())
    clock.advance(hours=2)
    assert made["slug"] in pf.tick(kit)["refreshed"]
    assert any("had a missing or damaged download" in a for a in alerts(state))
