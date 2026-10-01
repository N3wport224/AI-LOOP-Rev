"""Two site pages built from the product pages: a post-checkout thank-you page and a pricing page.

* ``thanks/`` (Phase 57): where Stripe sends a buyer after paying (when ``checkout_thank_you`` is
  on). It confirms the file is on its way by email, then offers what they don't have yet: the
  bundle, weekly updates, a team license.
* ``pricing/`` (Phase 59): every way to buy, side by side (one dataset, the yearly and monthly
  plans, the team license, the bundle), so visitors compare in one place.

Both are static HTML, no tracking beyond the usual attributed checkout links.
"""

from __future__ import annotations

import html
from typing import Any

CSS = ("body{font:16px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;max-width:860px;margin:0 auto;padding:16px;"
       "color:#1d1d1f;background:#fff}h1{font-size:1.6rem}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #eee;"
       "padding:8px;text-align:left;vertical-align:top}.cta{display:inline-block;background:#1d4ed8;color:#fff;padding:6px 12px;"
       "border-radius:6px;text-decoration:none}.muted{color:#666;font-size:.9rem}.wrap{overflow-x:auto}"
       "@media (prefers-color-scheme:dark){body{background:#111;color:#eee}td,th{border-color:#333}.muted{color:#aaa}}")


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}".replace(".00", "")


def _shell(title: str, body: str, site_title: str, noindex: bool = False, description: str = "") -> str:
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    if description:
        robots += f'<meta name="description" content="{html.escape(description)}">'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, '
            f'initial-scale=1"><title>{html.escape(title)} · {html.escape(site_title)}</title>{robots}<style>{CSS}</style></head>'
            f'<body>{body}<p class="muted"><a href="../">All datasets</a></p></body></html>\n')


def _a(url: str, text: str) -> str:
    return f'<a class="cta" data-checkout href="{html.escape(url)}" rel="noopener">{html.escape(text)}</a>'


def render_thanks(pages: list[Any], site_title: str) -> str:
    datasets = [p for p in pages if p.kind == "dataset"]
    bundle = next((p for p in pages if p.kind == "bundle" and p.checkout_url), None)
    offers = []
    if bundle:
        offers.append(f"<li><b>Every dataset in one download</b>: {_money(bundle.price_cents)}. {_a(bundle.checkout_url, 'Get the bundle')}</li>")
    for p in datasets:
        if p.subscription_url and p.subscription_price_cents:
            offers.append(f"<li><b>{html.escape(p.title)}, kept fresh</b>: weekly updates for {_money(p.subscription_price_cents)}"
                          f"/{html.escape(p.subscription_interval)}. {_a(p.subscription_url, 'Subscribe')}</li>")
        if p.team_url and p.team_price_cents:
            offers.append(f"<li><b>{html.escape(p.title)} for your team</b> (up to {p.team_seats} people): "
                          f"{_money(p.team_price_cents)}. {_a(p.team_url, 'Team license')}</li>")
    body = ("<h1>Thank you!</h1><p>Your payment went through. <b>Your file is on its way to your inbox</b> (it usually arrives "
            "within a minute; check spam if you don't see it). Reply to that email if anything is missing.</p>")
    if offers:
        body += "<h2>While you're here</h2><ul>" + "".join(offers[:6]) + "</ul>"
    return _shell("Thank you", body, site_title, noindex=True)


def render_changelog(page: Any, site_title: str) -> str:
    """Phase 66: every version of one dataset, with how much it grew: proof the data is alive."""
    rows = []
    versions = list(page.versions)
    for i, v in enumerate(versions):
        older = versions[i + 1] if i + 1 < len(versions) else None
        delta = int(v["rows"]) - int(older["rows"]) if older else None
        change = (f"+{delta}" if delta and delta > 0 else str(delta)) if delta is not None else "first release"
        rows.append(f"<tr><td>v{int(v['version'])}</td><td>{html.escape(str(v['date']))}</td><td>{int(v['rows']):,}</td>"
                    f"<td>{html.escape(change)}</td></tr>")
    latest = versions[0] if versions else {}
    label = f"Get v{int(latest.get('version', 1))}: {_money(page.price_cents)}"
    body = (f"<h1>{html.escape(page.title)}: version history</h1>"
            f"<p>A new version is built whenever new job postings arrive. Buyers get the newest version; subscribers get "
            f"every update.</p><p>{_a(page.checkout_url, label)}</p>"
            '<div class="wrap"><table><thead><tr><th>Version</th><th>Date</th><th>Rows</th><th>Change</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div>")
    desc = f"Every release of {page.title}: dates, row counts and growth. Updated whenever new job postings arrive."
    return _shell(f"{page.title} version history", body, site_title, description=desc[:158])


def render_pricing(pages: list[Any], site_title: str) -> str:
    rows = []
    for p in sorted((p for p in pages if p.kind == "dataset" and p.checkout_url), key=lambda p: p.title):
        cells = [f"<b>{html.escape(p.title)}</b>", _a(p.checkout_url, _money(p.price_cents))]
        cells.append(_a(p.subscription_url, f"{_money(p.subscription_price_cents)}/{p.subscription_interval}")
                     if p.subscription_url and p.subscription_price_cents else "<span class=muted>-</span>")
        cells.append(_a(p.annual_url, f"{_money(p.annual_price_cents)}/year") if p.annual_url and p.annual_price_cents
                     else "<span class=muted>-</span>")
        cells.append(_a(p.team_url, _money(p.team_price_cents)) if p.team_url and p.team_price_cents else "<span class=muted>-</span>")
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    bundle = next((p for p in pages if p.kind == "bundle" and p.checkout_url), None)
    body = ("<h1>Pricing</h1><p>Every dataset is a download of companies hiring right now, with their tech stack and how "
            "urgently they're hiring. Pay once, or subscribe for weekly updates.</p>")
    if rows:
        body += ('<div class="wrap"><table><thead><tr><th>Dataset</th><th>One-off</th><th>Weekly updates</th><th>Yearly</th>'
                 "<th>Team license</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    if bundle:
        body += f"<h2>Everything</h2><p>All datasets in one download: {_money(bundle.price_cents)}. {_a(bundle.checkout_url, 'Get the bundle')}</p>"
    if not rows and not bundle:
        body += "<p>Products appear here as soon as they're on sale.</p>"
    return _shell("Pricing", body, site_title, description="Every hiring dataset side by side: one-off downloads, weekly "
                                                           "updates, yearly plans, team licenses and the all-datasets bundle.")
