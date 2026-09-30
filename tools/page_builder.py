"""Static site generator for inbound traffic: product landers, index, sitemap and RSS feed.

Each dataset gets ``<niche>/index.html`` with:

* ``<title>``, meta description, canonical URL and Open Graph tags;
* a ``schema.org/Product`` JSON-LD block (name, description, sku, brand, ``Offer`` with price,
  currency, availability and checkout URL);
* urgency metrics from the tech radar (companies, high-urgency count, top intent signals);
* the 5-record sanitized sample table and the Stripe Payment Link CTA.

Site-wide: ``index.html``, ``sitemap.xml``, ``robots.txt`` and ``feeds/radar.xml`` (RSS 2.0).
Everything is written under ``data/site/`` and, when configured, committed to the GitHub Pages
branch/dir (``github_pages_branch`` / ``github_pages_dir``) through the contents API.
"""

from __future__ import annotations

import html
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
    from tools.storefront.github import GitHubClient

FEED_PATH = "feeds/radar.xml"


@dataclass
class ProductPage:
    niche: str
    title: str
    summary: str
    price_cents: int
    currency: str
    checkout_url: str
    sample_columns: list[str]
    sample_rows: list[dict[str, Any]]
    metrics: dict[str, Any] = field(default_factory=dict)  # companies, high_urgency, signals, roles
    updated_at: str = ""
    sku: str = ""
    kind: str = "dataset"  # dataset | bundle | premium

    @property
    def slug(self) -> str:
        return self.niche if self.kind == "dataset" else f"{self.niche}-{self.kind}"


def _cell(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value[:6])
    return str(value if value is not None else "")


def page_url(base_url: str, slug: str) -> str:
    path = f"{slug.strip('/')}/" if slug.strip("/") else ""
    return f"{base_url.rstrip('/')}/{path}" if base_url else (path or "./")


def product_jsonld(page: ProductPage, url: str, brand: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": page.title,
        "description": page.summary,
        "sku": page.sku or page.slug,
        "category": "Business Data / B2B Intelligence",
        "brand": {"@type": "Brand", "name": brand},
        "offers": {
            "@type": "Offer",
            "price": f"{page.price_cents / 100:.2f}",
            "priceCurrency": page.currency.upper(),
            "availability": "https://schema.org/InStock" if page.checkout_url else "https://schema.org/PreOrder",
            "url": page.checkout_url or url,
        },
    }
    if url.startswith("http"):
        data["url"] = url
    if page.updated_at:
        data["offers"]["priceValidUntil"] = page.updated_at[:4] + "-12-31"
    return data


