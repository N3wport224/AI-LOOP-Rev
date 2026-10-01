"""More of the public site (Phases 120-124).

* **Compare page** (Phase 120): ``compare/`` puts every dataset side by side (rows, companies,
  open roles, last update, price, link), so a visitor deciding between two doesn't have to open both.
* **Complete sitemap** (Phase 121): the sitemap also lists the pricing, compare, version-history and
  legal pages (``sitemap_extra``), not only products and intel pages.
* **llms.txt** (Phase 122): a plain-text summary of the site at ``/llms.txt`` (the emerging
  convention AI assistants read): what's sold, each dataset with its price and link.
* **Site structured data** (Phase 123): the home page carries ``Organization`` and ``WebSite``
  JSON-LD, so search engines show the site name properly.
* **security.txt** (Phase 124): ``/.well-known/security.txt`` (RFC 9116) tells anyone who finds a
  problem where to report it; it expires after a year and is rebuilt with every site build. A
  ``.nojekyll`` file makes GitHub Pages serve ``.well-known/`` (Jekyll skips dot-folders).

Everything is computed from the same pages the site is built from; nothing is invented.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta
from typing import Any

COMPARE = "compare/index.html"
LLMS = "llms.txt"
SECURITY = ".well-known/security.txt"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path}" if base_url else path


def datasets(pages: list[Any]) -> list[Any]:
    return sorted((p for p in pages if p.kind == "dataset"), key=lambda p: p.title)


def compare_page(pages: list[Any], shell: Any) -> str:
    """``shell(title, body, description) -> html`` (one level deep)."""
    rows = []
    for p in datasets(pages):
        m = p.metrics or {}
        latest = p.versions[0] if p.versions else {}
        cells = [f'<a href="../{html.escape(p.slug)}/">{html.escape(p.title)}</a>',
                 f"{int(latest.get('rows') or 0):,}" if latest else "-",
                 html.escape(str(m.get("companies") or "-")), html.escape(str(m.get("roles") or "-")),
                 html.escape(str(p.updated_at or "")[:10] or "-"),
                 f'<a href="{html.escape(p.checkout_url)}" rel="noopener">{_money(p.price_cents)}</a>' if p.checkout_url
                 else _money(p.price_cents)]
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    table = ('<div class="wrap"><table><thead><tr><th>Dataset</th><th>Rows</th><th>Companies</th><th>Open roles</th>'
             "<th>Updated</th><th>Price</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>") if rows \
        else "<p>The first dataset is being built.</p>"
    body = ("<h1>Compare datasets</h1><p>Every dataset side by side. Each is a download of companies hiring right now, with "
            'their tech stack and how urgently they hire. See also <a href="../pricing/">pricing</a>.</p>' + table)
    return shell("Compare datasets", body, "Compare every hiring dataset side by side: rows, companies, open roles, last "
                                           "update and price.")


SITEMAP_PAGES = ("trends/", "search/", "catalog/", "status/", "changes/", "new-employers/", "vs/", "company/")  # Phases 222-229, 267


def sitemap_extra(out: dict[str, Any]) -> list[str]:
    """Site paths beyond products and intel pages that belong in the sitemap."""
    keep = []
    for rel in sorted(out):
        if not rel.endswith("index.html") or rel == "index.html" or rel.startswith(("thanks/", "intel/")):
            continue
        if rel.count("/") == 1 and not rel.startswith(("pricing/", "compare/", "contact/", "hiring/", "more/", "affiliates/", "blog/", "embed/", "sources/", "request/",
                                                                *SITEMAP_PAGES)):
            continue  # product pages are already listed
        keep.append(rel[: -len("index.html")])
    return keep


def llms_txt(pages: list[Any], base_url: str, site_title: str) -> str:
    lines = [f"# {site_title}", "",
             "> Downloadable datasets of companies hiring right now, by technology: company, open roles, tech stack, hiring "
             "urgency and a link to every public posting. Delivered instantly by email as CSV, JSON, Excel and SQL.", ""]
    ds = datasets(pages)
    if ds:
        lines += ["## Datasets", ""]
        lines += [f"- [{p.title}]({_url(base_url, p.slug + '/')}): {_money(p.price_cents)}. {p.summary[:200]}" for p in ds]
        lines.append("")
    lines += ["## More", "", f"- [Pricing]({_url(base_url, 'pricing/')})", f"- [Compare datasets]({_url(base_url, 'compare/')})",
              f"- [Refund policy]({_url(base_url, 'legal/refunds/')})", f"- [Contact]({_url(base_url, 'contact/')})", ""]
    return "\n".join(lines)


def home_jsonld(base_url: str, site_title: str, email: str = "") -> str:
    home = _url(base_url, "") if base_url else ""
    org: dict[str, Any] = {"@type": "Organization", "name": site_title}
    if home:
        org["url"] = home
    if email:
        org["email"] = email
    graph = [org, {"@type": "WebSite", "name": site_title, **({"url": home} if home else {})}]
    data = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{data}</script>'


def security_txt(email: str, base_url: str, now: datetime) -> str:
    """RFC 9116. "" without a usable contact address (the file would be useless)."""
    email = (email or "").strip()
    if not email or any(c in email for c in "\r\n ") or "@" not in email:
        return ""
    expires = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"Contact: mailto:{email}", f"Expires: {expires}", "Preferred-Languages: en"]
    if base_url:
        lines.append(f"Canonical: {_url(base_url, SECURITY)}")
    return "\n".join(lines) + "\n"
