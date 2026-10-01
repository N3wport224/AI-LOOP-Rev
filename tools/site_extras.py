"""Trust pages and blocks for the public site (Phases 80-84).

* **Legal pages** (Phase 80): ``legal/terms/``, ``legal/privacy/``, ``legal/refunds/`` and
  ``contact/``, written from your settings (business name, contact email, postal address,
  ``refund_policy_days``). Payment processors expect a refund policy and contact details to be
  visible, and buyers look for them. Every page links to them in its footer.
* **Sale banner** (Phase 81): while a quarterly sale or a launch code is running, the product page
  and the pricing page show it at the top.
* **FAQ** (Phase 82): a short FAQ on each product page (format, delivery, freshness, refunds,
  updates, team use), with ``FAQPage`` structured data.
* **Search engine verification** (Phase 83): ``google_site_verification`` /
  ``bing_site_verification`` add the meta tag Search Console / Bing Webmaster Tools ask for.
* **404 page** (Phase 84): ``404.html`` (GitHub Pages serves it for missing URLs) with links to
  every dataset and the pricing page, so an old link still leads somewhere.
* **Trust row** (Phase 100): under the buy buttons, "Secure checkout by Stripe · Instant delivery
  by email · N-day refund", with N from ``refund_policy_days``.
* **Most popular** (Phase 103): the dataset with the most kept (not refunded or disputed) orders in
  the last 30 days, if it has at least two, gets a badge on the home and pricing pages.
* **Per-dataset feed** (Phase 104): ``<dataset>/changelog/feed.xml`` has one item per version, so
  a buyer can follow one dataset in a feed reader without the site-wide radar feed.

These pages state facts from your configuration only; they're templates, not legal advice. Read
them once (``site/legal/`` in the data folder) and adjust the wording in ``refund_policy_days`` or
by editing the generated pages if you need to.
"""

from __future__ import annotations

import html
import json
from typing import Any

LEGAL = {"legal/terms/index.html": "Terms of sale", "legal/privacy/index.html": "Privacy",
         "legal/refunds/index.html": "Refunds", "contact/index.html": "Contact"}


def footer_links(prefix: str) -> str:
    """Links to the legal pages from a page ``prefix`` levels deep ("" for the root, "../" one level down)."""
    return " · ".join(f'<a href="{prefix}{path[:-len("index.html")]}">{html.escape(label)}</a>' for path, label in LEGAL.items())


def _business(cfg: Any) -> str:
    return cfg.sender_name or cfg.site_title or "this site"


