"""More places to sell (Phases 165-169).

* **Phase 165, marketplace kits:** ``automonetize marketplace-export`` writes, for every live
  product, a ready-to-upload folder for Gumroad, Lemon Squeezy or a data marketplace (the zip, a
  listing text with title, description, price and tags, the preview) plus ``index.csv``. You
  upload them under your own accounts: the agent never signs up anywhere in your name.
* **Phase 166, affiliates:** anyone can apply from ``affiliates/`` on the site; you approve with
  ``automonetize affiliate add <email>``, which emails them their links (``?utm_source=aff&
  utm_campaign=<code>``: the product pages carry it into checkout). Sales they bring earn
  ``affiliate_rate`` (30%) of the price; ``automonetize affiliate`` shows what you owe and
  ``affiliate paid <code>`` records a payout (you pay them yourself; refunds are excluded).
* **Phase 167, public catalog:** ``GET /v1/catalog`` (through the tunnel) lists every product with
  price and link as JSON, so tools, aggregators and AI assistants can find them.
* **Phase 168, free teaser data on GitHub:** with ``github_samples_repo`` set, each product gets
  a 10-row teaser CSV (no contact details) in that public repo, with a README linking to the full
  products. Up to 10 new teasers per cycle.
* **Phase 169, product leaderboard:** ``automonetize products`` ranks every product by revenue,
  orders and last sale, and flags the ones that never sold.
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
from datetime import timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

AFFILIATES = "affiliates"
TEASERS = "github_teasers"
TEASER_ROWS = 10
TEASERS_PER_CYCLE = 10
TEASER_FIELDS = ["company", "title", "location", "remote", "seniority", "stack", "posted_at", "url"]


def _live(state: Any) -> list[dict[str, Any]]:
    from strategies.revenue_models import live_products

    return [a for a in live_products(state) if a.get("checkout_url")]


def product_url(cfg: Any, asset: dict[str, Any]) -> str:
    base = str(cfg.pages_base_url or "").rstrip("/")
    return f"{base}/{asset['niche']}/" if base and asset.get("niche") else str(asset.get("checkout_url") or "")


# ------------------------------------------------------------------ Phase 165
def marketplace_export(state: Any, cfg: Any, files: Any) -> tuple[str, int]:
    rows = []
    for a in _live(state):
        slug = str(a["niche"])
        base = f"exports/marketplace/{slug}"
        listing_path = next((p for p in (f"assets/{slug}/{a['kind']}-v{a['version']}/listing.json",
                                         f"assets/{slug}/v{a['version']}/listing.json") if files.exists(p)), "")
        listing = files.read_json(listing_path) if listing_path else {}
        description = listing.get("description_markdown") or listing.get("summary") or a["title"]
        tags = sorted({w for w in re.findall(r"[a-z+#.]{2,}", a["title"].lower()) if w not in {"companies", "hiring", "engineers",
                                                                                               "the", "for", "and", "in"}})[:8]
        text = (f"Title: {a['title']}\nPrice: ${int(a['price_cents']) / 100:.2f}\nTags: {', '.join(tags)}\n\n"
                f"{listing.get('summary', '')}\n\n{description}\n\nMore and fresher data: {product_url(cfg, a)}\n")
        files.write_text(f"{base}/listing.txt", text)
        if a.get("path") and files.exists(a["path"]):
            files.write_bytes(f"{base}/{a['path'].rsplit('/', 1)[-1]}", files.read_bytes(a["path"]))
        rows.append({"slug": slug, "title": a["title"], "price_usd": f"{int(a['price_cents']) / 100:.2f}",
                     "tags": " ".join(tags), "folder": base})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["slug", "title", "price_usd", "tags", "folder"])
    w.writeheader()
    w.writerows(rows)
    files.write_text("exports/marketplace/index.csv", buf.getvalue())
    return str(files.resolve("exports/marketplace")), len(rows)


# ------------------------------------------------------------------ Phase 166
def add_affiliate(state: Any, email: str) -> dict[str, Any]:
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ValueError(f"not an email address: {email!r}")
    affs = dict(state.get(AFFILIATES) or {})
    for code, a in affs.items():
        if a["email"] == email:
            return {"code": code, **a}
    code = secrets.token_hex(3)
    affs[code] = {"email": email, "added_at": state.now(), "paid_cents": 0}
    state.set(AFFILIATES, affs)
    return {"code": code, **affs[code]}


def affiliate_link(cfg: Any, url: str, code: str) -> str:
    from tools.attribution import _with_params

    return _with_params(url, {"utm_source": "aff", "utm_medium": "affiliate", "utm_campaign": code}, overwrite=True)


def commissions(state: Any, cfg: Any) -> list[dict[str, Any]]:
    rate = float(cfg.affiliate_rate)
    out = []
    for code, a in (state.get(AFFILIATES) or {}).items():
        row = state._one("SELECT COUNT(*) AS n, COALESCE(SUM(gross_cents), 0) AS gross FROM orders WHERE channel = 'aff' "
                         "AND campaign = ? AND status NOT IN ('refunded', 'disputed')", (code,))
        earned = round(int(row["gross"]) * rate)
        out.append({"code": code, "email": a["email"], "orders": int(row["n"]), "earned_cents": earned,
                    "paid_cents": int(a.get("paid_cents", 0)), "owed_cents": max(0, earned - int(a.get("paid_cents", 0)))})
    return sorted(out, key=lambda c: -c["owed_cents"])


def mark_paid(state: Any, cfg: Any, code: str) -> int:
    affs = dict(state.get(AFFILIATES) or {})
    if code not in affs:
        raise ValueError(f"no affiliate {code}")
    owed = next(c["owed_cents"] for c in commissions(state, cfg) if c["code"] == code)
    affs[code]["paid_cents"] = int(affs[code].get("paid_cents", 0)) + owed
    state.set(AFFILIATES, affs)
    return owed


def welcome_email(state: Any, cfg: Any, aff: dict[str, Any]) -> Any:
    from tools.dispatcher import Email

    base = str(cfg.pages_base_url or "").rstrip("/") + "/"
    lines = ["Hi,", "", f"You're in: you earn {float(cfg.affiliate_rate) * 100:.0f}% of every sale your links bring (refunds "
             "excluded). Your code is " + aff["code"] + ".", "", "Your link to the whole catalog:", affiliate_link(cfg, base, aff["code"]),
             "", "Best sellers to start with:"]
    for a in _live(state)[:5]:
        lines.append(f"- {a['title']}: {affiliate_link(cfg, product_url(cfg, a), aff['code'])}")
    lines += ["", "Payouts are monthly, by the method you tell me. Reply with any questions.", "", cfg.sender_name or "AutoMonetize"]
    return Email(to=aff["email"], subject="Your affiliate links", body="\n".join(lines), kind="delivery")


def affiliates_page(cfg: Any, shell: Any) -> str:
    import html

    contact = html.escape(cfg.sender_email or cfg.owner_email or "")
    apply = (f'<p><a class="cta" href="mailto:{contact}?subject=Affiliate%20application">Apply by email</a></p>' if contact
             else "<p>Applications open soon.</p>")
    body = (f"<h1>Affiliate program</h1><p>Recommend our hiring datasets and earn <b>{float(cfg.affiliate_rate) * 100:.0f}%</b> "
            "of every sale your link brings. Refunded orders don't count; payouts are monthly.</p><ul><li>Tell us where you'd "
            "share the links (newsletter, blog, community).</li><li>Once approved you get your links by email.</li></ul>" + apply)
    return shell("Affiliate program", body, "Earn a commission recommending hiring datasets: apply by email, get your links, "
                                            "get paid monthly.")


# ------------------------------------------------------------------ Phase 167
def catalog_json(state: Any, cfg: Any) -> list[dict[str, Any]]:
    return [{"title": a["title"], "price_usd": round(int(a["price_cents"]) / 100, 2), "url": product_url(cfg, a),
             "checkout_url": a["checkout_url"], "rows": int(a.get("lead_count") or 0), "updated": str(a["created_at"])[:10]}
            for a in _live(state)]


# ------------------------------------------------------------------ Phase 168
def teaser_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TEASER_FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in rows[:TEASER_ROWS]:
        w.writerow({f: ", ".join(map(str, r[f])) if isinstance(r.get(f), list) else r.get(f) for f in TEASER_FIELDS})
    return buf.getvalue()


def teaser_rows(files: Any, asset: dict[str, Any]) -> list[dict[str, Any]]:
    import zipfile

    if not asset.get("path") or not files.exists(asset["path"]):
        return []
    with zipfile.ZipFile(io.BytesIO(files.read_bytes(asset["path"]))) as zf:
        name = next((n for n in zf.namelist() if n.endswith("leads.json")), "")
        return json.loads(zf.read(name)) if name else []


class SalesChannels(Strategy):
    name = "sales_channels"
    tasks = ("publish_teasers",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg, state = tools.config, tools.state
        repo = cfg.github_samples_repo
        if not repo or not tools.github.configured():
            return TaskResult(True, "free teasers off (set github_samples_repo)", {"published": 0})
        done = dict(state.get(TEASERS) or {})
        published = []
        for a in _live(state):
            if len(published) >= TEASERS_PER_CYCLE:
                break
            slug = str(a["niche"])
            if done.get(slug) == a["id"]:
                continue
            rows = teaser_rows(tools.files, a)
            if not rows:
                done[slug] = a["id"]
                continue
            tools.github.put_file(repo, f"data/{slug}.csv", teaser_csv(rows), f"teaser: {a['title']}", cfg.github_branch)
            done[slug] = a["id"]
            published.append(slug)
        if published:
            lines = ["# Free hiring data", "", "10-row teasers of current hiring datasets (no contact details). The full, fresh "
                     "datasets are linked below.", "", "| Dataset | Teaser | Full dataset |", "|---|---|---|"]
            for a in _live(state):
                if str(a["niche"]) in done:
                    lines.append(f"| {a['title']} | [CSV](data/{a['niche']}.csv) | "
                                 f"[${int(a['price_cents']) / 100:.2f}]({product_url(cfg, a)}?utm_source=github&utm_medium=teaser) |")
            tools.github.put_file(repo, "README.md", "\n".join(lines) + "\n", "teasers: index", cfg.github_branch)
        state.set(TEASERS, done)
        return TaskResult(True, f"teasers: {len(published)} published", {"published": len(published)})


# ------------------------------------------------------------------ Phase 169
def leaderboard(state: Any, days: int = 90) -> list[dict[str, Any]]:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for a in _live(state):
        row = state._one("SELECT COUNT(*) AS n, COALESCE(SUM(o.gross_cents), 0) AS gross, MAX(o.occurred_at) AS last FROM orders o "
                         "JOIN assets x ON x.id = o.asset_id WHERE x.niche = ? AND o.occurred_at >= ? "
                         "AND o.status NOT IN ('refunded', 'disputed')", (a["niche"], since))
        first = state._one("SELECT MIN(created_at) AS t FROM assets WHERE niche = ?", (a["niche"],))["t"]
        out.append({"title": a["title"], "niche": a["niche"], "orders": int(row["n"]), "revenue_cents": int(row["gross"]),
                    "last_sale": str(row["last"] or "")[:10], "since": str(first or "")[:10], "never_sold": not int(row["n"])})
    return sorted(out, key=lambda r: (-r["revenue_cents"], -r["orders"], r["title"]))
