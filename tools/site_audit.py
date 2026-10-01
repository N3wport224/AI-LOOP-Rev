"""Site audit (Phase 65): check every built page before it goes public.

Run on the files the site builder produced (no network), each time the site is built:

* **Broken internal links:** an ``href``/``src`` pointing at a page or file of the site that wasn't
  built (relative links and links to ``pages_base_url`` both count; Stripe, the tunnel and other
  sites are not checked here).
* **SEO basics:** a missing ``<title>``, a meta description missing or outside 50-160 characters,
  no ``<h1>`` or more than one, images without ``alt`` text.
* **Weight:** pages over 500 KB load slowly on phones.
* **Accessibility** (Phase 118): no ``lang`` on ``<html>``, form fields without a label (a
  ``<label for>``, a wrapping ``<label>``, ``aria-label`` or ``title``), and links or buttons with
  no text a screen reader could announce. Reported under ``a11y``.

Findings go to kv ``site_audit``. New broken links raise one alert; the rest shows in
``automonetize doctor``.
"""

from __future__ import annotations

import posixpath
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

MAX_PAGE_BYTES = 500 * 1024
DESC_MIN, DESC_MAX = 50, 160


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title = False
        self.description: str | None = None
        self.h1 = 0
        self.img_without_alt = 0
        self.noindex = False
        self.lang = False
        self.label_for: set[str] = set()
        self.in_label = 0
        self.fields: list[tuple[str, bool]] = []  # (id, labelled by itself)
        self.open: list[dict[str, Any]] = []      # <a>/<button> being read: {"tag", "named"}
        self.unnamed: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self._a11y_start(tag, a)
        if tag == "title":
            self.title = True
        elif tag == "h1":
            self.h1 += 1
        elif tag == "meta" and (a.get("name") or "").lower() == "description":
            self.description = a.get("content") or ""
        elif tag == "meta" and (a.get("name") or "").lower() == "robots" and "noindex" in (a.get("content") or ""):
            self.noindex = True
        elif tag == "img" and not (a.get("alt") or "").strip():
            self.img_without_alt += 1
        for key in ("href", "src"):
            if a.get(key) and tag in ("a", "link", "img", "script"):
                self.links.append(str(a[key]))

    # -- accessibility (Phase 118) ------------------------------------------------------------------
    def _a11y_start(self, tag: str, a: dict[str, str | None]) -> None:
        if tag == "html" and (a.get("lang") or "").strip():
            self.lang = True
        elif tag == "label":
            self.in_label += 1
            if a.get("for"):
                self.label_for.add(str(a["for"]))
        elif tag in ("input", "select", "textarea") and (a.get("type") or "").lower() not in ("hidden", "submit", "button", "reset"):
            own = bool(self.in_label or (a.get("aria-label") or "").strip() or (a.get("title") or "").strip()
                       or a.get("aria-labelledby"))
            self.fields.append((str(a.get("id") or ""), own))
        elif tag in ("a", "button") and (tag == "button" or a.get("href") is not None):
            self.open.append({"tag": tag, "named": bool((a.get("aria-label") or "").strip() or (a.get("title") or "").strip())})
        elif tag == "img" and self.open and (a.get("alt") or "").strip():
            self.open[-1]["named"] = True

    def handle_data(self, data: str) -> None:
        if self.open and data.strip():
            self.open[-1]["named"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self.in_label:
            self.in_label -= 1
        elif tag in ("a", "button") and self.open and self.open[-1]["tag"] == tag:
            item = self.open.pop()
            if not item["named"]:
                self.unnamed.append(tag)
            elif self.open:
                self.open[-1]["named"] = True

    def a11y(self) -> list[str]:
        out = [] if self.lang else ["no lang on <html>"]
        unlabelled = sum(1 for fid, own in self.fields if not own and fid not in self.label_for)
        if unlabelled:
            out.append(f"{unlabelled} form field(s) without a label")
        for tag in ("a", "button"):
            n = self.unnamed.count(tag)
            if n:
                out.append(f"{n} {'link' if tag == 'a' else 'button'}(s) with no text")
        return out


def _target(page: str, link: str, base_url: str) -> str | None:
    """The site file a link points to, or None if it isn't one of ours."""
    link = link.strip()
    if not link or link.startswith(("#", "mailto:", "javascript:", "data:", "tel:")):
        return None
    parts = urlsplit(link)
    if parts.scheme or parts.netloc:
        if not base_url:
            return None
        base = urlsplit(base_url)
        prefix = base.path.rstrip("/")
        if parts.netloc != base.netloc or not (parts.path == prefix or parts.path.startswith(prefix + "/")):
            return None
        path = parts.path[len(prefix):].lstrip("/")
        trailing = parts.path.endswith("/") or not path
    else:
        if not parts.path:
            return None
        joined = parts.path.lstrip("/") if parts.path.startswith("/") else posixpath.join(posixpath.dirname(page), parts.path)
        path = posixpath.normpath(joined) if joined else ""
        path = "" if path == "." else path
        trailing = parts.path.endswith("/")
    if path.startswith(".."):
        return path  # above the site root: never a built file, so it's reported
    if trailing or not path:
        path = f"{path.rstrip('/')}/index.html" if path else "index.html"
    return path


def audit_site(out: dict[str, str | bytes], base_url: str = "") -> dict[str, list[str]]:
    broken: list[str] = []
    seo: list[str] = []
    heavy: list[str] = []
    a11y: list[str] = []
    files = set(out)
    for rel, content in sorted(out.items()):
        if isinstance(content, bytes):
            continue
        if len(content.encode()) > MAX_PAGE_BYTES and rel.endswith(".html"):
            heavy.append(f"{rel} is {len(content.encode()) // 1024} KB")
        if not rel.endswith(".html"):
            continue
        scan = _Scan()
        scan.feed(content)
        a11y += [f"{rel}: {problem}" for problem in scan.a11y()]
        for link in scan.links:
            target = _target(rel, link, base_url)
            if target and target not in files and target.split("?")[0] not in files:
                broken.append(f"{rel} → {link}")
        if scan.noindex:
            continue  # private pages (thank-you) need no SEO
        if not scan.title:
            seo.append(f"{rel}: no <title>")
        if scan.description is None:
            seo.append(f"{rel}: no meta description")
        elif not DESC_MIN <= len(scan.description) <= DESC_MAX:
            seo.append(f"{rel}: meta description is {len(scan.description)} characters (best {DESC_MIN}-{DESC_MAX})")
        if scan.h1 != 1:
            seo.append(f"{rel}: {scan.h1} <h1> headings (best: exactly one)")
        if scan.img_without_alt:
            seo.append(f"{rel}: {scan.img_without_alt} image(s) without alt text")
    return {"broken_links": sorted(set(broken)), "seo": seo, "heavy": heavy, "a11y": a11y}


def record(state: Any, report: dict[str, list[str]]) -> list[str]:
    """Store the report; return broken links that are new since the last audit."""
    previous = set((state.get("site_audit") or {}).get("broken_links") or [])
    state.set("site_audit", {"at": state.now(), **report})
    new = [b for b in report["broken_links"] if b not in previous]
    if new:
        state.log_error("site_audit", f"{len(new)} broken link(s) on your site, e.g. {new[0]}"
                                      + (f" (+{len(new) - 1} more)" if len(new) > 1 else ""), kind="alert")
    return new

