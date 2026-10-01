"""Take the catalog with you (Phases 340-344).

Backups (``automonetize backup``) copy the whole database. This moves just the catalog, for a new
Mac or a second installation selling the same products:

* **Phase 340, export:** ``automonetize catalog-export [file]`` writes one zip: ``catalog.json`` (every
  factory product: title, filters, status, price, checkout link and Stripe reference) and, for each
  product on sale, its current download, listing and sample. No orders, buyers or emails: nothing
  about customers leaves.
* **Phase 341, checksums:** the bundle lists every file's SHA-256 in ``SHA256SUMS``.
* **Phase 342, import:** ``automonetize catalog-import <file>`` verifies the bundle, restores the files
  under ``assets/`` and adds the products that aren't there yet, keeping their checkout links (the same
  Stripe account keeps selling them). Products already present are left alone.
* **Phase 343, check first:** ``--check`` verifies and lists what would be imported, writing nothing.
* **Phase 344, safe to open:** a bundle with a path outside ``assets/`` (or ``..``), a bad checksum, a
  checkout link that isn't ``https``, or more than ``MAX_BYTES`` is refused before anything is written.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

VERSION = 1
MAX_BYTES = 2_000_000_000
SUMS = "SHA256SUMS"


def _asset_files(files: Any, slug: str, path: str) -> list[str]:
    out = [path] if path and files.exists(path) else []
    for name in ("listing.json", "sample.json"):
        rel = f"assets/{slug}/micro-v1/{name}"
        if files.exists(rel):
            out.append(rel)
    return out


# ------------------------------------------------------------------ Phases 340-341
def export(state: Any, files: Any) -> bytes:
    from strategies.product_factory import ensure

    ensure(state)
    products, paths = [], []
    for r in state._all("SELECT * FROM factory_products ORDER BY created_at"):
        a = state.get_asset(int(r["asset_id"])) if r["asset_id"] else None
        entry = {k: r[k] for k in ("slug", "title", "filters", "rows", "companies", "keys_sample", "status", "created_at",
                                   "published_at", "retired_at")}
        if a:
            entry["asset"] = {k: a.get(k) for k in ("title", "path", "version", "lead_count", "price_cents", "product_ref",
                                                    "status", "provider", "checkout_url", "kind_meta")}
            if r["status"] == "live":
                paths += _asset_files(files, r["slug"], a.get("path") or "")
        products.append(entry)
    body = {"version": VERSION, "exported_at": state.now(), "products": products}
    contents = {"catalog.json": json.dumps(body, indent=2).encode()}
    for rel in paths:
        contents[rel] = files.read_bytes(rel)
    contents[SUMS] = "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in sorted(contents.items())).encode()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, b in contents.items():
            zf.writestr(name, b)
    return data.getvalue()


# ------------------------------------------------------------------ Phases 342-344
def _safe(name: str) -> bool:
    return name in ("catalog.json", SUMS) or (name.startswith("assets/") and ".." not in name.split("/")
                                               and not name.startswith("/") and "\\" not in name)


def check(data: bytes) -> dict[str, Any]:
    """Verify a bundle. Raises ValueError with the reason when it can't be imported."""
    if len(data) > MAX_BYTES:
        raise ValueError("the bundle is too big")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("not a catalog bundle (not a zip file)") from None
    with zf:
        names = zf.namelist()
        bad = [n for n in names if not _safe(n)]
        if bad:
            raise ValueError(f"refused: unexpected path in the bundle ({bad[0]})")
        if "catalog.json" not in names or SUMS not in names:
            raise ValueError("not a catalog bundle (no catalog.json or SHA256SUMS)")
        if sum(i.file_size for i in zf.infolist()) > MAX_BYTES:
            raise ValueError("the bundle unpacks to too much data")
        listed = {}
        for line in zf.read(SUMS).decode().splitlines():
            digest, _, name = line.partition("  ")
            listed[name] = digest
        for name in names:
            if name != SUMS and hashlib.sha256(zf.read(name)).hexdigest() != listed.get(name):
                raise ValueError(f"checksum mismatch: {name}")
        body = json.loads(zf.read("catalog.json"))
    if body.get("version") != VERSION:
        raise ValueError(f"unsupported bundle version {body.get('version')!r}")
    return body


def import_bundle(state: Any, files: Any, data: bytes, dry_run: bool = False) -> dict[str, Any]:
    from strategies.product_factory import KIND, _hypothesis, ensure

    body = check(data)
    for p in body.get("products") or []:
        url = str((p.get("asset") or {}).get("checkout_url") or "")
        if url and not url.startswith("https://"):
            raise ValueError(f"refused: {p.get('slug')} has a checkout link that isn't https")
    ensure(state)
    existing = {r["slug"] for r in state._all("SELECT slug FROM factory_products")}
    new = [p for p in body["products"] if p["slug"] not in existing]
    report = {"products": len(body["products"]), "new": [p["slug"] for p in new], "skipped": len(body["products"]) - len(new)}
    if dry_run:
        return report
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.startswith("assets/") and not files.exists(name):
                files.write_bytes(name, zf.read(name))
    for p in new:
        a = p.get("asset") or {}
        aid = None
        if a:
            hid = _hypothesis(state, p["slug"], p["title"])
            aid = state.add_asset(hid, KIND, a.get("title") or p["title"], a.get("path") or "", int(a.get("version") or 1),
                                  int(a.get("lead_count") or 0), int(a.get("price_cents") or 0), product_ref=a.get("product_ref"))
            state.update_asset(aid, niche=p["slug"], status=a.get("status") or "staged", provider=a.get("provider") or "stripe",
                               checkout_url=a.get("checkout_url") or "", **({"kind_meta": a["kind_meta"]} if a.get("kind_meta") else {}))
        state._exec("INSERT INTO factory_products (slug, asset_id, title, filters, rows, companies, keys_sample, status, "
                    "created_at, published_at, retired_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (p["slug"], aid, p["title"], p["filters"], p["rows"], p["companies"], p["keys_sample"], p["status"],
                     p["created_at"], p.get("published_at"), p.get("retired_at")))
    return report
