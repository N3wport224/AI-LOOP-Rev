"""Product descriptions and preview images (Phases 401-405).

* **Phase 401, a description that sells:** ``checkout_description`` writes what a buyer sees on the
  Stripe checkout: what the file is and how big, who is in it, how fresh, the formats, and how it's
  delivered. Every number comes from the product's own data, and it fits Stripe's 500 characters.
* **Phase 402, three preview images per product:** ``previews`` draws
  1. a cover (title, postings, companies, share remote, price, formats);
  2. a sample of real rows (5 rows, the same public facts the free preview shows);
  3. a chart: the companies with the most open roles, or the top locations, or, for products
     without those, the fields in every row.
  They're written next to the product's files as SVG (always) and PNG (when Pillow is installed).
* **Phase 403, on the product page:** a gallery under the summary, and the first image becomes
  the page's social-media preview (``og:image``) and its structured-data image.
* **Phase 404, on the checkout:** once the site has published a product's PNGs, ``sync_stripe``
  sets them as the Stripe product's images (the checkout page shows them) along with the
  description. A few products per cycle, each once (again if its preview changes).
* **Phase 405, existing products too:** products made before this get their previews at the next
  site builds (``PER_BUILD`` new ones per build, so a large catalog is caught up over a few cycles).
"""

from __future__ import annotations

import hashlib
import html
import io
from typing import Any

PER_BUILD = 40          # new previews drawn per site build
PER_SYNC = 5            # Stripe products updated per cycle
STRIPE_KEY = "stripe_media"  # kv: {slug: digest of what Stripe has}
W, H = 1200, 750
NAMES = ("preview-1", "preview-2", "preview-3")
BG, INK, MUTED, ACCENT, ROW = "#101418", "#ffffff", "#b8c0c8", "#2e7d32", "#1b2228"
FONT = "DejaVu Sans,Verdana,Geneva,sans-serif"


