"""Embeddable product cards (Phases 300-304): put a dataset on any page with two lines of HTML.

* **Phase 300, the data:** the card reads ``search.json`` (Phase 225), already on the site: title,
  price, rows and link of every product on sale. Nothing new to keep up to date.
* **Phase 301, the script:** ``widget.js`` at the site root turns every
  ``<div data-am-product="SLUG"></div>`` on a page into a small card: title, rows, price and a
  "See the dataset" link. It builds the card from DOM nodes and text only (no HTML from data), and
  uses no cookies or storage.
* **Phase 302, how to embed:** ``embed/products/`` shows the two lines for the best sellers and
  newest products, ready to copy.
* **Phase 303, affiliates get credit:** ``data-am-ref="CODE"`` adds the affiliate's tracking to the
  card's link (the same ``utm`` tags as affiliate links), so sales through the card are theirs.
  Without it the link is tagged ``utm_source=widget``.
* **Phase 304, small and quiet:** the script stays under ``MAX_BYTES`` (4 KB) and makes one request
  (to ``search.json`` on this site); a test enforces both.
"""

from __future__ import annotations

import html
from typing import Any

SCRIPT = "widget.js"
PAGE = "embed/products/index.html"
MAX_BYTES = 4096
SHOW = 12

WIDGET_JS = r"""(function(){
var me=document.currentScript,base=me&&me.src?me.src.replace(/widget\.js(\?.*)?$/,''):'';
var nodes=document.querySelectorAll('[data-am-product]');if(!nodes.length||!base)return;
function el(t,s,txt){var e=document.createElement(t);if(s)e.setAttribute('style',s);if(txt)e.textContent=txt;return e;}
function link(e,ref){var u=new URL(base+e.u);
if(ref){u.searchParams.set('utm_source','aff');u.searchParams.set('utm_medium','affiliate');u.searchParams.set('utm_campaign',ref);}
else{u.searchParams.set('utm_source','widget');u.searchParams.set('utm_medium','embed');u.searchParams.set('utm_campaign',location.hostname);}
return u.toString();}
fetch(base+'search.json').then(function(r){return r.json();}).then(function(all){
var by={};all.forEach(function(e){by[e.u.replace(/\/$/,'')]=e;});
nodes.forEach(function(n){var e=by[n.getAttribute('data-am-product')];if(!e)return;
var box=el('div','border:1px solid #ddd;border-radius:8px;padding:12px 14px;max-width:420px;font:14px/1.4 system-ui,sans-serif');
box.appendChild(el('div','font-weight:600;margin-bottom:4px',e.t));
box.appendChild(el('div','color:#555;margin-bottom:8px',(e.r?e.r+' rows \u00b7 ':'')+'$'+(e.p/100).toFixed(2).replace('.00','')));
var a=el('a','display:inline-block;padding:6px 12px;border-radius:6px;background:#1f6feb;color:#fff;text-decoration:none','See the dataset');
a.href=link(e,n.getAttribute('data-am-ref'));a.rel='noopener';a.target='_blank';box.appendChild(a);
while(n.firstChild)n.removeChild(n.firstChild);n.appendChild(box);});}).catch(function(){});
})();
"""


def snippet(cfg: Any, slug: str, ref: str = "") -> str:
    base = str(cfg.pages_base_url or "").rstrip("/")
    attr = f' data-am-ref="{html.escape(ref)}"' if ref else ""
    return f'<div data-am-product="{html.escape(slug)}"{attr}></div>\n<script src="{html.escape(base)}/widget.js" async></script>'


def embed_page(state: Any, cfg: Any, pages: list[Any], shell_at: Any) -> str:
    from strategies.sales_channels import leaderboard

    on_sale = {p.slug: p.title for p in pages if getattr(p, "checkout_url", "")}
    if not on_sale or not cfg.pages_base_url:
        return ""
    best = [r["niche"] for r in leaderboard(state) if r["orders"] and r["niche"] in on_sale]
    picks = list(dict.fromkeys(best + sorted(on_sale)))[:SHOW]
    items = "".join(f"<h3>{html.escape(on_sale[s])}</h3><pre><code>{html.escape(snippet(cfg, s))}</code></pre>" for s in picks)
    body = ("<h1>Embed a dataset card</h1><p>Paste two lines into any page and a small card shows the dataset's title, size "
            "and price, with a link to it. It loads one small script from this site and uses no cookies.</p>"
            "<p>Affiliates: add <code>data-am-ref=\"YOUR-CODE\"</code> to the first line and sales through the card are "
            f"credited to you.</p>{items}")
    return shell_at("Embed a dataset card", body, "Two lines of HTML put a hiring dataset card on any page.", 2)


def site_files(state: Any, cfg: Any, pages: list[Any], shell_at: Any) -> dict[str, str]:
    page = embed_page(state, cfg, pages, shell_at)
    return {SCRIPT: WIDGET_JS, PAGE: page} if page else {}
