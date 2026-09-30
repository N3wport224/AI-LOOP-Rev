"""Acquisition attribution: UTM tagging on the way out, channel recovery on the way back in.

Links we publish carry ``utm_source`` / ``utm_medium`` / ``utm_campaign``. Stripe Payment Links
don't hand UTM values back to us, but they do accept a ``client_reference_id`` query parameter,
which is copied onto every Checkout Session (completed *and* abandoned). So:

* landers carry the visitor's UTM values into the checkout link (a small inline script);
* links that go straight to checkout get ``client_reference_id`` added directly;
* webhooks and polling decode it back into ``(channel, campaign)``.

Encoding: ``am--<source>--<campaign>``, lowercase ``[a-z0-9_]`` components (Stripe allows
letters, digits, dashes and underscores, up to 200 characters). The lander script applies
exactly the same cleaning.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PREFIX = "am"
SEP = "--"
MAX_COMPONENT = 60
DIRECT = "direct"
KNOWN_CHANNELS = ("devto", "hashnode", "github", "rss", "hn", "showcase", "email", "site", DIRECT)


def clean(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", (value or "").lower())[:MAX_COMPONENT]


def _with_params(url: str, params: dict[str, str], overwrite: bool = False) -> str:
    if not url:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for k, v in params.items():
        if v and (overwrite or k not in query):
            query[k] = v
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def add_utm(url: str, source: str, medium: str = "syndication", campaign: str = "") -> str:
    """Tag a link to one of our pages. Existing UTM values are kept (first touch wins)."""
    return _with_params(url, {"utm_source": source, "utm_medium": medium, "utm_campaign": campaign})


def parse_utm(url: str) -> dict[str, str]:
    query = dict(parse_qsl(urlsplit(url or "").query))
    return {k: query.get(f"utm_{k}", "") for k in ("source", "medium", "campaign")}


def encode_ref(source: str, campaign: str = "") -> str:
    return f"{PREFIX}{SEP}{clean(source) or DIRECT}{SEP}{clean(campaign)}"


def decode_ref(ref: str | None) -> tuple[str, str]:
    """``client_reference_id`` → (channel, campaign). Anything not ours counts as direct."""
    if not ref or not ref.startswith(PREFIX + SEP):
        return DIRECT, ""
    parts = ref.split(SEP)
    source = parts[1] if len(parts) > 1 and parts[1] else DIRECT
    return source, parts[2] if len(parts) > 2 else ""


def checkout_link(url: str, source: str, campaign: str = "") -> str:
    """A Payment Link URL that attributes the sale to ``source`` without going through a lander."""
    return _with_params(url, {"client_reference_id": encode_ref(source, campaign)})


# Mirrors clean()/encode_ref() for visitors arriving on a lander with UTM parameters.
LANDER_ATTRIBUTION_JS = """(function(){try{var p=new URLSearchParams(location.search),s=p.get('utm_source');
if(!s)return;function c(x){return(x||'').toLowerCase().replace(/[^a-z0-9_]/g,'_').slice(0,%d)}
var ref='%s%s'+c(s)+'%s'+c(p.get('utm_campaign'));
document.querySelectorAll('a[data-checkout]').forEach(function(a){var u=new URL(a.href);
if(!u.searchParams.has('client_reference_id')){u.searchParams.set('client_reference_id',ref);a.href=u.toString();}});
}catch(e){}})();""" % (MAX_COMPONENT, PREFIX, SEP, SEP)