# ------------------------------------------------------------------ Phase 401
def _n(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def facts(title: str, summary: str, insight: dict[str, Any] | None, rows: int, price_cents: int) -> dict[str, Any]:
    data = insight or {}
    return {"title": title, "summary": summary, "rows": _n(data.get("rows")) or _n(rows),
            "companies": _n(data.get("companies")), "remote_pct": _n(data.get("remote_pct")),
            "top_companies": [(str(c), _n(n)) for c, n in (data.get("top_companies") or [])][:5],
            "top_locations": [(str(p), _n(n)) for p, n in (data.get("top_locations") or [])][:4],
            "price": f"${price_cents / 100:,.2f}" if price_cents else ""}


def checkout_description(f: dict[str, Any], updated: str = "", refund_days: int = 0, limit: int = 500) -> str:
    """What the Stripe checkout says about the product: facts first, at most ``limit`` characters."""
    summary = " ".join(str(f["summary"] or "").split())
    if summary and not summary.endswith("."):
        summary += "."
    parts = []
    counted = f["rows"] and (str(f["rows"]) in summary or f"{f['rows']:,}" in summary)
    if f["rows"] and not counted:  # the factory's summaries already start with the count: say it once
        parts.append(f"{f['rows']:,} current job postings from {f['companies']:,} companies." if f["companies"]
                     else f"{f['rows']:,} records.")
    if summary:
        parts.append(summary)
    if f["top_companies"]:
        parts.append("Hiring most: " + ", ".join(c for c, _ in f["top_companies"][:3]) + ".")
    if f["remote_pct"]:
        parts.append(f"{f['remote_pct']}% remote.")
    parts.append("Every row links to its public posting; a README and a field guide are included." if "CSV" in summary
                 else "Every row links to its public posting. CSV, Excel, JSON and SQL with a README, emailed instantly.")
    if updated:
        parts.append(f"Updated {updated[:10]}.")
    if refund_days:
        parts.append(f"{refund_days}-day refund.")
    out = ""
    for p in parts:
        if len(out) + len(p) + 1 > limit:
            continue  # a long line is skipped, shorter ones after it may still fit
        out = f"{out} {p}".strip()
    return out[:limit]


# ------------------------------------------------------------------ Phase 402: drawing
def _wrap(text: str, chars: int, lines: int) -> list[str]:
    words, out = str(text).split(), [""]
    for w in words:
        if out[-1] and len(out[-1]) + 1 + len(w) > chars:
            if len(out) == lines:
                return out[:-1] + [_cut(out[-1] + " " + w + " …", chars)]
            out.append("")
        out[-1] = f"{out[-1]} {w}".strip()
    return out


def _cut(text: Any, chars: int) -> str:
    if isinstance(text, bool):
        return "Yes" if text else "No"
    if isinstance(text, list):
        text = ", ".join(str(t) for t in text[:3])
    text = " ".join(str(text if text is not None else "").split())
    return text if len(text) <= chars else text[: max(1, chars - 1)].rstrip() + "…"


def _layout(kind: int, f: dict[str, Any], fields: list[str], sample: list[dict[str, Any]]) -> list[tuple]:
    """Shapes shared by the SVG and PNG versions: ("text", x, y, size, colour, text, bold) and
    ("rect", x, y, w, h, colour)."""
    ops: list[tuple] = [("rect", 0, 0, W, H, BG), ("rect", 0, 0, 16, H, ACCENT)]
    if kind == 0:
        for i, line in enumerate(_wrap(f["title"], 38, 2)):
            ops.append(("text", 80, 120 + i * 60, 48, INK, line, True))
        stats = [(f"{f['rows']:,}", "postings")] if f["rows"] else []
        if f["companies"]:
            stats.append((f"{f['companies']:,}", "companies"))
        if f["remote_pct"]:
            stats.append((f"{f['remote_pct']}%", "remote"))
        for i, (big, small) in enumerate(stats[:3]):
            x = 80 + i * 340
            ops += [("rect", x, 230, 300, 170, ROW), ("text", x + 28, 320, 64, INK, big, True),
                    ("text", x + 28, 372, 28, MUTED, small, False)]
        ops.append(("text", 80, 500, 30, MUTED, "Every row links to its public job posting", False))
        ops.append(("text", 80, 560, 30, MUTED, "CSV · Excel · JSON · SQL · README", False))
        if f["price"]:
            ops += [("rect", 80, 610, 330, 80, ACCENT), ("text", 108, 665, 34, INK, f"{f['price']} · instant", True)]
        return ops
    if kind == 1:
        cols = [c for c in fields if c][:4] or ["company", "title"]
        ops.append(("text", 80, 95, 36, INK, f"Sample rows ({min(5, len(sample))} of {f['rows']:,})" if f["rows"]
                    else "Sample rows", True))
        weights = [1.8 if c in ("title", "company") else 1.0 for c in cols]  # names and titles need the room
        widths = [int((W - 160) * w / sum(weights)) for w in weights]
        xs = [96 + sum(widths[:j]) for j in range(len(cols))]
        ops.append(("rect", 80, 130, W - 160, 64, ROW))
        for j, c in enumerate(cols):
            ops.append(("text", xs[j], 172, 24, MUTED, _cut(c.replace("_", " ").title(), max(6, widths[j] // 15)), True))
        for i, r in enumerate(sample[:5]):
            y = 194 + i * 100
            if i % 2:
                ops.append(("rect", 80, y, W - 160, 100, ROW))
            for j, c in enumerate(cols):
                ops.append(("text", xs[j], y + 60, 24, INK, _cut(r.get(c), max(6, widths[j] // 15)), False))
        ops.append(("text", 80, 715, 22, MUTED, "Public facts from job postings: no personal contact details", False))
        return ops
    bars = f["top_companies"] or f["top_locations"]
    if bars:
        what = "Companies with the most open roles" if f["top_companies"] else "Top locations"
        ops.append(("text", 80, 95, 36, INK, what, True))
        top = max(n for _, n in bars) or 1
        for i, (name, n) in enumerate(bars[:5]):
            y = 150 + i * 110
            ops += [("text", 80, y + 30, 26, INK, _cut(name, 40), False),
                    ("rect", 80, y + 48, max(8, int((W - 300) * n / top)), 40, ACCENT),
                    ("text", 100 + max(8, int((W - 300) * n / top)), y + 78, 26, MUTED, str(n), True)]
        return ops
    ops.append(("text", 80, 95, 36, INK, "What's in every row", True))
    for i, c in enumerate([c for c in fields if c][:8]):
        ops.append(("text", 100, 170 + i * 66, 30, INK, "• " + c.replace("_", " "), False))
    return ops


def to_svg(ops: list[tuple], label: str) -> str:
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="{html.escape(label)}">']
    for op in ops:
        if op[0] == "rect":
            _, x, y, w, h, colour = op
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{0 if w == W or w == 16 else 10}" fill="{colour}"/>')
        else:
            _, x, y, size, colour, text, bold = op
            parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{colour}" font-family="{FONT}"'
                         f'{" font-weight=" + chr(34) + "700" + chr(34) if bold else ""}>{html.escape(text)}</text>')
    return "".join(parts) + "</svg>\n"


def to_png(ops: list[tuple]) -> bytes | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    from tools.seo_assets import _font

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    fonts: dict[int, Any] = {}
    for op in ops:
        if op[0] == "rect":
            _, x, y, w, h, colour = op
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=colour)
        else:
            _, x, y, size, colour, text, _ = op
            font = fonts.get(size) or fonts.setdefault(size, _font(size))
            draw.text((x, y - size), text, font=font, fill=colour)  # SVG y is the baseline; PIL's is the top
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def previews(f: dict[str, Any], fields: list[str], sample: list[dict[str, Any]], png: bool = True) -> dict[str, bytes]:
    """{"preview-1.svg": ..., "preview-1.png": ...} for the three images."""
    labels = (f"{f['title']}: overview", f"{f['title']}: sample rows", f"{f['title']}: chart")
    out: dict[str, bytes] = {}
    for i, name in enumerate(NAMES):
        ops = _layout(i, f, fields, sample)
        out[f"{name}.svg"] = to_svg(ops, labels[i]).encode()
        data = to_png(ops) if png else None
        if data:
            out[f"{name}.png"] = data
    return out


# ------------------------------------------------------------------ Phase 403/405: files for the site
def ensure(files: Any, base: str, f: dict[str, Any], fields: list[str], sample: list[dict[str, Any]],
           png: bool = True, budget: list[int] | None = None) -> dict[str, bytes]:
    """The product's previews (drawn once, kept next to its files). ``budget`` is a one-item counter
    of how many may still be drawn in this build; products over budget get theirs next build."""
    have = {f"{n}.{ext}" for n in NAMES for ext in ("svg", "png") if files.exists(f"{base}/{n}.{ext}")}
    if not {f"{n}.svg" for n in NAMES} <= have:
        if budget is not None:
            if budget[0] <= 0:
                return {}
            budget[0] -= 1
        for name, data in previews(f, fields, sample, png).items():
            files.write_bytes(f"{base}/{name}", data)
        have = {f"{n}.{ext}" for n in NAMES for ext in ("svg", "png") if files.exists(f"{base}/{n}.{ext}")}
    return {name: files.read_bytes(f"{base}/{name}") for name in sorted(have)}


def gallery_names(images: dict[str, bytes]) -> list[str]:
    """One file per image for the page: the PNG when there is one (it also works in social previews)."""
    return [f"{n}.png" if f"{n}.png" in images else f"{n}.svg" for n in NAMES if f"{n}.svg" in images or f"{n}.png" in images]


def gallery_html(page_title: str, names: list[str]) -> str:
    if not names:
        return ""
    alts = ("overview", "sample rows", "chart")
    imgs = "".join(f'<a href="{html.escape(n)}"><img src="{html.escape(n)}" alt="{html.escape(page_title)}: {alts[i]}" '
                   f'width="{W}" height="{H}" loading="{"eager" if i == 0 else "lazy"}"></a>' for i, n in enumerate(names[:3]))
    return f'<div class="gallery">{imgs}</div>'


# ------------------------------------------------------------------ Phase 404: the Stripe checkout
def _published(state: Any, cfg: Any, rel: str) -> bool:
    branch = cfg.github_pages_branch or cfg.github_branch
    prefix = f"{cfg.github_pages_dir.strip('/')}/" if cfg.github_pages_dir.strip("/") else ""
    manifest = state.get(f"site_manifest:{cfg.github_pages_repo}:{branch}:{prefix}") or {}
    return rel in manifest


def sync_stripe(tools: Any, limit: int = PER_SYNC) -> int:
    """Give live Stripe products their preview images and description, once the images are online."""
    from strategies.revenue_models import listed_products
    from tools.promo import Promos
    from tools.storefront.stripe_pages_publisher import STRIPE_API

    state, cfg = tools.state, tools.config
    base_url = cfg.pages_base_url.rstrip("/")
    if not (cfg.stripe_secret_key and base_url and cfg.github_pages_repo and getattr(cfg, "product_previews", True)):
        return 0
    done = dict(state.get(STRIPE_KEY) or {})
    client = Promos(tools.http, cfg.stripe_secret_key)
    changed = 0
    for a in listed_products(state):
        ref, slug = str(a.get("product_ref") or ""), str(a.get("niche") or "")
        if changed >= limit or not ref.startswith("plink_") or not slug:
            continue
        names = [f"{slug}/{n}.png" for n in NAMES]
        if not all(_published(state, cfg, n) for n in names):
            continue  # the checkout would show broken images until the site is up
        base = _asset_base(a)
        listing = tools.files.read_json(f"{base}/listing.json") if tools.files.exists(f"{base}/listing.json") else {}
        f = facts(a["title"], listing.get("summary", ""), listing.get("insight"), int(a.get("lead_count") or 0),
                  int(a.get("price_cents") or 0))
        desc = checkout_description(f, str(a.get("created_at") or ""), int(cfg.refund_policy_days or 0))
        images = [f"{base_url}/{n}" for n in names]
        digest = hashlib.sha256(("|".join(images) + desc + _png_digest(tools.files, base)).encode()).hexdigest()[:16]
        if done.get(slug) == digest:
            continue
        try:
            product = client.product_for_link(ref)
            if not product:
                continue
            tools.http.post(f"{STRIPE_API}/products/{product}", check_robots=False,
                            headers=client.client._headers(f"media-{product}-{digest}"),
                            data={**{f"images[{i}]": u for i, u in enumerate(images)}, "description": desc})
        except Exception as exc:  # noqa: BLE001 - the product keeps selling without images; retried next cycle
            state.log_error("product_media", f"couldn't update {slug} on Stripe: {exc!r}")
            break
        done[slug] = digest
        changed += 1
    if changed:
        state.set(STRIPE_KEY, done)
    return changed


def _asset_base(a: dict[str, Any]) -> str:
    kind = {"lead_directory": "dataset"}.get(a["kind"], a["kind"])
    return f"assets/{a['niche']}/v{a['version']}" if kind == "dataset" else f"assets/{a['niche']}/{kind}-v{a['version']}"


def _png_digest(files: Any, base: str) -> str:
    out = []
    for n in NAMES:
        rel = f"{base}/{n}.png"
        out.append(hashlib.sha256(files.read_bytes(rel)).hexdigest()[:8] if files.exists(rel) else "-")
    return "".join(out)