def _jsonld_script(data: dict[str, Any]) -> str:
    # Escape <, > and & as JSON unicode escapes: "</script>" can't end the block early and
    # "<!--<script" can't trigger the HTML parser's double-escaped script state. Still valid JSON.
    text = json.dumps(data, indent=2, ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


CSS = """body{font-family:system-ui,-apple-system,sans-serif;max-width:940px;margin:0 auto;padding:2rem 1rem;line-height:1.55;color:#111;background:#fff}
h1{font-size:1.9rem;margin-bottom:.3rem}.lede{color:#444;font-size:1.1rem}
.kpis{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}.kpi{border:1px solid #e3e3e3;border-radius:8px;padding:.6rem .9rem;min-width:9rem}
.kpi b{display:block;font-size:1.4rem}.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.92rem}
td,th{border-bottom:1px solid #e3e3e3;padding:.45rem;text-align:left;vertical-align:top}th{text-transform:capitalize}
.cta{display:inline-block;background:#111;color:#fff;padding:.8rem 1.2rem;border-radius:8px;text-decoration:none;font-weight:600;margin:1rem 0}
.muted{color:#666;font-size:.9rem}a{color:inherit}
@media (prefers-color-scheme: dark){body{background:#111;color:#eee}.lede{color:#bbb}td,th,.kpi{border-color:#333}.cta{background:#eee;color:#111}.muted{color:#999}}"""


def render_product_page(page: ProductPage, base_url: str = "", brand: str = "Tech Stack Intel") -> str:
    url = page_url(base_url, page.slug)
    title = html.escape(page.title)
    desc = html.escape(page.summary[:300])
    price = f"${page.price_cents / 100:.2f}"
    m = page.metrics
    kpis = [
        ("Companies", m.get("companies")),
        ("High-urgency", m.get("high_urgency")),
        ("Open roles", m.get("roles")),
        ("Verified careers pages", m.get("verified_urls")),
    ]
    kpi_html = "".join(f'<div class="kpi"><b>{html.escape(str(v))}</b>{html.escape(k)}</div>' for k, v in kpis if v is not None)
    signals = m.get("signals") or []
    signals_html = (
        "<p><strong>Top buying signals:</strong> " + html.escape(", ".join(f"{s} ({n})" for s, n in signals[:5])) + "</p>"
        if signals else ""
    )
    head_cells = "".join(f"<th>{html.escape(c.replace('_', ' '))}</th>" for c in page.sample_columns)
    rows = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(_cell(r.get(c)))}</td>" for c in page.sample_columns) + "</tr>"
        for r in page.sample_rows
    )
    cta = (
        f'<a class="cta" href="{html.escape(page.checkout_url)}" rel="noopener">Buy the full dataset: {price}</a>'
        if page.checkout_url else "<p><em>Checkout opens soon.</em></p>"
    )
    canonical = f'<link rel="canonical" href="{html.escape(url)}">' if url.startswith("http") else ""
    feed_href = f"{base_url.rstrip('/')}/{FEED_PATH}" if base_url else f"../{FEED_PATH}"
    feed = f'<link rel="alternate" type="application/rss+xml" title="Tech Radar" href="{html.escape(feed_href)}">'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
{canonical}
{feed}
<meta property="og:type" content="product"><meta property="og:title" content="{title}"><meta property="og:description" content="{desc}">
{f'<meta property="og:url" content="{html.escape(url)}">' if url.startswith("http") else ""}
<meta property="product:price:amount" content="{page.price_cents / 100:.2f}"><meta property="product:price:currency" content="{html.escape(page.currency.upper())}">
<script type="application/ld+json">
{_jsonld_script(product_jsonld(page, url, brand))}
</script>
<style>{CSS}</style></head><body>
<h1>{title}</h1>
<p class="lede">{html.escape(page.summary)}</p>
<div class="kpis">{kpi_html}</div>
{signals_html}
{cta}
<h2>Free 5-record preview</h2>
<div class="wrap"><table><thead><tr>{head_cells}</tr></thead><tbody>
{rows}
</tbody></table></div>
<p class="muted">Built from public job-board APIs; every record links to its source. Delivered instantly by email as CSV + JSON + an executive summary.{f" Updated {html.escape(page.updated_at[:10])}." if page.updated_at else ""}</p>
<p class="muted"><a href="../">All datasets</a> · <a href="../feeds/radar.xml">RSS</a></p>
</body></html>
"""


def render_index(pages: list[ProductPage], base_url: str, site_title: str) -> str:
    items = "\n".join(
        f'<li><a href="{html.escape(p.slug)}/">{html.escape(p.title)}</a>: {html.escape(p.summary[:160])} '
        f"(${p.price_cents / 100:.2f})</li>"
        for p in pages
    )
    canonical = f'<link rel="canonical" href="{html.escape(base_url.rstrip("/") + "/")}">' if base_url else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(site_title)}</title><meta name="description" content="Company-level tech stack and hiring-intent datasets.">
{canonical}<link rel="alternate" type="application/rss+xml" title="Tech Radar" href="feeds/radar.xml">
<style>{CSS}</style></head><body>
<h1>{html.escape(site_title)}</h1>
<p class="lede">Who is hiring, what they run, and who is about to buy: company-level tech stack intelligence, refreshed continuously.</p>
<ul>
{items}
</ul>
<p class="muted"><a href="feeds/radar.xml">Subscribe via RSS</a></p>
</body></html>
"""


