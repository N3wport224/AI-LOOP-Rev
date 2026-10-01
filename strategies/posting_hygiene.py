"""Posting hygiene (Phases 290-294): tidy rows, no personal data, no dead links.

Applied to the postings every factory product is built from (``clean``), so new products and weekly
refreshes get them:

* **Phase 290, tidy titles:** "Sr." becomes "Senior", "Jr." "Junior", gender tags like "(m/w/d)" and
  trailing " - Remote" or " | Berlin" go, and SHOUTED titles are put in title case.
* **Phase 291, no personal emails:** ``contact_email`` is kept only when it's a role mailbox
  (``jobs@``, ``careers@``, ``hiring@``...). A person's address is removed: the datasets list
  companies and roles, not people.
* **Phase 292, clean links:** tracking parameters (``utm_*``, ``ref``, ``source``, ``gclid``...) and
  fragments are removed from posting links.
* **Phase 293, one posting, one row:** the same posting found on two job boards (same link, or the
  same company, title and location from different boards) is kept once: the copy with a salary, or
  the newer one.
* **Phase 294, dead links** (``link_checks``): once a day the factory checks ``LINK_CHECKS_PER_DAY`` posting links
  (HEAD request, robots.txt respected, within the API budget). A link that answers 404 or 410 marks
  its posting as gone, and it's left out from then on.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

LINK_CHECKS_PER_DAY = 20
DEAD = "dead_postings"
CHECKED = "link_check_at"
CHECKED_URLS = "link_checked_urls"
ROLE_MAILBOXES = ("jobs", "job", "careers", "career", "hiring", "recruiting", "recruitment", "talent", "hr", "people",
                  "apply", "applications", "work", "join", "joinus", "team", "hello", "info", "contact")
TRACKING = re.compile(r"^(utm_\w+|ref|refs|source|src|gclid|fbclid|mc_cid|mc_eid|trk|tracking|campaign)$", re.I)
_GENDER = re.compile(r"\s*\((?:[mwfdxh]\s*/\s*){1,3}[mwfdxh]\)|\s*\((?:all genders|gn)\)", re.I)
_TAIL = re.compile(r"\s+[-|–]\s+([^-|–]{2,40})$")
_WORK_MODE = ("remote", "hybrid", "on-site", "onsite", "on site", "fully remote")


# ------------------------------------------------------------------ Phase 290
def tidy_title(title: str) -> str:
    t = " ".join(str(title or "").split())
    t = _GENDER.sub("", t)
    tail = _TAIL.search(t)
    if tail:  # only a place or a work mode: "- Remote", "| Berlin"; "- Payments Platform" stays
        from strategies.kinds_countries import country_of

        place = tail.group(1).strip()
        if place.lower() in _WORK_MODE or country_of({"location": place}):
            t = t[: tail.start()]
    t = re.sub(r"\bSr\.?(?=\s|$)", "Senior", t, flags=re.I)
    t = re.sub(r"\bJr\.?(?=\s|$)", "Junior", t, flags=re.I)
    letters = [c for c in t if c.isalpha()]
    if len(letters) > 6 and all(c.isupper() for c in letters):
        t = " ".join(w if len(w) <= 3 else w.capitalize() for w in t.lower().split())
    return t.strip(" -|")


# ------------------------------------------------------------------ Phase 291
def role_mailbox(email: str) -> bool:
    local = str(email or "").split("@", 1)[0].lower()
    return bool(local) and re.split(r"[.+_-]", local)[0] in ROLE_MAILBOXES


# ------------------------------------------------------------------ Phase 292
def clean_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return str(url or "")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))


# ------------------------------------------------------------------ Phase 293
def dedupe(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from strategies.posting_quality import company_key

    def better(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        sa, sb = bool(a.get("salary_min")), bool(b.get("salary_min"))
        if sa != sb:
            return a if sa else b
        return a if str(a.get("posted_at") or "") >= str(b.get("posted_at") or "") else b

    by_url: dict[str, dict[str, Any]] = {}
    rest = []
    for lead in leads:
        url = lead.get("url") or ""
        if url and url in by_url:
            by_url[url] = better(by_url[url], lead)
        elif url:
            by_url[url] = lead
        else:
            rest.append(lead)
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], tuple[int, set[str]]] = {}
    for lead in list(by_url.values()) + rest:
        key = (company_key(lead.get("company") or ""), str(lead.get("title") or "").lower(), str(lead.get("location") or "").lower())
        source = str(lead.get("source") or "")
        if key in seen and source not in seen[key][1]:  # the same posting, found on another board
            i, sources = seen[key]
            out[i] = better(out[i], lead)
            sources.add(source)
            continue
        if key not in seen:  # (a second opening on the same board is its own row)
            seen[key] = (len(out), {source})
        out.append(lead)
    return out


def clean(state: Any, leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dead = set(state.get(DEAD) or [])
    out = []
    for lead in leads:
        if str(lead.get("dedupe_key")) in dead:  # Phase 294
            continue
        row = dict(lead)
        row["title"] = tidy_title(row.get("title") or "")
        if row.get("contact_email") and not role_mailbox(row["contact_email"]):
            row["contact_email"] = ""
        if row.get("url"):
            row["url"] = clean_url(row["url"])
        out.append(row)
    return dedupe(out)


# ------------------------------------------------------------------ Phase 294
def check_links(tools: Any) -> int:
    """Daily: HEAD a few posting links used by products on sale. Returns how many were dead."""
    from strategies.b2b_lead_aggregator import POOL_NICHE
    from tools.errors import CircuitOpenError
    from tools.http_client import HttpError

    state = tools.state
    if not getattr(tools.config, "link_checks", True):
        return 0
    last = state.get(CHECKED)
    if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=24):
        return 0
    state.set(CHECKED, state.now())
    checked = set(state.get(CHECKED_URLS) or [])
    dead = set(state.get(DEAD) or [])
    todo = [(lead["dedupe_key"], lead["url"]) for lead in state.leads_for_niche(POOL_NICHE)
            if lead.get("url") and lead["url"] not in checked and str(lead["dedupe_key"]) not in dead][:LINK_CHECKS_PER_DAY]
    found = 0
    for key, url in todo:
        checked.add(url)
        try:
            tools.http.request("HEAD", url, attempts=1)
        except CircuitOpenError:
            break
        except HttpError as exc:
            if exc.status in (404, 410):
                dead.add(str(key))
                found += 1
        except Exception:  # noqa: BLE001 - robots.txt, timeouts, odd servers: not evidence the posting is gone
            continue
    state.set(CHECKED_URLS, sorted(checked)[-5000:])
    state.set(DEAD, sorted(dead)[-20000:])
    return found