def legal_pages(cfg: Any, shell: Any) -> dict[str, str]:
    """``shell(title, body, description) -> html`` renders each page in the site's style."""
    name, email = html.escape(_business(cfg)), html.escape(cfg.sender_email or cfg.owner_email or "")
    address = html.escape(cfg.sender_postal_address or "")
    days = int(cfg.refund_policy_days)
    contact_line = (f"<p>Email: <a href=\"mailto:{email}\">{email}</a></p>" if email else "") + (f"<p>Post: {address}</p>" if address else "")
    terms = (f"<h1>Terms of sale</h1><p>{name} sells downloadable datasets built from public job postings, and subscriptions "
             "to weekly updates of them.</p><ul><li><b>Delivery:</b> by email right after payment, as a zip file or a private "
             "download link.</li><li><b>Use:</b> one purchase is for your own use (a team license covers the people it names). "
             "Please don't resell or republish the data.</li><li><b>Subscriptions</b> renew until you cancel; cancel any time "
             "from the link in your receipt or by replying to any update email.</li><li><b>Accuracy:</b> every row links to a "
             "public posting; postings change and close, so the data is a snapshot of the date shown, provided as is.</li>"
             f"<li><b>Refunds:</b> see the <a href=\"../refunds/\">refund policy</a>.</li><li><b>Payments</b> are processed by "
             "Stripe; card details never reach us.</li></ul><h2>Contact</h2>" + contact_line)
    privacy = (f"<h1>Privacy</h1><p>What {name} keeps, and why:</p><ul>"
               "<li><b>Buyers:</b> your email address and what you bought, to deliver it, send receipts and updates you paid for, "
               "and answer you. Payment details stay with Stripe.</li>"
               "<li><b>Free sample:</b> your email address, after you confirm it, for the weekly sample. Every email has a "
               "one-click unsubscribe.</li>"
               "<li><b>Site visits:</b> no cookies and no third-party trackers. The page remembers in your own browser which "
               "site sent you (for 30 days), so a purchase can be credited to it, and counts anonymously which page wording "
               "visitors click.</li>"
               "<li><b>Companies in our datasets:</b> the rows come from public job postings. A company that would rather "
               "not appear can <a href=\"../../remove/\">ask to be left out</a>; once checked, its postings leave every "
               "dataset within a week.</li>"
               "<li><b>Site search:</b> the words of a search may be sent to us (never with your address or any identifier, "
               "and not at all when your browser asks not to be tracked), so we can build what people look for. We keep the "
               "most common ones for 90 days.</li></ul>"
               "<p>We never sell or share your data. Ask for a copy of what we hold about you, or for it to be deleted, by "
               "emailing us; we answer within 30 days.</p>" + contact_line)
    refunds = (f"<h1>Refunds</h1><p>If a dataset doesn't match its description, or the file doesn't arrive, email us within "
               f"<b>{days} days</b> of buying and we'll fix it or refund you in full.</p><p>If the file didn't arrive, use the "
               "link in your receipt or reply to it and it's re-sent at once.</p><p>Subscriptions can be cancelled at any "
               "time; the current period isn't charged again.</p>" + contact_line)
    contact = f"<h1>Contact</h1><p>Questions about an order, the data or a subscription: we usually answer within a day.</p>{contact_line}"
    return {
        "legal/terms/index.html": shell("Terms of sale", terms, f"Terms of sale for {_business(cfg)}: delivery, use, subscriptions "
                                                                "and refunds for downloadable hiring datasets."),
        "legal/privacy/index.html": shell("Privacy", privacy, f"What {_business(cfg)} keeps about buyers and visitors, why, and "
                                                              "how to get a copy or have it deleted."),
        "legal/refunds/index.html": shell("Refunds", refunds, f"Refund policy: email within {days} days if a dataset doesn't match "
                                                              "its description or never arrived."),
        "contact/index.html": shell("Contact", contact, f"How to reach {_business(cfg)} about an order, the data or a "
                                                        "subscription; replies usually within a day."),
    }


def trust_html(refund_days: int) -> str:
    """Phase 100: what a first-time buyer wants to know before clicking buy."""
    parts = ["🔒 Secure checkout by Stripe", "Instant delivery by email"]
    if refund_days > 0:
        parts.append(f'<a href="../legal/refunds/">{int(refund_days)}-day refund</a>')
    return f'<p class="muted trust">{" · ".join(parts)}</p>'


POPULAR_MIN = 2


def popular_niche(state: Any, days: int = 30) -> str:
    """Phase 103: the niche with the most kept orders lately ("" if none has POPULAR_MIN)."""
    from datetime import timedelta

    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    row = state._one("SELECT a.niche AS niche, COUNT(*) AS n FROM orders o JOIN assets a ON a.id = o.asset_id "
                     "WHERE o.status NOT IN ('refunded', 'disputed') AND o.occurred_at >= ? AND a.niche IS NOT NULL "
                     "GROUP BY a.niche ORDER BY n DESC, a.niche LIMIT 1", (since,))
    return row["niche"] if row and int(row["n"]) >= POPULAR_MIN else ""


POPULAR_BADGE = ('<span style="background:#1d4ed8;color:#fff;border-radius:4px;padding:1px 6px;font-size:.8rem;'
                 'font-weight:600;margin-left:6px">Most popular</span>')


