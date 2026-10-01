"""Static site generator for inbound traffic: product landers, index, sitemap and RSS feed.

Each dataset gets ``<niche>/index.html`` with:

* ``<title>``, meta description, canonical URL and Open Graph tags;
* a ``schema.org/Product`` JSON-LD block (name, description, sku, brand, ``Offer`` with price,
  currency, availability and checkout URL);
* urgency metrics from the tech radar (companies, high-urgency count, top intent signals);
* the 5-record sanitized sample table and the Stripe Payment Link CTA.

**Search-intent matrix pages** (``intel/``): for every technology with enough companies behind
it, ``intel/companies-hiring-<tech>-engineers.html`` and, where companies are migrating to or
from it, ``intel/<tech>-infrastructure-migrations.html``. Each has hiring-velocity stats, the
intent mix, a sanitized 5-record preview, ``schema.org/Dataset`` JSON-LD, the Stripe Payment Link
and the free-sample form. Technologies below ``seo_min_companies`` get no page: thin, templated
pages hurt a site's standing more than they help.

Site-wide: ``index.html``, ``intel/index.html``, ``sitemap.xml`` (every lander and matrix page),
``robots.txt`` (points at the sitemap, which is how Google discovers it now that its ping
endpoint is retired), ``feeds/radar.xml`` (RSS 2.0) and the IndexNow key file.
Everything is written under ``data/site/`` and, when configured, committed to the GitHub Pages
branch/dir (``github_pages_branch`` / ``github_pages_dir``) through the contents API.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any

from tools.attribution import FIRST_TOUCH_DAYS, STORAGE_KEY
from tools.attribution import LANDER_ATTRIBUTION_JS as ATTRIBUTION_JS
from tools.seo_assets import badge_for, render_badge_svg, render_og_png, render_og_svg
from tools.site_extras import POPULAR_BADGE, banner_html, faq_html, footer_links, trust_html

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
    from tools.storefront.github import GitHubClient

FEED_PATH = "feeds/radar.xml"
STALE_DELETES_PER_RUN = 25

# "Updated 2 hours ago", computed in the visitor's browser so a static page never shows a stale
# relative time. Without JS the absolute timestamp stays visible.
RELATIVE_TIME_JS = """document.querySelectorAll('time.ago').forEach(function(t){var s=(Date.now()-Date.parse(t.getAttribute('datetime')))/1e3;
if(!(s>=0))return;var u=[[86400,'day'],[3600,'hour'],[60,'minute']];for(var i=0;i<u.length;i++){var n=Math.floor(s/u[i][0]);
if(n>=1){t.textContent=n+' '+u[i][1]+(n>1?'s':'')+' ago';return;}}t.textContent='just now';});"""


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
    subscription_url: str = ""
    subscription_price_cents: int = 0
    subscription_interval: str = "month"
    # Social proof, all computed from real data; a zero/empty field is simply not shown.
    data_updated_at: str = ""      # newest lead refresh (ISO)
    profiles_added_7d: int = 0     # companies first seen in the last 7 days
    verified_profiles: int = 0     # companies with a live-verified careers page
    purchases_7d: int = 0
    lead_capture_url: str = ""     # https://<tunnel>/lead-magnet/capture when the free sample is on
    # Copy bandit (tools/copy_bandit.py): {slot: {variant: {"text", "w"}}}, empty = static copy
    copy_arms: dict[str, Any] = field(default_factory=dict)
    copy_version: int = 0
    copy_targets: dict[str, str] = field(default_factory=dict)   # cta variant -> href
    telemetry_url: str = ""
    testimonials: list[str] = field(default_factory=list)  # approved customer quotes (strategies/testimonials.py)
    banner: str = ""               # sale / launch offer line (tools/site_extras.py)
    faq: list[tuple[str, str]] = field(default_factory=list)
    team_url: str = ""             # team license (strategies/plans.py)
    team_price_cents: int = 0
    team_seats: int = 0
    annual_url: str = ""           # yearly subscription
    annual_price_cents: int = 0
    versions: list[dict[str, Any]] = field(default_factory=list)  # [{version, date, rows}] newest first (changelog page)
    refund_days: int = 0           # trust row under the buy buttons (0 = no refund mention)
    related_html: str = ""         # "Often bought together" (strategies/upsells.py)
    insight_html: str = ""         # this product's own numbers (tools/seo_scale.py)
    extra_jsonld: str = ""         # breadcrumbs / Dataset markup (tools/seo_scale.py)
    hub_href: str = ""             # "More <tech> datasets" (relative)
    hub_label: str = ""
    ratings_html: str = ""         # verified-buyer ratings, shown from 3 ratings (strategies/buyer_experience.py)
    aggregate_rating: dict[str, Any] | None = None
    files_html: str = ""           # what's in the download
    popular: bool = False          # "Most popular" badge on the home and pricing pages

    @property
    def slug(self) -> str:
        return self.niche if self.kind in ("dataset", "micro") else f"{self.niche}-{self.kind}"


def rel_depth(title: str) -> int:
    """Folder depth of a legal page by its title: contact/ is one level down, legal/<x>/ two."""
    return 1 if title == "Contact" else 2


def meta_description(text: str, limit: int = 158) -> str:
    """Search engines show about 160 characters: cut at a word boundary instead of mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:.-") + "…"


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
        data["image"] = f"{url}og.png"
    if page.updated_at:
        data["offers"]["priceValidUntil"] = page.updated_at[:4] + "-12-31"
    if page.aggregate_rating:
        data["aggregateRating"] = page.aggregate_rating
    if page.subscription_url and page.subscription_price_cents:
        sub_offer = {
            "@type": "Offer",
            "name": f"{page.subscription_interval.title()}ly updates",
            "price": f"{page.subscription_price_cents / 100:.2f}",
            "priceCurrency": page.currency.upper(),
            "availability": "https://schema.org/InStock",
            "url": page.subscription_url,
            "priceSpecification": {
                "@type": "UnitPriceSpecification",
                "price": f"{page.subscription_price_cents / 100:.2f}",
                "priceCurrency": page.currency.upper(),
                "billingDuration": {"week": "P1W", "month": "P1M", "year": "P1Y"}.get(page.subscription_interval, "P1M"),
                "unitText": page.subscription_interval,
            },
        }
        data["offers"] = [data["offers"], sub_offer]
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
.muted{color:#666;font-size:.9rem}a{color:inherit}.proof{color:#2e7d32;font-weight:600;font-size:.95rem}.quote{border-left:3px solid #2e7d32;margin:.6rem 0;padding:.2rem .8rem}.cta.alt{background:#2e7d32}
.lead{border:1px solid #e3e3e3;border-radius:8px;padding:1rem;margin:1.2rem 0;max-width:34rem}.lead label{font-weight:600;display:block;margin-bottom:.4rem}
.lead input[type=email]{padding:.6rem;border:1px solid #bbb;border-radius:6px;width:100%;max-width:20rem;font-size:1rem}.lead button{padding:.62rem 1rem;border:0;border-radius:6px;background:#2e7d32;color:#fff;font-weight:600;cursor:pointer}
.hero{font-size:1.25rem;font-weight:600;margin:.2rem 0 .6rem}.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}.tag{display:inline-block;background:#fff3e0;color:#8a4b00;border-radius:4px;padding:0 .35rem;font-size:.85rem}
@media (prefers-color-scheme: dark){body{background:#111;color:#eee}.lede{color:#bbb}td,th,.kpi{border-color:#333}.cta{background:#eee;color:#111}.muted{color:#999}}"""


def lead_form_html(action: str, niche: str, source: str, size: int = 10) -> str:
    """Plain HTML form (works without JavaScript) posting to the tunnel's capture endpoint.
    ``website`` is a honeypot: people never see or fill it, form-spamming bots do."""
    if not action:
        return ""
    return f"""<form class="lead" id="lead" method="post" action="{html.escape(action)}">
<label for="lm-email">Free: {size} records + a hiring-intent cheatsheet, by email</label>
<input type="email" id="lm-email" name="email" required maxlength="254" autocomplete="email" placeholder="you@company.com">
<input type="hidden" name="niche" value="{html.escape(niche)}"><input type="hidden" name="source" value="{html.escape(source)}">
<input type="hidden" name="ref" value="" data-lm-ref>
<span class="hp" aria-hidden="true"><label>Website <input type="text" name="website" tabindex="-1" autocomplete="off"></label></span>
<button type="submit">Send me the free sample</button>
<p class="muted">One email with the sample now. Confirm from it to get the Monday Tech Pulse (3 fresh buying signals a week). Unsubscribe with one click, any time.</p>
</form>"""


# Copies first-touch attribution (set by ATTRIBUTION_JS) into the free-sample form, so signups
# are attributed to the channel that brought the visitor, like checkouts are.
LEAD_REF_JS = """(function(){try{var st=JSON.parse(localStorage.getItem('%(key)s')||'null');
document.querySelectorAll('[data-lm-ref]').forEach(function(el){if(st&&st.ref&&Date.now()-st.t<%(days)d*864e5){el.value=st.ref;}});
}catch(e){}})();""" % {"key": STORAGE_KEY, "days": FIRST_TOUCH_DAYS}


# Copy bandit, phase 1 (before ATTRIBUTION_JS): pick this visitor's arms (sticky per policy
# version), swap the headline and CTA, report the view. Phase 2 (after it): tag checkout links'
# client_reference_id with the variant, so Stripe purchases are credited to the copy that sold.
COPY_APPLY_JS = """var AMC=(function(){try{var el=document.getElementById('am-copy');if(!el)return null;var cfg=JSON.parse(el.textContent);
function ls(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v);}catch(e){return null;}}
var vid=ls('am_vid');if(!vid){vid=Math.random().toString(36).slice(2)+Date.now().toString(36);ls('am_vid',vid);}
var key='am_copy_'+cfg.version,pick={};try{pick=JSON.parse(ls(key)||'{}')||{};}catch(e){pick={};}
Object.keys(cfg.arms).forEach(function(slot){var arms=cfg.arms[slot];if(pick[slot]&&arms[pick[slot]])return;
var r=Math.random(),acc=0,names=Object.keys(arms);pick[slot]=names[names.length-1];
for(var i=0;i<names.length;i++){acc+=arms[names[i]].w;if(r<acc){pick[slot]=names[i];break;}}});ls(key,JSON.stringify(pick));
var h=document.querySelector('[data-copy=headline]');if(h&&pick.headline)h.textContent=cfg.arms.headline[pick.headline].text;
var c=document.querySelector('[data-copy=cta]');if(c&&pick.cta){c.textContent=cfg.arms.cta[pick.cta].text;var t=cfg.targets[pick.cta];
if(t){c.setAttribute('href',t);if(t.charAt(0)==='#')c.removeAttribute('data-checkout');else c.setAttribute('data-checkout','');}}
var tag='h'+cfg.codes.headline[pick.headline]+'_c'+cfg.codes.cta[pick.cta];
document.querySelectorAll('form.lead').forEach(function(f){var i=document.createElement('input');i.type='hidden';i.name='copy';i.value=tag;f.appendChild(i);});
function send(e){if(!cfg.endpoint||!navigator.sendBeacon)return;try{navigator.sendBeacon(cfg.endpoint,new Blob([JSON.stringify(
{e:e,h:pick.headline,c:pick.cta,v:vid,p:cfg.page})],{type:'text/plain'}));}catch(x){}}
send('view');if(c)c.addEventListener('click',function(){send('click');});return {tag:tag};}catch(e){return null;}})();"""
COPY_TAG_JS = """(function(){if(!AMC)return;document.querySelectorAll('a[data-checkout]').forEach(function(a){try{var u=new URL(a.href);
var ref=u.searchParams.get('client_reference_id')||'am--direct--';var p=ref.split('--');while(p.length<3)p.push('');
u.searchParams.set('client_reference_id',p.slice(0,3).join('--')+'--'+AMC.tag);a.href=u.toString();}catch(e){}});})();"""


def copy_blob(page: ProductPage) -> str:
    from tools.copy_bandit import SLOTS

    data = {"version": page.copy_version, "page": page.slug, "endpoint": page.telemetry_url, "arms": page.copy_arms,
            "targets": page.copy_targets, "codes": {slot: {v: a["code"] for v, a in arms.items()} for slot, arms in SLOTS.items()}}
    return _jsonld_script(data)  # same escaping: the blob can't close its <script> element


def _server_pick(arms: dict[str, Any]) -> str | None:
    """What crawlers and no-JS visitors see: the arm with the most traffic (the current winner)."""
    return max(arms, key=lambda v: (arms[v]["w"], v)) if arms else None


def render_product_page(page: ProductPage, base_url: str = "", brand: str = "Tech Stack Intel") -> str:
    url = page_url(base_url, page.slug)
    title = html.escape(page.title)
    desc = html.escape(meta_description(page.summary))
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
        f'<a class="cta" data-checkout href="{html.escape(page.checkout_url)}" rel="noopener">Buy the full dataset: {price}</a>'
        if page.checkout_url else "<p><em>Checkout opens soon.</em></p>"
    )
    headline_html = ""
    if page.copy_arms.get("cta"):
        v = _server_pick(page.copy_arms["cta"])
        href = page.copy_targets.get(v, page.checkout_url)
        checkout_attr = "" if href.startswith("#") else " data-checkout"
        cta = (f'<a class="cta" data-copy="cta"{checkout_attr} href="{html.escape(href)}" rel="noopener">'
               f'{html.escape(page.copy_arms["cta"][v]["text"])}</a>'
               + (f' <a class="cta alt" data-checkout href="{html.escape(page.checkout_url)}" rel="noopener">Buy the full dataset: {price}</a>'
                  if page.checkout_url and v != "instant_feed" else ""))
    if page.copy_arms.get("headline"):
        v = _server_pick(page.copy_arms["headline"])
        headline_html = f'<p class="hero" data-copy="headline">{html.escape(page.copy_arms["headline"][v]["text"])}</p>'
    copy_script = (f'<script type="application/json" id="am-copy">{copy_blob(page)}</script>'
                   if page.copy_arms and page.telemetry_url else "")
    if page.subscription_url and page.subscription_price_cents:
        cta += (f' <a class="cta alt" data-checkout href="{html.escape(page.subscription_url)}" rel="noopener">'
                f"Weekly updates: ${page.subscription_price_cents / 100:.2f}/{html.escape(page.subscription_interval)}</a>")
    if page.annual_url and page.annual_price_cents:
        cta += (f' <a class="cta alt" data-checkout href="{html.escape(page.annual_url)}" rel="noopener">'
                f"Yearly: ${page.annual_price_cents / 100:.0f}/year</a>")
    if page.team_url and page.team_price_cents:
        cta += (f'<p class="muted">Buying for a team? <a data-checkout href="{html.escape(page.team_url)}" rel="noopener">'
                f"Team license for up to {page.team_seats} people: ${page.team_price_cents / 100:.0f}</a></p>")
    if page.checkout_url:
        cta += trust_html(page.refund_days)
    proof = []
    if page.data_updated_at:
        proof.append(f'Updated <time class="ago" datetime="{html.escape(page.data_updated_at)}">'
                     f"{html.escape(page.data_updated_at[:16].replace('T', ' '))} UTC</time>")
    if page.profiles_added_7d:
        proof.append(f"{page.profiles_added_7d} company profiles added this week")
    if page.verified_profiles:
        proof.append(f"{page.verified_profiles} verified careers pages")
    if page.purchases_7d:
        proof.append(f"{page.purchases_7d} purchase{'s' if page.purchases_7d != 1 else ''} in the last 7 days")
    proof_html = f'<p class="proof">{" · ".join(proof)}</p>' if proof else ""
    if page.testimonials:
        proof_html += "".join(f'<blockquote class="quote">“{html.escape(q)}”<br><span class="muted">— Verified buyer</span></blockquote>'
                              for q in page.testimonials)
    og_image = f"{url}og.png" if url.startswith("http") else "og.png"
    canonical = f'<link rel="canonical" href="{html.escape(url)}">' if url.startswith("http") else ""
    feed_href = f"{base_url.rstrip('/')}/{FEED_PATH}" if base_url else f"../{FEED_PATH}"
    feed = f'<link rel="alternate" type="application/rss+xml" title="Tech Radar" href="{html.escape(feed_href)}">'
    if page.versions and page.kind == "dataset":
        feed += f'<link rel="alternate" type="application/rss+xml" title="{title} updates" href="changelog/feed.xml">'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
{canonical}
{feed}
<meta property="og:type" content="product"><meta property="og:title" content="{title}"><meta property="og:description" content="{desc}">
{f'<meta property="og:url" content="{html.escape(url)}">' if url.startswith("http") else ""}
<meta property="og:image" content="{html.escape(og_image)}"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{title}"><meta name="twitter:description" content="{desc}"><meta name="twitter:image" content="{html.escape(og_image)}">
<meta property="product:price:amount" content="{page.price_cents / 100:.2f}"><meta property="product:price:currency" content="{html.escape(page.currency.upper())}">
<script type="application/ld+json">
{_jsonld_script(product_jsonld(page, url, brand))}
</script>
{page.extra_jsonld}
<style>{CSS}</style></head><body>
<h1>{title}</h1>
{banner_html(page.banner)}
{headline_html}
<p class="lede">{html.escape(page.summary)}</p>
{page.insight_html}
{page.ratings_html}
{proof_html}
<p><img src="radar-badge.svg" alt="{html.escape(str((page.metrics or {}).get("roles") or 0))} hiring signals tracked" height="20"></p>
<div class="kpis">{kpi_html}</div>
{signals_html}
{cta}
{lead_form_html(page.lead_capture_url, page.niche, page.slug)}
{page.related_html}
{faq_html(page.faq)}
{page.files_html}
<h2>Free 5-record preview</h2>
<p class="muted"><a href="sample.csv" download>Download the free sample (CSV)</a></p>
<div class="wrap"><table><thead><tr>{head_cells}</tr></thead><tbody>
{rows}
</tbody></table></div>
<p class="muted">Built from public job-board APIs; every record links to its source. Delivered instantly by email as CSV + JSON + an executive summary.{f" Updated {html.escape(page.updated_at[:10])}." if page.updated_at else ""}</p>
<p class="muted">{f'<a href="{html.escape(page.hub_href)}">More {html.escape(page.hub_label)} datasets</a> · ' if page.hub_href else ""}<a href="../">All datasets</a> · <a href="../pricing/">Pricing</a> · <a href="../intel/">Hiring intel by technology</a> · <a href="../feeds/radar.xml">RSS</a>{' · <a href="changelog/">Version history</a>' if page.versions else ""}</p>
<p class="muted">{footer_links("../")}</p>
{copy_script}
<script>{COPY_APPLY_JS if copy_script else ""}
{ATTRIBUTION_JS}
{COPY_TAG_JS if copy_script else ""}
{RELATIVE_TIME_JS}
{LEAD_REF_JS}</script>
</body></html>
"""


# ----------------------------------------------------------------------------- matrix pages
MATRIX_DIR = "intel"
PREVIEW_FIELDS = ["company", "intent_tag", "stack", "openings", "latest_posted_at"]
MIGRATION_PREVIEW_FIELDS = ["company", "migration_path", "intent_tag", "stack", "openings"]
_SLUG_SPECIAL = {"c++": "cpp", "c#": "csharp", ".net": "dotnet", "next.js": "nextjs", "node.js": "nodejs",
                 "vector db": "vector-databases", "llms": "llm"}


def tech_slug(tech: str) -> str:
    t = tech.lower()
    return _SLUG_SPECIAL.get(t) or re.sub(r"[^a-z0-9]+", "-", t.replace("+", "plus").replace("#", "sharp")).strip("-")


@dataclass
class MatrixPage:
    kind: str                      # hiring | migrations
    tech: str
    path: str                      # intel/companies-hiring-python-engineers.html
    title: str
    description: str
    stats: dict[str, Any]
    columns: list[str]
    rows: list[dict[str, Any]]
    updated_at: str
    offer: dict[str, Any] = field(default_factory=dict)  # niche, title, price_cents, checkout_url, subscription_url...
    related: list[tuple[str, str]] = field(default_factory=list)  # (title, path) for internal links


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _within(ts: str, now: datetime, days: int) -> bool:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - dt <= timedelta(days=days)


def _preview(records: list[dict[str, Any]], columns: list[str], n: int = 5) -> list[dict[str, Any]]:
    """Derived facts only: no contact details, descriptions or text quoted from postings."""
    ranked = sorted(records, key=lambda r: (-int(r.get("intent_score") or 0), -int(r.get("urgency_score") or 0),
                                            r.get("company", "").lower()))
    out = []
    for r in ranked[:n]:
        row = {c: r.get(c) for c in columns}
        if "stack" in row:
            row["stack"] = (r.get("stack") or [])[:5]
        if row.get("latest_posted_at"):
            row["latest_posted_at"] = str(row["latest_posted_at"])[:10]
        out.append(row)
    return out


def compile_matrix_pages(datasets: dict[str, list[dict[str, Any]]], offers: dict[str, dict[str, Any]], now: datetime,
                         min_companies: int = 5, min_migrations: int = 3, max_pages: int = 60) -> list[MatrixPage]:
    """Build the search-intent page set from every niche's tech radar.

    ``datasets``: niche -> records (``exports/intel/<niche>/tech_radar.json``).
    ``offers``: niche -> {title, price_cents, currency, checkout_url, subscription_url, ...}."""
    by_tech: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
    for niche, records in datasets.items():
        for r in records:
            for tech in r.get("stack") or []:
                # one row per company per technology, across niches (keep the strongest record)
                cur = by_tech.setdefault(tech, {}).get(_norm(r.get("company", "")))
                if cur is None or (r.get("intent_score") or 0) > (cur[1].get("intent_score") or 0):
                    by_tech[tech][_norm(r.get("company", ""))] = (niche, r)
    migrating: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
    for niche, records in datasets.items():
        for r in records:
            path = r.get("migration_path") or ""
            ends = [p.strip() for p in path.split("→")] if path else []
            if r.get("intent_category") == "migration" and not ends:
                ends = [t for t in (r.get("stack") or []) if t in ("Kubernetes", "AWS", "GCP", "Azure", "PostgreSQL", "Snowflake")]
            for tech in ends:
                if tech and tech not in ("Cloud", "On-prem"):
                    migrating.setdefault(tech, {})[_norm(r.get("company", ""))] = (niche, r)

    def offer_for(entries: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        counts = Counter(n for n, _ in entries if offers.get(n, {}).get("checkout_url"))
        return dict(offers[counts.most_common(1)[0][0]]) if counts else {}

    stamp = now.isoformat(timespec="seconds")
    month = now.strftime("%B %Y")
    pages: list[MatrixPage] = []
    for tech, entries_map in sorted(by_tech.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        entries = list(entries_map.values())
        if len(entries) < min_companies:
            continue
        recs = [r for _, r in entries]
        co = Counter(t for r in recs for t in (r.get("stack") or []) if t != tech)
        intent = Counter((r.get("commercial_signals") or [None])[0] for r in recs if r.get("intent_score"))
        stats = {
            "companies": len(recs), "roles": sum(int(r.get("openings") or 0) for r in recs),
            "companies_7d": sum(1 for r in recs if _within(r.get("latest_posted_at", ""), now, 7)),
            "companies_30d": sum(1 for r in recs if _within(r.get("latest_posted_at", ""), now, 30)),
            "high_intent": sum(1 for r in recs if r.get("intent_level") == "High"),
            "remote": sum(1 for r in recs if r.get("remote_friendly")),
            "co_stack": co.most_common(6), "intent_mix": [(k, v) for k, v in intent.most_common(5) if k],
        }
        pages.append(MatrixPage(
            kind="hiring", tech=tech, path=f"{MATRIX_DIR}/companies-hiring-{tech_slug(tech)}-engineers.html",
            title=f"{len(recs)} Companies Hiring {tech} Engineers ({month})",
            description=(f"{len(recs)} companies are hiring {tech} engineers right now ({stats['roles']} open roles, "
                         f"{stats['companies_7d']} posted in the last 7 days). See their stacks, buying-intent signals "
                         f"and hiring velocity, with a free 5-company preview."),
            stats=stats, columns=PREVIEW_FIELDS, rows=_preview(recs, PREVIEW_FIELDS), updated_at=stamp,
            offer=offer_for(entries),
        ))
    for tech, entries_map in sorted(migrating.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        entries = list(entries_map.values())
        if len(entries) < min_migrations:
            continue
        recs = [r for _, r in entries]
        paths = Counter(r.get("migration_path") for r in recs if r.get("migration_path"))
        stats = {
            "companies": len(recs), "roles": sum(int(r.get("openings") or 0) for r in recs),
            "companies_7d": sum(1 for r in recs if _within(r.get("latest_posted_at", ""), now, 7)),
            "companies_30d": sum(1 for r in recs if _within(r.get("latest_posted_at", ""), now, 30)),
            "high_intent": sum(1 for r in recs if r.get("intent_level") == "High"),
            "paths": paths.most_common(6),
            "into": sum(1 for r in recs if (r.get("migration_path") or "").endswith(tech)),
            "away": sum(1 for r in recs if (r.get("migration_path") or "").startswith(tech + " ")),
        }
        pages.append(MatrixPage(
            kind="migrations", tech=tech, path=f"{MATRIX_DIR}/{tech_slug(tech)}-infrastructure-migrations.html",
            title=f"{tech} Infrastructure Migrations: {len(recs)} Companies Hiring for Them ({month})",
            description=(f"{len(recs)} companies are hiring engineers for {tech} migration work ({stats['into']} moving to "
                         f"{tech}, {stats['away']} moving away). Migration paths, stacks and intent scores, with a free preview."),
            stats=stats, columns=MIGRATION_PREVIEW_FIELDS, rows=_preview(recs, MIGRATION_PREVIEW_FIELDS), updated_at=stamp,
            offer=offer_for(entries),
        ))
    return link_related(pages[:max_pages])


def link_related(pages: list[MatrixPage]) -> list[MatrixPage]:
    """Internal links: the same technology's other page, then the biggest neighbours."""
    for p in pages:
        same = [(q.title, q.path) for q in pages if q.tech == p.tech and q.path != p.path]
        others = [(q.title, q.path) for q in pages if q.tech != p.tech][:5]
        p.related = (same + others)[:6]
    return pages


def dataset_jsonld(page: MatrixPage, url: str, brand: str) -> dict[str, Any]:
    offer = page.offer or {}
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": page.title,
        "description": page.description,
        "keywords": [page.tech, f"{page.tech} hiring", "tech stack", "buying intent", "B2B leads",
                     *(["migration"] if page.kind == "migrations" else [])],
        "creator": {"@type": "Organization", "name": brand},
        "dateModified": page.updated_at,
        "isAccessibleForFree": False,
        "variableMeasured": ["company", "technology stack", "open roles", "intent score", "intent tag",
                             *(["migration path"] if page.kind == "migrations" else [])],
        "measurementTechnique": "Aggregated from public job-board APIs; stack fingerprinting and buying-intent classification",
        "size": f"{page.stats.get('companies', 0)} companies",
    }
    if url.startswith("http"):
        data["url"] = url
    if offer.get("checkout_url"):
        data["offers"] = {"@type": "Offer", "price": f"{int(offer.get('price_cents') or 0) / 100:.2f}",
                          "priceCurrency": str(offer.get("currency") or "usd").upper(),
                          "availability": "https://schema.org/InStock", "url": offer["checkout_url"]}
    return data


def render_matrix_page(page: MatrixPage, base_url: str = "", brand: str = "Tech Stack Intel",
                       lead_capture_url: str = "", sample_size: int = 10) -> str:
    url = f"{base_url.rstrip('/')}/{page.path}" if base_url else ""
    title, desc = html.escape(page.title), html.escape(meta_description(page.description))
    st = page.stats
    kpis = [("Companies", st.get("companies")), ("Open roles", st.get("roles")), ("Posted in 7 days", st.get("companies_7d")),
            ("Posted in 30 days", st.get("companies_30d")), ("High buying intent", st.get("high_intent"))]
    kpi_html = "".join(f'<div class="kpi"><b>{html.escape(str(v))}</b>{html.escape(k)}</div>' for k, v in kpis if v is not None)
    facts = []
    if page.kind == "hiring":
        velocity = (f"{st.get('companies_7d', 0)} of the {st.get('companies', 0)} companies posted a {page.tech} role in "
                    f"the last 7 days and {st.get('companies_30d', 0)} in the last 30.")
        facts.append(velocity)
        if st.get("co_stack"):
            facts.append(f"Most common alongside {page.tech}: " + ", ".join(f"{t} ({n})" for t, n in st["co_stack"]) + ".")
        if st.get("intent_mix"):
            facts.append("Buying signals: " + ", ".join(f"{t} ({n})" for t, n in st["intent_mix"]) + ".")
        if st.get("remote"):
            facts.append(f"{st['remote']} hire remotely.")
    else:
        facts.append(f"{st.get('into', 0)} companies are moving to {page.tech}; {st.get('away', 0)} are moving away from it.")
        if st.get("paths"):
            facts.append("Migration paths: " + ", ".join(f"{p} ({n})" for p, n in st["paths"]) + ".")
    facts_html = "".join(f"<li>{html.escape(f)}</li>" for f in facts)
    head = "".join(f"<th>{html.escape(c.replace('_', ' '))}</th>" for c in page.columns)
    rows = "\n".join("<tr>" + "".join(
        f'<td>{("<span class=tag>" + html.escape(_cell(r.get(c))) + "</span>") if c == "intent_tag" and r.get(c) else html.escape(_cell(r.get(c)))}</td>'
        for c in page.columns) + "</tr>" for r in page.rows)
    offer = page.offer or {}
    cta = ""
    if offer.get("checkout_url"):
        cta = (f'<a class="cta" data-checkout href="{html.escape(offer["checkout_url"])}" rel="noopener">'
               f'Get the full dataset: ${int(offer.get("price_cents") or 0) / 100:.2f}</a>')
        if offer.get("subscription_url") and offer.get("subscription_price_cents"):
            cta += (f' <a class="cta alt" data-checkout href="{html.escape(offer["subscription_url"])}" rel="noopener">'
                    f'Weekly updates: ${int(offer["subscription_price_cents"]) / 100:.2f}/{html.escape(offer.get("subscription_interval", "month"))}</a>')
    lander = f'<a href="../{html.escape(offer["niche"])}/">{html.escape(offer.get("title", "the dataset"))}</a>' if offer.get("niche") else ""
    related = "".join(f'<li><a href="../{html.escape(p)}">{html.escape(t)}</a></li>' for t, p in page.related)
    canonical = f'<link rel="canonical" href="{html.escape(url)}">' if url else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
{canonical}
<meta property="og:type" content="website"><meta property="og:title" content="{title}"><meta property="og:description" content="{desc}">
{f'<meta property="og:url" content="{html.escape(url)}">' if url else ""}
<meta name="twitter:card" content="summary"><meta name="twitter:title" content="{title}"><meta name="twitter:description" content="{desc}">
<link rel="alternate" type="application/rss+xml" title="Tech Radar" href="../{FEED_PATH}">
<script type="application/ld+json">
{_jsonld_script(dataset_jsonld(page, url, brand))}
</script>
<style>{CSS}</style></head><body>
<p class="muted"><a href="../">Home</a> › <a href="./">Hiring intel</a> › {html.escape(page.tech)}</p>
<h1>{title}</h1>
<p class="lede">{html.escape(page.description)}</p>
<p class="proof">Updated <time class="ago" datetime="{html.escape(page.updated_at)}">{html.escape(page.updated_at[:16].replace("T", " "))} UTC</time></p>
<div class="kpis">{kpi_html}</div>
<ul>{facts_html}</ul>
{cta}
{lead_form_html(lead_capture_url, offer.get("niche", ""), page.path, sample_size)}
<h2>Preview: 5 of {st.get("companies", 0)} companies</h2>
<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>
{rows}
</tbody></table></div>
<p class="muted">Derived from public job postings: company-level stack fingerprints and buying-intent signals, no contact details. {("Full data: " + lander + ".") if lander else ""}</p>
{f"<h2>Related</h2><ul>{related}</ul>" if related else ""}
<script>{ATTRIBUTION_JS}
{RELATIVE_TIME_JS}
{LEAD_REF_JS}</script>
</body></html>
"""


def render_matrix_index(pages: list[MatrixPage], base_url: str, site_title: str) -> str:
    def section(kind: str, heading: str) -> str:
        items = "".join(f'<li><a href="{html.escape(p.path.split("/", 1)[1])}">{html.escape(p.title)}</a></li>'
                        for p in pages if p.kind == kind)
        return f"<h2>{heading}</h2><ul>{items}</ul>" if items else ""

    canonical = f'<link rel="canonical" href="{html.escape(base_url.rstrip("/"))}/{MATRIX_DIR}/">' if base_url else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hiring intel by technology: {html.escape(site_title)}</title>
<meta name="description" content="Which companies are hiring for each technology, and who is migrating: live counts, stacks and buying-intent signals.">
{canonical}<style>{CSS}</style></head><body>
<p class="muted"><a href="../">Home</a></p>
<h1>Hiring intel by technology</h1>
{section("hiring", "Companies hiring, by technology")}
{section("migrations", "Infrastructure migrations")}
</body></html>
"""


def render_index(pages: list[ProductPage], base_url: str, site_title: str, head_extra: str = "", sponsor: str = "",
                 more: bool = False, best: str = "", hubs: bool = False, blog: bool = False, products_feed: bool = False,
                 heatmap: bool = False, embed: bool = False, sources: bool = False, library: bool = False,
                 request: bool = False, links: tuple[tuple[str, str], ...] = ()) -> str:
    items = "\n".join(
        f'<li><a href="{html.escape(p.slug)}/">{html.escape(p.title)}</a>{POPULAR_BADGE if p.popular else ""}: '
        f'{html.escape(p.summary[:160])} '
        f"(${p.price_cents / 100:.2f})</li>"
        for p in pages
    )
    canonical = f'<link rel="canonical" href="{html.escape(base_url.rstrip("/") + "/")}">' if base_url else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(site_title)}</title><meta name="description" content="Company-level tech stack and hiring-intent datasets.">
{canonical}{head_extra}<link rel="alternate" type="application/rss+xml" title="Tech Radar" href="feeds/radar.xml">{'<link rel="alternate" type="application/rss+xml" title="New datasets" href="feeds/products.xml">' if products_feed else ""}
<style>{CSS}</style></head><body>
<h1>{html.escape(site_title)}</h1>
{sponsor}
<p class="lede">Who is hiring, what they run, and who is about to buy: company-level tech stack intelligence, refreshed continuously.</p>
{best}
<ul>
{items}
</ul>
<p class="muted"><a href="pricing/">Pricing</a> · <a href="compare/">Compare datasets</a> ·{' <a href="hiring/">By technology</a> ·' if hubs else ""}{' <a href="blog/">Blog</a> ·' if blog else ""}{' <a href="tools/hiring-heatmap/">Hiring heatmap</a> ·' if heatmap else ""}{' <a href="embed/">Embed badges</a> ·' if embed else ""}{' <a href="sources/">Data sources</a> ·' if sources else ""}{' <a href="library/">Your library</a> ·' if library else ""}{' <a href="request/">Request a dataset</a> ·' if request else ""}{"".join(f' <a href="{html.escape(href)}">{html.escape(text)}</a> ·' for href, text in links)}{' <a href="feeds/products.xml">New datasets (RSS)</a> ·' if products_feed else ""}{' <a href="more/">Custom datasets, lifetime pass &amp; gifts</a> ·' if more else ""} <a href="intel/">Hiring intel by technology</a> · <a href="feeds/radar.xml">Subscribe via RSS</a></p>
<p class="muted">{footer_links("")}</p>
</body></html>
"""


def matrix_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path}" if base_url else path


def render_sitemap(pages: list[ProductPage], base_url: str, matrix: list[MatrixPage] | None = None,
                   extra: list[str] | None = None) -> str:
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    urlset = ET.Element("urlset", xmlns=ns)
    entries = [(page_url(base_url, ""), None)] + [(page_url(base_url, p.slug), p.updated_at) for p in pages]
    entries += [(matrix_url(base_url, path), None) for path in extra or []]
    if matrix:
        entries.append((matrix_url(base_url, f"{MATRIX_DIR}/"), max(m.updated_at for m in matrix)))
        entries += [(matrix_url(base_url, m.path), m.updated_at) for m in matrix]
    for loc, lastmod in entries:
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

    @property
    def lead_capture_url(self) -> str:
        cfg = self.config
        base = cfg.lead_capture_base if cfg.lead_magnet_enabled else ""
        return f"{base}/lead-magnet/capture" if base else ""

    def build(self, pages: list[ProductPage], feed_items: list[FeedItem], now: datetime,
              matrix: list[MatrixPage] | None = None, indexnow_key: str = "", extra: dict[str, str] | None = None,
              sponsor: str = "", thanks_extra: str = "", best_sellers: str = "") -> dict[str, str | bytes]:
        cfg = self.config
        out: dict[str, str | bytes] = {}
        matrix = matrix or []
        capture = self.lead_capture_url
        for p in pages:
            if capture and not p.lead_capture_url and p.kind == "dataset":
                p.lead_capture_url = capture
            out[f"{p.slug}/index.html"] = render_product_page(p, self.base_url, cfg.site_title)
            label = f"{p.niche.split('-')[0]} radar"
            out[f"{p.slug}/radar-badge.svg"] = badge_for(label, p.metrics or {})
            from strategies.buyer_experience import sample_csv

            out[f"{p.slug}/sample.csv"] = sample_csv(p.sample_columns, p.sample_rows)  # Phase 206
            price = f"${p.price_cents / 100:.2f}"
            out[f"{p.slug}/og.svg"] = render_og_svg(p.title, p.metrics or {}, price)
            png = render_og_png(p.title, p.metrics or {}, price) if cfg.og_images and p.kind != "micro" else None
            if png:
                out[f"{p.slug}/og.png"] = png
        total_roles = sum(int((p.metrics or {}).get("roles") or 0) for p in pages if p.kind == "dataset")
        out["radar-badge.svg"] = render_badge_svg("tech radar", f"{total_roles} hiring signals tracked")
        from tools.offer_pages import _shell
        from tools.site_extras import legal_pages, not_found_page, verification_meta

        from strategies.site_discovery import INDEX_LINKS
        from tools import site_more

        contact = cfg.sender_email or cfg.owner_email or ""
        out["index.html"] = render_index(pages, self.base_url, cfg.site_title, verification_meta(cfg)
                                         + site_more.home_jsonld(self.base_url, cfg.site_title, contact), sponsor=sponsor,
                                         more="more/index.html" in (extra or {}), best=best_sellers,
                                         hubs="hiring/index.html" in (extra or {}), blog="blog/index.html" in (extra or {}),
                                         products_feed="feeds/products.xml" in (extra or {}),
                                         heatmap="tools/hiring-heatmap/index.html" in (extra or {}),
                                         embed="embed/index.html" in (extra or {}),
                                         sources="sources/index.html" in (extra or {}),
                                         library="library/index.html" in (extra or {}), request="request/index.html" in (extra or {}),
                                         links=tuple((href, text) for path, href, text in INDEX_LINKS if path in (extra or {})))
        out.update(extra or {})  # e.g. more/ (strategies/revenue_models.py)
        out[site_more.COMPARE] = site_more.compare_page(pages, lambda title, body, desc: _shell(
            title, body, cfg.site_title, description=desc))
        out[site_more.LLMS] = site_more.llms_txt(pages, self.base_url, cfg.site_title)
        security = site_more.security_txt(contact, self.base_url, now)
        if security:
            out[site_more.SECURITY] = security
        out[".nojekyll"] = ""  # GitHub Pages: serve the files as they are (Jekyll would hide .well-known/)
        for rel, page_html in legal_pages(cfg, lambda title, body, desc: _shell(
                title, body, cfg.site_title, description=desc, depth=rel_depth(title))).items():
            out[rel] = page_html
        out["404.html"] = not_found_page(pages, lambda title, body, root: _shell(title, body, cfg.site_title, noindex=True,
                                                                                 root=root), self.base_url)
        from tools.offer_pages import render_changelog
        from tools.site_extras import changelog_feed

        for p in pages:
            if p.kind == "dataset" and p.versions:
                out[f"{p.slug}/changelog/index.html"] = render_changelog(p, cfg.site_title)
                out[f"{p.slug}/changelog/feed.xml"] = changelog_feed(p, self.base_url, cfg.site_title)
        from tools.offer_pages import render_pricing, render_thanks

        out["thanks/index.html"] = render_thanks(pages, cfg.site_title, extra=thanks_extra)
        out["pricing/index.html"] = render_pricing(pages, cfg.site_title, sponsor=sponsor)
        for m in matrix:
            out[m.path] = render_matrix_page(m, self.base_url, cfg.site_title, capture, cfg.lead_magnet_sample_size)
        # Every page links to the intel index, so it always exists (an empty one says pages come as data grows):
        # the site audit caught that link 404ing on small sites.
        out[f"{MATRIX_DIR}/index.html"] = render_matrix_index(matrix, self.base_url, cfg.site_title)
        if indexnow_key:
            out[f"{indexnow_key}.txt"] = indexnow_key
        out["sitemap.xml"] = render_sitemap(pages, self.base_url, matrix, site_more.sitemap_extra(out))
        out["robots.txt"] = "User-agent: *\nAllow: /\n" + (f"Sitemap: {self.base_url}/sitemap.xml\n" if self.base_url else "")
        out[FEED_PATH] = render_rss(feed_items, self.base_url, cfg.site_title, now)
        for rel, content in out.items():
            if isinstance(content, bytes):
                self.files.write_bytes(f"site/{rel}", content)
            else:
                self.files.write_text(f"site/{rel}", content)
        return out

    pending: int = 0  # files left for the next cycle when the API budget ran out

    def publish(self, out: dict[str, str | bytes], state: Any = None) -> int:
        """Commit changed files to the Pages branch/dir. Returns how many files changed.

        With ``state``, a manifest of published content hashes (``site_manifest``) skips files
        that haven't changed since the last successful publish, so a site with dozens of matrix
        pages costs API calls only for what changed. If the per-cycle API budget runs out
        part-way, the rest is published next cycle (``self.pending``) instead of failing the task."""
        from tools.errors import CircuitOpenError

        cfg = self.config
        self.pending = 0
        if not (self.github and self.github.configured() and cfg.github_pages_repo):
            return 0
        branch = cfg.github_pages_branch or cfg.github_branch
        prefix = f"{cfg.github_pages_dir.strip('/')}/" if cfg.github_pages_dir.strip("/") else ""
        key = f"site_manifest:{cfg.github_pages_repo}:{branch}:{prefix}"
        manifest: dict[str, str] = dict((state.get(key) if state is not None else None) or {})
        todo = []
        for rel, content in sorted(out.items()):
            digest = hashlib.sha256(content if isinstance(content, bytes) else content.encode()).hexdigest()[:20]
            if manifest.get(rel) != digest:
                todo.append((rel, content, digest))
        changed = 0
        try:
            for i, (rel, content, digest) in enumerate(todo):
                try:
                    res = self.github.put_file(cfg.github_pages_repo, prefix + rel, content, f"site: update {rel}", branch)
                except CircuitOpenError:
                    self.pending = len(todo) - i
                    break
                changed += bool(res.get("changed"))
                manifest[rel] = digest
            if not self.pending and state is not None:
                changed += self._delete_stale(out, manifest, prefix, branch, state)
        finally:
            if state is not None:
                state.set(key, manifest)
        return changed

    def _delete_stale(self, out: dict[str, str | bytes], manifest: dict[str, str], prefix: str, branch: str, state: Any) -> int:
        """Phase 210: pages that are no longer built (a retired product, a removed hub) are taken down.
        Only files this agent published (in its manifest) are ever deleted, and never when the build
        looks broken (much smaller than what's published), so a bug can't wipe the site."""
        from tools.errors import CircuitOpenError

        stale = sorted(rel for rel in manifest if rel not in out)
        if not stale:
            return 0
        if len(out) < 0.5 * len(manifest):
            state.log_error("site", f"not removing {len(stale)} old page(s): this build has {len(out)} files against "
                                    f"{len(manifest)} published, which looks wrong", kind="alert")
            return 0
        removed = 0
        for rel in stale[:STALE_DELETES_PER_RUN]:
            try:
                self.github.delete_file(self.config.github_pages_repo, prefix + rel, f"site: remove {rel}", branch)
            except CircuitOpenError:
                break
            except Exception as exc:  # noqa: BLE001 - the page stays up; tried again next build
                state.log_error("site", f"couldn't remove {rel}: {exc!r}")
                break
            manifest.pop(rel, None)
            removed += 1
        return removed


# ----------------------------------------------------------------------------- IndexNow
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"


def indexnow_key(config: "Config", state: Any) -> str:
    """The configured key, or one generated once and kept in state (the key file is published with the site)."""
    if config.indexnow_key:
        return config.indexnow_key
    key = state.get("indexnow_key")
    if not key:
        key = secrets.token_hex(16)
        state.set("indexnow_key", key)
    return key


def changed_urls(out: dict[str, str | bytes], base_url: str, previous: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """HTML pages whose content changed since the last submission (by hash). Returns (urls, new hashes)."""
    hashes: dict[str, str] = {}
    urls = []
    for rel, content in sorted(out.items()):
        if not rel.endswith(".html"):
            continue
        body = content if isinstance(content, bytes) else content.encode()
        # the relative-time script and timestamps change every build; hash without the timestamp lines
        digest = hashlib.sha256(re.sub(rb"<time[^>]*>.*?</time>|\d{4}-\d\d-\d\dT[\d:]+(?:\+00:00)?", b"", body)).hexdigest()[:16]
        hashes[rel] = digest
        if previous.get(rel) != digest:
            loc = rel[: -len("index.html")] if rel.endswith("index.html") else rel
            urls.append(f"{base_url.rstrip('/')}/{loc}")
    return urls, hashes


def submit_indexnow(http: Any, base_url: str, key: str, urls: list[str]) -> int:
    """POST changed URLs to IndexNow (shared by Bing, Yandex, Seznam, Naver). Returns the HTTP status."""
    from urllib.parse import urlsplit

    if not urls or not base_url.startswith("https://"):
        return 0
    resp = http.post(INDEXNOW_ENDPOINT, json_body={
        "host": urlsplit(base_url).netloc, "key": key, "keyLocation": f"{base_url.rstrip('/')}/{key}.txt",
        "urlList": urls[:10000],
    }, headers={"Content-Type": "application/json; charset=utf-8"}, check_robots=False, attempts=2)
    return resp.status