def render_sitemap(pages: list[ProductPage], base_url: str) -> str:
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    urlset = ET.Element("urlset", xmlns=ns)
    for loc, lastmod in [(page_url(base_url, ""), None)] + [(page_url(base_url, p.slug), p.updated_at) for p in pages]:
        u = ET.SubElement(urlset, "url")
        ET.SubElement(u, "loc").text = loc
        if lastmod:
            ET.SubElement(u, "lastmod").text = lastmod[:10]
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(urlset, encoding="unicode") + "\n"


@dataclass
class FeedItem:
    title: str
    link: str
    description: str
    guid: str
    published: str  # ISO timestamp
    body_html: str = ""


def render_rss(items: list[FeedItem], base_url: str, site_title: str, now: datetime) -> str:
    """RSS 2.0 with atom:self link, built with ElementTree so it is always well-formed and escaped."""
    atom = "http://www.w3.org/2005/Atom"
    content_ns = "http://purl.org/rss/1.0/modules/content/"
    ET.register_namespace("atom", atom)
    ET.register_namespace("content", content_ns)
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    home = page_url(base_url, "") if base_url else "https://example.invalid/"
    ET.SubElement(ch, "title").text = f"{site_title}: Weekly Tech Radar"
    ET.SubElement(ch, "link").text = home
    ET.SubElement(ch, "description").text = "Data-driven summaries of who is hiring, what they run and who is migrating."
    ET.SubElement(ch, "language").text = "en"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now.astimezone(timezone.utc))
    ET.SubElement(ch, f"{{{atom}}}link", href=home.rstrip("/") + "/" + FEED_PATH, rel="self", type="application/rss+xml")
    for it in sorted(items, key=lambda i: i.published, reverse=True)[:50]:
        el = ET.SubElement(ch, "item")
        ET.SubElement(el, "title").text = it.title
        ET.SubElement(el, "link").text = it.link
        ET.SubElement(el, "guid", isPermaLink="false").text = it.guid
        ET.SubElement(el, "pubDate").text = format_datetime(datetime.fromisoformat(it.published).astimezone(timezone.utc))
        ET.SubElement(el, "description").text = it.description
        if it.body_html:
            ET.SubElement(el, f"{{{content_ns}}}encoded").text = it.body_html
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"


class SiteBuilder:
    """Writes the whole site to ``data/site`` and mirrors it to GitHub Pages when configured."""

    def __init__(self, config: "Config", files, github: "GitHubClient | None" = None):
        self.config = config
        self.files = files
        self.github = github

    @property
    def base_url(self) -> str:
        return self.config.pages_base_url.rstrip("/")

    def build(self, pages: list[ProductPage], feed_items: list[FeedItem], now: datetime) -> dict[str, str]:
        cfg = self.config
        out: dict[str, str] = {}
        for p in pages:
            out[f"{p.slug}/index.html"] = render_product_page(p, self.base_url, cfg.site_title)
        out["index.html"] = render_index(pages, self.base_url, cfg.site_title)
        out["sitemap.xml"] = render_sitemap(pages, self.base_url)
        out["robots.txt"] = "User-agent: *\nAllow: /\n" + (f"Sitemap: {self.base_url}/sitemap.xml\n" if self.base_url else "")
        out[FEED_PATH] = render_rss(feed_items, self.base_url, cfg.site_title, now)
        for rel, content in out.items():
            self.files.write_text(f"site/{rel}", content)
        return out

    def publish(self, out: dict[str, str]) -> int:
        """Commit changed files to the Pages branch/dir. Returns how many files changed."""
        cfg = self.config
        if not (self.github and self.github.configured() and cfg.github_pages_repo):
            return 0
        branch = cfg.github_pages_branch or cfg.github_branch
        prefix = f"{cfg.github_pages_dir.strip('/')}/" if cfg.github_pages_dir.strip("/") else ""
        changed = 0
        for rel, content in sorted(out.items()):
            res = self.github.put_file(cfg.github_pages_repo, prefix + rel, content, f"site: update {rel}", branch)
            changed += bool(res.get("changed"))
        return changed