def changelog_feed(page: Any, base_url: str, site_title: str) -> str:
    """Phase 104: RSS 2.0 for one dataset's versions."""
    import xml.etree.ElementTree as ET
    from datetime import datetime, timezone
    from email.utils import format_datetime

    home = f"{base_url.rstrip('/')}/{page.slug}/" if base_url else f"{page.slug}/"
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = f"{page.title}: updates ({site_title})"
    ET.SubElement(ch, "link").text = home
    ET.SubElement(ch, "description").text = f"A new item whenever a new version of {page.title} is built."
    for v in page.versions[:30]:
        el = ET.SubElement(ch, "item")
        ET.SubElement(el, "title").text = f"{page.title} v{int(v['version'])}: {int(v['rows']):,} rows"
        ET.SubElement(el, "link").text = home
        ET.SubElement(el, "guid", isPermaLink="false").text = f"{page.slug}-v{int(v['version'])}"
        try:
            when = datetime.fromisoformat(str(v["date"])[:10]).replace(tzinfo=timezone.utc)
            ET.SubElement(el, "pubDate").text = format_datetime(when)
        except ValueError:
            pass
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"


def banner_html(text: str) -> str:
    if not text:
        return ""
    return (f'<p style="background:#fef3c7;color:#78350f;padding:8px 12px;border-radius:6px;font-weight:600">'
            f"{html.escape(text)}</p>")


def faq(cfg: Any, page: Any) -> list[tuple[str, str]]:
    days = int(cfg.refund_policy_days)
    qa = [
        ("What do I get?", "A zip with the full list as CSV (plus an Excel-ready copy) and JSON, an executive summary, a "
                           "coverage report (QUALITY.md) and a guide to every column."),
        ("How is it delivered?", "By email within a minute of paying. Large files come as a private download link."),
        ("How fresh is it?", "It's rebuilt whenever new job postings arrive" + (f"; this version is from {page.updated_at[:10]}."
                                                                                if page.updated_at else ".")),
        ("Can I get a refund?", f"Yes: if it doesn't match the description or never arrives, email within {days} days."),
    ]
    if page.subscription_url:
        qa.append(("Can I get updates?", "Yes: the subscription emails the newest version every week. Cancel any time."))
    if page.team_url:
        qa.append(("Can my team use it?", f"Buy the team license: it covers up to {page.team_seats} people in one organisation."))
    return qa


def faq_html(qa: list[tuple[str, str]]) -> str:
    if not qa:
        return ""
    items = "".join(f"<details><summary>{html.escape(q)}</summary><p>{html.escape(a)}</p></details>" for q, a in qa)
    data = {"@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in qa]}
    blob = json.dumps(data).replace("</", "<\\/")
    return f'<h2>Questions</h2>{items}<script type="application/ld+json">{blob}</script>'


def verification_meta(cfg: Any) -> str:
    tags = []
    if getattr(cfg, "google_site_verification", ""):
        tags.append(f'<meta name="google-site-verification" content="{html.escape(cfg.google_site_verification)}">')
    if getattr(cfg, "bing_site_verification", ""):
        tags.append(f'<meta name="msvalidate.01" content="{html.escape(cfg.bing_site_verification)}">')
    return "".join(tags)


def not_found_page(pages: list[Any], shell: Any, base_url: str) -> str:
    """Served for any missing URL, at any depth: every link is absolute (the site's base URL)."""
    root = (base_url.rstrip("/") if base_url else "") + "/"
    links = "".join(f'<li><a href="{html.escape(root + p.slug)}/">{html.escape(p.title)}</a></li>' for p in pages)
    body = ("<h1>Page not found</h1><p>This page moved or no longer exists. Every dataset on sale:</p>"
            f"<ul>{links}</ul><p><a href=\"{html.escape(root)}pricing/\">Pricing</a></p>")
    return shell("Page not found", body, root)
