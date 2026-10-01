"""Site discovery (Phases 225-229): with hundreds of products, visitors need to find the right one.

* **Phase 225, search index:** ``search.json`` lists every product on sale (title, link, price,
  technology, type, rows): small, and cached by the browser.
* **Phase 226, search page:** ``search/`` filters that list as you type, in the browser (no
  server, nothing sent anywhere). Without JavaScript it points to the full catalog.
* **Phase 227, full catalog:** ``catalog/`` lists every product grouped by technology, so every
  product is one click from a crawlable page. Over ``PER_PAGE`` products it continues on
  ``catalog/2/`` and so on.
* **Phase 228, status page:** ``status/`` shows when the data last changed, postings seen in the
  last 7 days, products on sale and which job boards are answering. Only counts and board names:
  nothing private.
* **Phase 229, what's new:** ``changes/`` lists, week by week for the last ``CHANGE_WEEKS`` weeks,
  the products added and retired and the price changes.

The home page links each of these pages that exists (``INDEX_LINKS``).
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

SEARCH_JSON = "search.json"
PER_PAGE = 200
CHANGE_WEEKS = 8
INDEX_LINKS = (("search/index.html", "search/", "Search"), ("catalog/index.html", "catalog/", "All products"),
               ("trends/index.html", "trends/", "Hiring trends"), ("changes/index.html", "changes/", "What's new"),
               ("status/index.html", "status/", "Status"), ("new-employers/index.html", "new-employers/", "Just started hiring"),
               ("vs/index.html", "vs/", "Compare technologies"))


def _money(cents: int) -> str:
    return f"${int(cents) / 100:,.2f}".replace(".00", "")


def factory_meta(state: Any) -> dict[str, dict[str, Any]]:
    from strategies.product_factory import ensure

    ensure(state)
    out = {}
    for r in state._all("SELECT slug, filters, rows FROM factory_products WHERE status = 'live'"):
        f = json.loads(r["filters"])
        out[r["slug"]] = {"tech": f.get("tech") or "", "group": f.get("group") or "", "type": f.get("type", "slice"),
                          "rows": int(r["rows"] or 0)}
    return out


def _entries(pages: list[Any], state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import label

    meta = factory_meta(state)
    out = []
    for p in pages:
        if not getattr(p, "checkout_url", ""):
            continue
        m = meta.get(p.slug, {})
        tech = m.get("tech") or ""
        out.append({"t": p.title, "u": f"{p.slug}/", "p": int(p.price_cents), "k": label(tech) if tech else m.get("group") or "",
                    "y": m.get("type") or p.kind, "r": m.get("rows") or int((p.metrics or {}).get("companies") or 0)})
    return sorted(out, key=lambda e: (e["k"] or "~", e["t"]))


# ------------------------------------------------------------------ Phase 225
def search_index(pages: list[Any], state: Any) -> str:
    return json.dumps(_entries(pages, state), separators=(",", ":"))


# ------------------------------------------------------------------ Phase 226
SEARCH_JS = """(function(){var q=document.getElementById('q'),ul=document.getElementById('results'),
n=document.getElementById('count'),all=[];
function show(){var s=q.value.toLowerCase().trim(),hits=all.filter(function(e){
return !s||(e.t+' '+e.k+' '+e.y).toLowerCase().indexOf(s)>=0;});
while(ul.firstChild){ul.removeChild(ul.firstChild);}
hits.slice(0,100).forEach(function(e){var li=document.createElement('li'),a=document.createElement('a');
a.href='../'+e.u;a.textContent=e.t;li.appendChild(a);
li.appendChild(document.createTextNode(': $'+(e.p/100).toFixed(2).replace('.00','')+(e.r?' · '+e.r+' rows':'')));
ul.appendChild(li);});n.textContent=hits.length+' of '+all.length+' products';
clearTimeout(timer);timer=setTimeout(function(){beacon(hits.length===0);},1500);}
var logUrl=q.getAttribute('data-log'),timer=null,sent={};
var dnt=navigator.doNotTrack==='1'||window.doNotTrack==='1';
function beacon(zero){var s=q.value.toLowerCase().trim();if(!logUrl||dnt||s.length<3||sent[s])return;sent[s]=1;
try{navigator.sendBeacon(logUrl,JSON.stringify({q:s,zero:zero}));}catch(e){}}
fetch('../search.json').then(function(r){return r.json();}).then(function(d){all=d;show();});
q.addEventListener('input',show);
q.addEventListener('keydown',function(ev){if(ev.key==='Enter'){clearTimeout(timer);beacon(!ul.firstChild);}});})();"""


def search_page(shell: Any, log_url: str = "") -> str:
    log = f' data-log="{html.escape(log_url)}"' if log_url else ""  # Phase 351
    body = ('<h1>Search the datasets</h1><p><label for="q">Technology, region or kind of list</label><br>'
            f'<input id="q" type="search" placeholder="e.g. rust, remote, salary" autocomplete="off" style="width:100%"{log}></p>'
            '<p class="muted" id="count" aria-live="polite"></p><ul id="results"></ul>'
            '<noscript><p>Search needs JavaScript: <a href="../catalog/">see every product in the catalog</a>.</p></noscript>'
            f"<script>{SEARCH_JS}</script>")
    return shell("Search the datasets", body, "Search every hiring dataset on sale by technology, region or kind of list.")


# ------------------------------------------------------------------ Phase 227
def catalog_pages(pages: list[Any], state: Any, shell_at: Any) -> dict[str, str]:
    """``shell_at(title, body, desc, depth)``. Returns {path: html}; empty with nothing on sale."""
    entries = _entries(pages, state)
    if not entries:
        return {}
    chunks = [entries[i:i + PER_PAGE] for i in range(0, len(entries), PER_PAGE)]
    out = {}
    for n, chunk in enumerate(chunks, start=1):
        depth = 1 if n == 1 else 2
        up = "../" * depth
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in chunk:
            groups[e["k"] or "Across technologies"].append(e)
        body = [f"<h1>Every dataset ({len(entries)})</h1>"
                + (f"<p class=\"muted\">Page {n} of {len(chunks)}.</p>" if len(chunks) > 1 else "")]
        for tech in sorted(groups, key=lambda k: (k == "Across technologies", k)):
            items = "".join(f'<li><a href="{up}{html.escape(e["u"])}">{html.escape(e["t"])}</a>: {_money(e["p"])}'
                            + (f" · {e['r']} rows" if e["r"] else "") + "</li>" for e in groups[tech])
            body.append(f"<h2>{html.escape(tech)} ({len(groups[tech])})</h2><ul>{items}</ul>")
        nav = []
        if n > 1:
            nav.append(f'<a href="{up}catalog/{"" if n == 2 else f"{n - 1}/"}">← Previous</a>')
        if n < len(chunks):
            nav.append(f'<a href="{up}catalog/{n + 1}/">Next →</a>')
        if nav:
            body.append("<p>" + " · ".join(nav) + "</p>")
        path = "catalog/index.html" if n == 1 else f"catalog/{n}/index.html"
        out[path] = shell_at(f"Every dataset{f' (page {n})' if n > 1 else ''}", "".join(body),
                             f"All {len(entries)} hiring datasets on sale, grouped by technology.", depth)
    return out


# ------------------------------------------------------------------ Phase 228
def status_page(state: Any, cfg: Any, shell: Any) -> str:
    from strategies.b2b_lead_aggregator import POOL_NICHE
    from strategies.job_sources import CREDITS
    from strategies.product_factory import catalog

    last = state.niche_last_seen(POOL_NICHE) or ""
    since = (state.clock() - timedelta(days=7)).isoformat(timespec="seconds")
    seen = int(state._one("SELECT COUNT(*) AS n FROM leads WHERE niche = ? AND last_seen >= ?", (POOL_NICHE, since))["n"])
    on_sale = catalog(state).get("live", 0)
    sources = ((state.get("ops") or {}).get("sources")) or {}
    rows = "".join(f"<li>{html.escape(CREDITS.get(s, (s, ''))[0])}: {'answering' if h.get('ok') else 'not answering lately'}</li>"
                   for s, h in sorted(sources.items()) if s in cfg.lead_sources)
    body = ("<h1>Status</h1><ul>"
            f"<li>Data last updated: {html.escape(last[:16].replace('T', ' ')) + ' UTC' if last else 'not yet'}</li>"
            f"<li>Job postings seen in the last 7 days: {seen:,}</li><li>Products on sale: {on_sale:,}</li></ul>"
            + (f"<h2>Job boards</h2><ul>{rows}</ul>" if rows else "")
            + "<p class=\"muted\">Products are rebuilt from fresh postings every week; downloads always carry the latest "
              "version.</p>")
    return shell("Status", body, "When the hiring data was last updated, how many postings were seen this week and how many "
                                 "datasets are on sale.")


# ------------------------------------------------------------------ Phase 229
def changes(state: Any, weeks: int = CHANGE_WEEKS) -> list[dict[str, Any]]:
    from strategies.money_insight import price_history
    from strategies.product_controls import hidden
    from strategies.product_factory import ensure

    ensure(state)
    unlisted = hidden(state)
    now = state.clock()
    start = now - timedelta(days=7 * weeks)
    out = []
    for i in range(weeks):
        hi = now - timedelta(days=7 * i)
        lo = hi - timedelta(days=7)
        out.append({"from": lo.date().isoformat(), "to": hi.date().isoformat(), "added": [], "retired": [], "prices": [],
                    "updated": []})

    def bucket(when: str) -> dict[str, Any] | None:
        try:
            t = datetime.fromisoformat(when)
        except (TypeError, ValueError):
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=now.tzinfo)
        if t < start or t > now:
            return None
        return out[min(weeks - 1, int((now - t).total_seconds() // (7 * 86400)))]

    for r in state._all("SELECT slug, title, status, published_at, retired_at FROM factory_products "
                        "WHERE published_at IS NOT NULL"):
        b = bucket(r["published_at"])
        if b is not None:  # a retired or hidden product has no page: named, not linked
            b["added"].append((r["slug"] if r["status"] == "live" and r["slug"] not in unlisted else "", r["title"]))
        if r["retired_at"]:
            b = bucket(r["retired_at"])
            if b is not None:
                b["retired"].append(r["title"])
    from strategies.version_diffs import refreshes

    for slug, title, d in refreshes(state, start.isoformat(timespec="seconds")):  # Phase 279
        b = bucket(d["at"])
        if b is not None and slug not in unlisted:
            b["updated"].append((slug, title, d))
    for p in price_history(state, 200):
        b = bucket(p["at"])
        if b is not None:
            b["prices"].append(f"{p['title']}: {_money(p['old_cents'])} → {_money(p['new_cents'])}")
    return out


def changes_page(state: Any, shell: Any) -> str:
    weeks = [w for w in changes(state) if w["added"] or w["retired"] or w["prices"] or w["updated"]]
    if not weeks:
        return ""
    body = ["<h1>What's new</h1><p>New datasets, retired ones and price changes, week by week.</p>"]
    for w in weeks:
        parts = [f"<h2>{html.escape(w['from'])} to {html.escape(w['to'])}</h2>"]
        if w["added"]:
            parts.append(f"<p><b>New ({len(w['added'])}):</b></p><ul>" + "".join(
                f'<li><a href="../{html.escape(slug)}/">{html.escape(title)}</a></li>' if slug else f"<li>{html.escape(title)}</li>"
                for slug, title in w["added"][:50]) + "</ul>")
        if w["updated"]:
            parts.append(f"<p><b>Updated ({len(w['updated'])}):</b></p><ul>" + "".join(
                f'<li><a href="../{html.escape(slug)}/">{html.escape(title)}</a>: +{d["new_rows"]} new rows'
                + (f", +{d['companies_added']} companies" if d.get("companies_added") else "") + "</li>"
                for slug, title, d in w["updated"][:50]) + "</ul>")
        if w["retired"]:
            parts.append(f"<p><b>Retired ({len(w['retired'])}):</b> " + html.escape(", ".join(w["retired"][:30])) + "</p>")
        if w["prices"]:
            parts.append("<p><b>Price changes:</b></p><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in w["prices"][:20])
                         + "</ul>")
        body.append("".join(parts))
    return shell("What's new", "".join(body), "New hiring datasets, retired ones and price changes, week by week.")


def site_files(pages: list[Any], state: Any, cfg: Any, shell_at: Any) -> dict[str, str]:
    """Every discovery page for the site build."""
    shell = lambda title, body, desc: shell_at(title, body, desc, 1)  # noqa: E731
    out = {"status/index.html": status_page(state, cfg, shell)}
    catalog = catalog_pages(pages, state, shell_at)
    if catalog:  # search and its no-JavaScript fallback need something on sale
        out.update(catalog)
        log = f"{cfg.lead_capture_base}/v1/search-log" if cfg.lead_capture_base else ""
        out.update({SEARCH_JSON: search_index(pages, state), "search/index.html": search_page(shell, log)})
    page = changes_page(state, shell)
    if page:
        out["changes/index.html"] = page
    return out
