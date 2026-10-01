"""A fast, light site at any size (Phases 310-314). Applied to every site build, before it's checked
and published.

* **Phase 310, smaller pages:** HTML pages lose the indentation and blank lines between tags
  (``minify``). Text inside ``pre``, ``textarea``, ``script`` and ``style`` is left exactly as it is.
* **Phase 311, a size budget per page:** a page over ``PAGE_BUDGET_KB`` (200 KB) is named in the site
  audit and raises one alert a day: usually a list that should be split.
* **Phase 312, faster checkout:** pages with a Stripe checkout link tell the browser to connect to
  Stripe early (``preconnect``), so the checkout opens a little sooner.
* **Phase 313, sitemaps that scale:** past ``MAX_URLS`` (45,000) addresses the sitemap is split into
  ``sitemap-1.xml``, ``sitemap-2.xml``... and ``sitemap.xml`` becomes their index (search engines
  read at most 50,000 per file).
* **Phase 314, the whole site:** total size and file count are kept with the site audit; past
  ``SITE_WARN_MB`` (800 MB, GitHub Pages allows 1 GB) or ``FILES_WARN`` files you get one alert a day.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

PAGE_BUDGET_KB = 200
MAX_URLS = 45000
SITE_WARN_MB = 800
FILES_WARN = 50000
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_PROTECTED = re.compile(r"(<(pre|textarea|script|style)\b.*?</\2>)", re.S | re.I)
PRECONNECT = '<link rel="preconnect" href="https://buy.stripe.com">'


# ------------------------------------------------------------------ Phase 310
def minify(page: str) -> str:
    """Drop indentation and blank lines outside pre/textarea/script/style (whitespace there matters)."""
    parts = _PROTECTED.split(page)  # text, block, tag name, text, block, tag name, ..., text
    out = []
    for i, chunk in enumerate(parts):
        if i % 3 == 0:
            out.append(re.sub(r"\n\s*\n+", "\n", re.sub(r"\n[ \t]+", "\n", chunk)))
        elif i % 3 == 1:
            out.append(chunk)
    return "".join(out)


# ------------------------------------------------------------------ Phase 312
def preconnect(page: str) -> str:
    if "https://buy.stripe.com/" not in page or PRECONNECT in page or "<head>" not in page:
        return page
    return page.replace("<head>", "<head>" + PRECONNECT, 1)


# ------------------------------------------------------------------ Phase 313
def split_sitemap(out: dict[str, Any], base_url: str) -> int:
    """Returns how many sitemap files there are now (1 = unchanged)."""
    text = out.get("sitemap.xml")
    if not isinstance(text, str):
        return 0
    root = ET.fromstring(text.split("\n", 1)[1] if text.startswith("<?xml") else text)
    urls = root.findall(f"{{{NS}}}url")
    if len(urls) <= MAX_URLS:
        return 1
    ET.register_namespace("", NS)
    parts = [urls[i:i + MAX_URLS] for i in range(0, len(urls), MAX_URLS)]
    index = ET.Element(f"{{{NS}}}sitemapindex")
    base = str(base_url or "").rstrip("/")
    for n, chunk in enumerate(parts, start=1):
        urlset = ET.Element(f"{{{NS}}}urlset")
        urlset.extend(chunk)
        out[f"sitemap-{n}.xml"] = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(urlset, encoding="unicode") + "\n"
        ET.SubElement(ET.SubElement(index, f"{{{NS}}}sitemap"), f"{{{NS}}}loc").text = f"{base}/sitemap-{n}.xml"
    out["sitemap.xml"] = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(index, encoding="unicode") + "\n"
    return len(parts)


# ------------------------------------------------------------------ all together, plus 311 and 314
def optimise(out: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Minify and preconnect HTML in place, split the sitemap; returns the size report."""
    saved = 0
    for rel, body in list(out.items()):
        if rel.endswith(".html") and isinstance(body, str):
            small = minify(body)
            saved += len(body.encode()) - len(small.encode())
            out[rel] = preconnect(small)
    sitemaps = split_sitemap(out, base_url)
    sizes = {rel: len(b if isinstance(b, bytes) else str(b).encode()) for rel, b in out.items()}
    big = sorted(((rel, n // 1024) for rel, n in sizes.items() if rel.endswith(".html") and n > PAGE_BUDGET_KB * 1024),
                 key=lambda x: -x[1])
    return {"files": len(sizes), "mb": round(sum(sizes.values()) / 1e6, 1), "saved_kb": saved // 1024,
            "over_budget": big[:10], "sitemaps": sitemaps}


def check(state: Any, report: dict[str, Any]) -> list[str]:
    from agent.ops_checks import _daily

    notes = []
    if report["over_budget"]:
        rel, kb = report["over_budget"][0]
        notes.append(f"{len(report['over_budget'])} page(s) over {PAGE_BUDGET_KB} KB, e.g. {rel} ({kb} KB)")
    if report["mb"] > SITE_WARN_MB or report["files"] > FILES_WARN:
        notes.append(f"the site is {report['mb']:,} MB in {report['files']:,} files (GitHub Pages allows 1 GB)")
    state.set("site_size", {"at": state.now(), **report})
    if notes and _daily(state, "site_size"):
        state.log_error("site", "Website: " + "; ".join(notes) + ".", kind="alert")
    return notes
