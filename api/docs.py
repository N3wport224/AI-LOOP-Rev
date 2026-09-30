"""``/docs/api``: human-readable reference generated from the OpenAPI document, with a "Try it"
console. Self-contained on purpose: people paste API keys into this page, so it loads no
third-party JavaScript. The key stays in the page's memory (never stored, never sent anywhere
but this API)."""

from __future__ import annotations

import html
import json
from typing import Any

from api.openapi import ERROR_CODES

CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

CONSOLE_JS = r"""(function(){"use strict";
var f=document.getElementById('try');if(!f)return;
var out=document.getElementById('try-out'),meta=document.getElementById('try-meta');
f.addEventListener('submit',function(ev){ev.preventDefault();
  var key=f.key.value.trim(),ep=f.endpoint.value,url=ep;
  if(ep==='/v1/companies/'){url+=encodeURIComponent(f.domain.value.trim()||'example.com');}
  else if(ep==='/v1/signals'){var p=new URLSearchParams();['tech','intent_tag','min_urgency','since','limit'].forEach(function(n){var v=f[n].value.trim();if(v)p.set(n,v);});
    var qs=p.toString();if(qs)url+='?'+qs;}
  out.textContent='…';meta.textContent='GET '+url;
  fetch(url,{headers:key?{'Authorization':'Bearer '+key}:{}}).then(function(r){
    meta.textContent='GET '+url+'  →  '+r.status+'  ·  remaining today: '+(r.headers.get('X-RateLimit-Remaining')||'?')+
      (r.headers.get('Retry-After')?'  ·  retry after '+r.headers.get('Retry-After')+'s':'');
    return r.text();}).then(function(t){try{out.textContent=JSON.stringify(JSON.parse(t),null,2);}catch(e){out.textContent=t;}})
  .catch(function(e){out.textContent=String(e);});});
})();"""

STYLE = """body{font:15px/1.55 -apple-system,Segoe UI,system-ui,sans-serif;max-width:980px;margin:0 auto;padding:2rem 1rem;color:#111;background:#fff}
code,pre{font:13px ui-monospace,Menlo,monospace}pre{background:#f5f6f8;padding:.8rem;border-radius:8px;overflow:auto}
h1{margin-bottom:.2rem}h2{margin-top:2.2rem;border-bottom:1px solid #e5e5e5;padding-bottom:.3rem}
.method{display:inline-block;font-weight:700;font-size:.8rem;padding:.1rem .45rem;border-radius:4px;background:#1f6feb;color:#fff;margin-right:.4rem}
.method.post{background:#2e7d32}table{border-collapse:collapse;width:100%;font-size:.9rem;margin:.5rem 0}
td,th{border-bottom:1px solid #eee;padding:.35rem .5rem;text-align:left;vertical-align:top}.muted{color:#666}
form#try{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.5rem;align-items:end}
form#try label{display:flex;flex-direction:column;font-size:.85rem;font-weight:600}
form#try input,form#try select{padding:.45rem;border:1px solid #bbb;border-radius:6px;font:inherit}
form#try button{padding:.55rem 1rem;border:0;border-radius:6px;background:#111;color:#fff;font-weight:600}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}pre{background:#1c1c1c}td,th{border-color:#333}.muted{color:#999}}"""


def _params_table(params: list[dict[str, Any]]) -> str:
    if not params:
        return ""
    rows = "".join(
        f"<tr><td><code>{html.escape(p['name'])}</code></td><td>{html.escape(p['in'])}</td>"
        f"<td>{html.escape(str(p['schema'].get('type')))}</td><td>{html.escape(p.get('description', ''))}</td></tr>"
        for p in params)
    return f"<table><tr><th>Parameter</th><th>In</th><th>Type</th><th>Description</th></tr>{rows}</table>"


