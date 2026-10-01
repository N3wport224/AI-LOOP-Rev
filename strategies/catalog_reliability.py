"""Catalog reliability (Phases 240-244): a catalog that runs for months without surprises.

* **Phase 240, current version after a price change:** an order completed on a factory product's
  previous checkout (a price rise closed that link while someone was paying) gets the current
  version of the dataset, not the replaced one (``StateStore.asset_for_product``).
* **Phase 241, no dead ends for old links:** for ``STUB_DAYS`` (90) after a product is retired,
  its address shows a short "no longer on sale" page that isn't indexed by search engines and
  points to the same technology's products and the catalog, instead of a "not found" page for
  people arriving from old posts and emails.
* **Phase 242, no pile-up without a checkout:** when ``STAGED_MAX`` (25) products are already
  waiting for a checkout (payments not set up yet, or Stripe refusing), the factory stops making
  more until they go on sale. Doctor shows why.
* **Phase 243, catalog integrity:** once an hour the factory checks every product on sale still
  has its download file. One that doesn't (a disk restore, a deleted folder) is rebuilt at the next
  refresh instead of failing a buyer, and you get one alert a day. If it still has no file an hour
  later (its postings are gone, so there's nothing to rebuild from), its checkout is closed and it's
  retired, so nobody pays for a download that can't be sent.
* **Phase 244, rehearsed end to end:** ``automonetize test-full-loop`` now also has the factory
  make a product and checks it reaches the site with a checkout.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta
from typing import Any

STUB_DAYS = 90
STAGED_MAX = 25
INTEGRITY_KEY = "factory_integrity_at"
MISSING_KEY = "factory_missing_files"


def take_off_sale(tools: Any, slug: str, reason: str = "") -> None:
    """Close the checkout and retire a product that can't be delivered."""
    state = tools.state
    row = state._one("SELECT asset_id FROM factory_products WHERE slug = ?", (slug,))
    asset = state.get_asset(int(row["asset_id"])) if row else None
    ref = str((asset or {}).get("product_ref") or "")
    if ref.startswith("plink_") and hasattr(tools.storefront, "deactivate"):
        tools.storefront.deactivate(ref)
    if asset:
        state.update_asset(int(asset["id"]), status="retired")
    state._exec("UPDATE factory_products SET status = 'retired', retired_at = ? WHERE slug = ?", (state.now(), slug))
    if reason:
        state.log_action(int(state.get("iteration", 0)), None, "product_retired", "ok", f"{slug}: {reason}")
    else:
        state.log_error("product_factory", f"{slug} was taken off sale: its download is missing and couldn't be rebuilt "
                                           "(its postings are gone).", kind="alert")


# ------------------------------------------------------------------ Phase 241
def retired_stubs(state: Any, built: set[str], shell: Any) -> dict[str, str]:
    """``shell(title, body, desc) -> html`` for a page one folder deep (noindex added here)."""
    from strategies.product_factory import ensure, label

    ensure(state)
    since = (state.clock() - timedelta(days=STUB_DAYS)).isoformat(timespec="seconds")
    live: dict[str, list[tuple[str, str]]] = {}
    for r in state._all("SELECT slug, title, filters FROM factory_products WHERE status = 'live' ORDER BY slug"):
        tech = json.loads(r["filters"]).get("tech") or ""
        live.setdefault(tech, []).append((r["slug"], r["title"]))
    out = {}
    for r in state._all("SELECT slug, title, filters FROM factory_products WHERE status = 'retired' AND retired_at >= ?",
                        (since,)):
        path = f"{r['slug']}/index.html"
        if path in built:
            continue
        tech = json.loads(r["filters"]).get("tech") or ""
        related = [(s, t) for s, t in live.get(tech, []) if f"{s}/index.html" in built][:5]
        links = "".join(f'<li><a href="../{html.escape(s)}/">{html.escape(t)}</a></li>' for s, t in related)
        more = ('<p><a href="../catalog/">See every dataset</a></p>' if "catalog/index.html" in built
                else '<p><a href="../">See every dataset</a></p>')
        body = (f"<h1>{html.escape(r['title'])}</h1><p>This dataset is no longer on sale.</p>"
                + (f"<h2>Current {html.escape(label(tech))} datasets</h2><ul>{links}</ul>" if links else "") + more)
        page = shell(f"{r['title']} (no longer on sale)", body, "This dataset is no longer on sale.")
        out[path] = page.replace("<head>", '<head><meta name="robots" content="noindex">', 1)
    return out


# ------------------------------------------------------------------ Phase 242
def too_many_waiting(state: Any) -> int:
    n = int(state._one("SELECT COUNT(*) AS n FROM factory_products WHERE status = 'staged'")["n"])
    return n if n >= STAGED_MAX else 0


# ------------------------------------------------------------------ Phase 243
def _verified(tools: Any, state: Any, path: str) -> bool:
    """Phase 274: checksums, for up to VERIFY_PER_HOUR downloads per check (the counter resets each hour)."""
    from strategies.download_extras import VERIFY_PER_HOUR, verify_zip

    done = int(state.get(_VERIFIED_KEY) or 0)
    if done >= VERIFY_PER_HOUR:
        return True
    state.set(_VERIFIED_KEY, done + 1)
    return verify_zip(tools.files.read_bytes(path))


_VERIFIED_KEY = "factory_verified_this_hour"



def check_integrity(tools: Any) -> list[str]:
    """Live products whose download is missing; each is queued for a rebuild. At most hourly."""
    state = tools.state
    last = state.get(INTEGRITY_KEY)
    if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=1):
        return []
    state.set(INTEGRITY_KEY, state.now())
    state.set(_VERIFIED_KEY, 0)
    missing = []
    for r in state._all("SELECT f.slug AS slug, a.path AS path FROM factory_products f JOIN assets a ON a.id = f.asset_id "
                        "WHERE f.status = 'live' ORDER BY RANDOM()"):  # a different sample of checksums each hour
        if not r["path"] or not tools.files.exists(r["path"]) or not _verified(tools, state, r["path"]):
            missing.append(r["slug"])
            state._exec("UPDATE factory_products SET refreshed_at = '1970-01-01T00:00:00+00:00' WHERE slug = ?", (r["slug"],))
    flagged = set(state.get(MISSING_KEY) or [])
    for slug in [s for s in missing if s in flagged]:  # still missing an hour after a rebuild: nothing to sell
        take_off_sale(tools, slug)
    state.set(MISSING_KEY, [s for s in missing if s not in flagged])
    if missing:
        from agent.ops_checks import _daily

        if _daily(state, "factory_integrity"):
            state.log_error("product_factory", f"{len(missing)} product(s) on sale had a missing or damaged download (e.g. {missing[0]}); "
                                               "they're being rebuilt now.", kind="alert")
    return missing
