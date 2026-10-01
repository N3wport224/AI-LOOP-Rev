"""What else goes in every dataset zip (Phases 75-77).

* **QUALITY.md** (Phase 75): an honest coverage report: rows, companies, how many rows have a
  company domain / a job link / a salary, remote share, how fresh the postings are, which sources
  they came from, and (with intel) how many careers pages were verified live. Buyers can see exactly
  what they're getting, and it's all computed from the file itself.
* **FIELDS.md** (Phase 76): a data dictionary: what every column means and its format.
* **leads-excel.csv** (Phase 77): the same rows with a UTF-8 byte-order mark, so Excel shows accented
  company names correctly when the file is double-clicked (``leads.csv`` stays plain UTF-8 for
  Google Sheets, pandas and databases).
* **CHANGES.md** (Phase 95): which companies are new since the previous version (and which left), so
  update buyers and subscribers see the difference at a glance.
* **TOP20.md** (Phase 96): the 20 companies to call first: highest hiring urgency (or most open roles
  when there's no intel), with what they run.
* **regions/** (Phase 97): ``leads-us.csv``, ``leads-europe.csv``, ``leads-remote.csv``: the same rows
  split by where the job is, from the posted location (rows that say nothing are only in the main file).
* **leads.jsonl** and **schema.sql** (Phase 98): one JSON object per line for scripts, and a
  ``CREATE TABLE`` + ``INSERT`` file that loads into SQLite or Postgres as is.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from statistics import median
from typing import Any

FIELD_DOCS = {
    "company": "Hiring company's name as posted.",
    "title": "Job title as posted.",
    "location": "Location as posted (may be a region or 'Remote').",
    "remote": "true if the posting says the role is remote.",
    "seniority": "Seniority inferred from the title (junior, mid, senior, lead, ...), blank if unclear.",
    "stack": "Technologies mentioned in the posting, comma-separated.",
    "tags": "The job board's own tags, comma-separated.",
    "salary_min": "Lowest salary in the posting, as posted (often yearly, in the posting's currency); blank if none.",
    "salary_max": "Highest salary in the posting; blank if none.",
    "company_domain": "The company's website domain, when known.",
    "contact_email": "A contact address published in the posting itself, when there is one (never guessed).",
    "url": "Link to the original posting.",
    "posted_at": "When the job was posted (ISO 8601, UTC).",
    "source": "Which public job board the posting came from.",
}
INTEL_DOCS = {
    "company": "Company name (one row per company).",
    "domain": "Company website domain.",
    "intent_tag": "The main buying signal behind the hiring (e.g. cloud migration, data platform build-out).",
    "urgency_score": "0-100: how urgently they're hiring (number of roles, recency, repeated postings).",
    "intent_signals": "Signals detected across their postings.",
    "stack": "Technologies across all their postings.",
    "open_positions": "Number of open roles found.",
    "careers_url_verified": "true if their careers page was checked and answered.",
}


def _pct(n: int, total: int) -> str:
    return f"{n * 100 // total}%" if total else "0%"


def quality_md(niche: str, leads: list[dict[str, Any]], intel: list[dict[str, Any]], now: datetime) -> str:
    total = len(leads)
    companies = len({str(r.get("company") or "").strip().lower() for r in leads if r.get("company")})
    ages = []
    for r in leads:
        try:
            posted = datetime.fromisoformat(str(r.get("posted_at") or "").replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            ages.append((now - posted).days)
        except ValueError:
            continue
    sources = Counter(str(r.get("source") or "unknown") for r in leads).most_common(6)
    lines = [f"# Data quality: {niche}", "", f"Built {now:%Y-%m-%d %H:%M} UTC from the rows in this file.", "",
             "| Measure | Value |", "|---|---|",
             f"| Job postings (rows) | {total} |", f"| Distinct companies | {companies} |",
             f"| Rows with a company domain | {_pct(sum(1 for r in leads if r.get('company_domain')), total)} |",
             f"| Rows with a link to the posting | {_pct(sum(1 for r in leads if r.get('url')), total)} |",
             f"| Rows with a salary | {_pct(sum(1 for r in leads if r.get('salary_min') or r.get('salary_max')), total)} |",
             f"| Rows with a tech stack | {_pct(sum(1 for r in leads if r.get('stack')), total)} |",
             f"| Remote roles | {_pct(sum(1 for r in leads if str(r.get('remote')).lower() in ('true', '1', 'yes')), total)} |"]
    if ages:
        lines.append(f"| Median posting age | {int(median(ages))} days |")
        lines.append(f"| Posted in the last 7 days | {_pct(sum(1 for a in ages if a <= 7), len(ages))} |")
    if intel:
        lines.append(f"| Company profiles (tech radar) | {len(intel)} |")
        lines.append(f"| Careers pages verified live | {_pct(sum(1 for r in intel if r.get('careers_url_verified')), len(intel))} |")
    lines += ["", "## Sources", "", *[f"- {s}: {n} row(s)" for s, n in sources], "",
              "Every row links to a public posting. Nothing is estimated or padded: blank means the posting didn't say.", ""]
    return "\n".join(lines)


def fields_md(intel: bool) -> str:
    lines = ["# Fields", "", "## leads.csv / leads.json (one row per job posting)", "", "| Column | Meaning |", "|---|---|"]
    lines += [f"| `{k}` | {v} |" for k, v in FIELD_DOCS.items()]
    if intel:
        lines += ["", "## tech_radar.csv / tech_radar.json (one row per company)", "", "| Column | Meaning |", "|---|---|"]
        lines += [f"| `{k}` | {v} |" for k, v in INTEL_DOCS.items()]
    lines += ["", "`leads-excel.csv` is `leads.csv` with a byte-order mark so Excel opens it correctly.", ""]
    return "\n".join(lines)


def excel_csv(csv_bytes: bytes) -> bytes:
    bom = "﻿".encode()
    return csv_bytes if csv_bytes.startswith(bom) else bom + csv_bytes


def _company(r: dict[str, Any]) -> str:
    return " ".join(str(r.get("company") or "").split())


def changes_md(niche: str, version: int, leads: list[dict[str, Any]], previous: list[dict[str, Any]] | None) -> str:
    """Phase 95: companies added and gone since the previous version."""
    if previous is None:
        return f"# Changes: {niche} v{version}\n\nFirst release: every company is new.\n"
    now = {_company(r).lower(): _company(r) for r in leads if _company(r)}
    before = {_company(r).lower(): _company(r) for r in previous if _company(r)}
    added = sorted(now[k] for k in now.keys() - before.keys())
    gone = sorted(before[k] for k in before.keys() - now.keys())
    lines = [f"# Changes: {niche} v{version}", "", f"Compared with v{version - 1}: {len(leads)} rows now, {len(previous)} before.",
             "", f"## New companies ({len(added)})", ""]
    lines += [f"- {c}" for c in added[:300]] or ["- none"]
    lines += ["", f"## No longer hiring here ({len(gone)})", ""] + ([f"- {c}" for c in gone[:300]] or ["- none"]) + [""]
    return "\n".join(lines)


def _roles(r: dict[str, Any]) -> int:
    """Open roles: intel stores a list of positions, older records a count."""
    v = r.get("open_positions") or r.get("openings") or 0
    return len(v) if isinstance(v, (list, tuple)) else int(v or 0)


def top20_md(niche: str, leads: list[dict[str, Any]], intel: list[dict[str, Any]]) -> str:
    """Phase 96: the companies to contact first."""
    lines = [f"# Top 20 companies to watch: {niche}", ""]
    if intel:
        ranked = sorted(intel, key=lambda r: (-(r.get("urgency_score") or 0), -_roles(r)))[:20]
        lines += ["Ranked by hiring urgency (0-100).", "", "| # | Company | Urgency | Open roles | Stack |", "|---|---|---|---|---|"]
        for i, r in enumerate(ranked, 1):
            stack = r.get("stack") or []
            stack = ", ".join(stack[:6]) if isinstance(stack, list) else str(stack)[:80]
            lines.append(f"| {i} | {_company(r)} | {r.get('urgency_score', '')} | {_roles(r) or ''} "
                         f"| {stack} |")
    else:
        counts = Counter(_company(r) for r in leads if _company(r))
        lines += ["Ranked by open roles in this dataset.", "", "| # | Company | Open roles |", "|---|---|---|"]
        lines += [f"| {i} | {c} | {n} |" for i, (c, n) in enumerate(counts.most_common(20), 1)]
    return "\n".join(lines) + "\n"


US = ("united states", "usa", "u.s.", " us", "us-", "new york", "san francisco", "california", "texas", "seattle", "boston",
      "chicago", "austin", "remote - us", "remote (us")
EUROPE = ("europe", " eu", "emea", "united kingdom", " uk", "london", "germany", "berlin", "france", "paris", "netherlands",
          "amsterdam", "spain", "madrid", "ireland", "dublin", "sweden", "poland", "portugal", "lisbon", "italy", "switzerland")


def region_of(row: dict[str, Any]) -> set[str]:
    loc = " " + str(row.get("location") or "").lower()
    out = set()
    if any(k in loc for k in US):
        out.add("us")
    if any(k in loc for k in EUROPE):
        out.add("europe")
    if str(row.get("remote")).lower() in ("true", "1", "yes") or "remote" in loc or "anywhere" in loc:
        out.add("remote")
    return out


def region_split(leads: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Phase 97: rows per region (a row can be in several, e.g. remote-US)."""
    out: dict[str, list[dict[str, Any]]] = {"us": [], "europe": [], "remote": []}
    for r in leads:
        for region in region_of(r):
            out[region].append(r)
    return out


def jsonl(leads: list[dict[str, Any]], fields: list[str]) -> str:
    import json

    return "".join(json.dumps({f: r.get(f) for f in fields}, default=str, ensure_ascii=False) + "\n" for r in leads)


def schema_sql(leads: list[dict[str, Any]], fields: list[str], table: str = "leads") -> str:
    """Phase 98: portable SQL (SQLite/Postgres): every value as text, quotes doubled."""
    def lit(v: Any) -> str:
        if v is None or v == "":
            return "NULL"
        text = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        return "'" + text.replace("'", "''") + "'"

    cols = ", ".join(f'"{f}" TEXT' for f in fields)
    lines = [f'CREATE TABLE IF NOT EXISTS "{table}" ({cols});']
    names = ", ".join(f'"{f}"' for f in fields)
    lines += [f'INSERT INTO "{table}" ({names}) VALUES ({", ".join(lit(r.get(f)) for f in fields)});' for r in leads]
    return "\n".join(lines) + "\n"