def render_docs(doc: dict[str, Any], base_url: str) -> str:
    base = base_url.rstrip("/") or ""
    example_base = base or "https://<your API host>"
    sections = []
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            codes = ", ".join(sorted(op["responses"]))
            sections.append(
                f'<h3 id="{html.escape(op["operationId"])}"><span class="method {method}">{method.upper()}</span>'
                f"<code>{html.escape(path)}</code></h3><p>{html.escape(op['summary'])}. {html.escape(op.get('description', ''))}</p>"
                f"{_params_table(op.get('parameters', []))}<p class=\"muted\">Responses: {html.escape(codes)}</p>")
    errors = "".join(f"<tr><td><code>{html.escape(c)}</code></td><td>{html.escape(d)}</td></tr>" for c, d in sorted(ERROR_CODES.items()))
    signal_example = {
        "company_id": "acme", "company": "Acme", "domain": "acme.com", "niches": ["devops-sre"],
        "stack": ["AWS", "Kubernetes", "Terraform"], "intent_tag": "Urgency: High (Cloud Migration)", "intent_level": "High",
        "intent_score": 72, "intent_category": "migration", "commercial_signals": ["Cloud Migration"],
        "migration_path": "On-prem → AWS", "urgency_score": 65, "openings": 3, "open_positions": ["Platform Engineer"],
        "remote_friendly": True, "careers_url": "https://acme.com/careers", "careers_url_verified": True,
        "latest_posted_at": "2026-09-28T10:00:00+00:00",
    }
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(doc['info']['title'])}: documentation</title><style>{STYLE}</style></head><body>
<h1>{html.escape(doc['info']['title'])}</h1>
<p class="muted">Version {html.escape(doc['info']['version'])} · OpenAPI 3.1: <a href="{html.escape(base)}/openapi.json">openapi.json</a>
(import it into Postman, Insomnia or any OpenAPI client generator)</p>
<p>{html.escape(doc['info']['summary'])}</p>

<h2>Quickstart</h2>
<pre>export AM_KEY=am_live_...   # from your welcome email

curl -H "Authorization: Bearer $AM_KEY" \\
  "{html.escape(example_base)}/v1/signals?tech=kubernetes&amp;intent_tag=Cloud%20Migration&amp;min_urgency=60&amp;limit=5"

curl -H "Authorization: Bearer $AM_KEY" "{html.escape(example_base)}/v1/companies/acme.com"

# next page: pass pagination.next_cursor back
curl -H "Authorization: Bearer $AM_KEY" "{html.escape(example_base)}/v1/signals?tech=kubernetes&amp;cursor=eyJv..."

# rotate the key (the old one stops working immediately)
curl -X POST -H "Authorization: Bearer $AM_KEY" "{html.escape(example_base)}/v1/auth/rotate"</pre>

<h2>Authentication, limits and errors</h2>
<p>Send the key as <code>Authorization: Bearer am_live_…</code> or <code>X-API-Key: am_live_…</code>. Never put it in a URL.
Each key has a daily quota (<code>X-RateLimit-Limit</code>, <code>X-RateLimit-Remaining</code>, <code>X-RateLimit-Reset</code> on every
response) and a short-burst limit. A <code>429</code> always carries <code>Retry-After</code> in seconds. If a subscription payment
fails, the key keeps working on a reduced quota while Stripe retries the card; it's revoked when the subscription ends.</p>
<table><tr><th>error.code</th><th>Meaning</th></tr>{errors}</table>
<pre>{html.escape(json.dumps({"error": {"code": "invalid_parameter", "message": "min_urgency must be between 0 and 100",
                                   "status": 400, "param": "min_urgency", "doc_url": f"{base}/docs/api#errors",
                                   "request_id": "req_4f1c..."}}, indent=2))}</pre>

<h2>Endpoints</h2>
{''.join(sections)}

<h2>Signal object</h2>
<pre>{html.escape(json.dumps(signal_example, indent=2, ensure_ascii=False))}</pre>

<h2>Try it</h2>
<form id="try" autocomplete="off">
<label>API key<input name="key" type="password" placeholder="am_live_…" required></label>
<label>Endpoint<select name="endpoint"><option value="/v1/signals">GET /v1/signals</option>
<option value="/v1/companies/">GET /v1/companies/{{domain}}</option><option value="/v1/me">GET /v1/me</option></select></label>
<label>tech<input name="tech" placeholder="Kubernetes"></label>
<label>intent_tag<input name="intent_tag" placeholder="Cloud Migration"></label>
<label>min_urgency<input name="min_urgency" inputmode="numeric" placeholder="60"></label>
<label>since<input name="since" placeholder="2026-09-01"></label>
<label>limit<input name="limit" inputmode="numeric" placeholder="5"></label>
<label>domain<input name="domain" placeholder="acme.com"></label>
<button type="submit">Send request</button></form>
<p class="muted" id="try-meta"></p><pre id="try-out">Responses appear here. Your key stays in this page and goes only to this API.</pre>
<script src="/docs/api/console.js"></script>
</body></html>
"""
