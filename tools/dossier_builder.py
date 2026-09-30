"""Executive Migration Dossier: a one-company briefing, sold for $49 (Phase 9).

For companies with a strong signal (``max(urgency_score, intent_score) > dossier_min_score``, 75 by
default) the agent can build, on demand:

1. **Summary**: intent tag and score, urgency, open roles, the one-line "why now".
2. **Technology footprint**: the stack by category, with the legacy systems called out
   (Oracle, mainframe, on-prem, Hadoop, Teradata...).
3. **Migration path**: the source → target the postings describe, with the short phrases they
   were detected from, and how the company's tag and path changed over time
   (``company_history``). Labelled "detected from N public postings", because that's what it is.
4. **Hiring activity**: every active role (title, location, posted, source link), grouped by
   department with 7- and 30-day counts, and the leadership roles being hired (Head of, Staff,
   Lead, Principal, founding/first hires). These are roles, not people: the dossier contains no
   personal data.
5. **Recommended pitch angles**: rule-based from the signals (migration, compliance deadline,
   new leadership, scaling team), each tied to the evidence behind it.
6. **Method & sources**: where every fact came from.

Rendered as Markdown and as PDF (``tools/pdf_writer.py``, standard library only).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from tools.pdf_writer import PDFDocument

LEGACY = {"Oracle", "SQL Server", "DB2", "Teradata", "Netezza", "Sybase", "Hadoop", "Mainframe", "Heroku", "VMware",
          "On-prem", "Monolith", "Informatica", "SAS", "PHP", "Perl", "AngularJS"}
CATEGORY_LABELS = {"languages": "Languages", "frameworks": "Frameworks", "databases": "Databases", "data_platform": "Data platform",
                   "cloud": "Cloud", "infrastructure": "Infrastructure", "observability": "Observability", "ai_ml": "AI / ML"}
DEPARTMENTS = [
    ("Platform & Infrastructure", r"platform|infra|devops|sre|site reliability|cloud|kubernetes|systems"),
    ("Data", r"data|analytics|etl|warehouse|bi\b"),
    ("Security & Compliance", r"security|secops|compliance|grc|iam"),
    ("AI / ML", r"\bml\b|machine learning|\bai\b|llm|scientist"),
    ("Backend", r"backend|back-end|api|server"),
    ("Frontend & Mobile", r"frontend|front-end|react|ios|android|mobile|ui\b"),
    ("Engineering leadership", r"head of|director|vp|vice president|engineering manager|cto"),
]
LEADERSHIP = re.compile(r"\b(head of|director|vp|vice president|principal|staff|lead|founding|first|manager|architect)\b", re.I)


def eligible(record: dict[str, Any], min_score: int = 75) -> bool:
    return max(int(record.get("urgency_score") or 0), int(record.get("intent_score") or 0)) > min_score


def _department(title: str) -> str:
    t = title.lower()
    return next((name for name, rx in DEPARTMENTS if re.search(rx, t)), "Other engineering")


def _within(ts: str | None, now: datetime, days: int) -> bool:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return now - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)) <= timedelta(days=days)


def pitch_angles(company: dict[str, Any]) -> list[str]:
    signals = set(company.get("commercial_signals") or [])
    path = company.get("migration_path") or ""
    stack = set(company.get("stack") or [])
    out = []
    if path:
        src, _, dst = path.partition(" → ")
        out.append(f"Migration delivery: they're moving {path}. Lead with a {src or 'legacy'}-to-{dst or 'target'} migration "
                   "you've done: cut-over plan, parallel-run, data validation. The hiring shows the project is funded now.")
    if any("Cloud Migration" in s for s in signals) and not path:
        out.append("Cloud move without a named source: offer a discovery/assessment sprint (inventory, landing zone, cost model).")
    if any(k in s for s in signals for k in ("SOC 2", "HIPAA", "FedRAMP", "PCI", "ISO", "HITRUST", "CMMC")):
        which = ", ".join(sorted(s.replace(" Compliance", "").replace(" Authorization", "").replace(" Certification", "")
                                 for s in signals if any(k in s for k in ("SOC 2", "HIPAA", "FedRAMP", "PCI", "ISO", "HITRUST", "CMMC"))))
        out.append(f"Compliance deadline ({which}): evidence collection, control automation, policy-as-code, pen-test readiness. "
                   "Audits have dates, which makes budget easier to unlock.")
    if any(s.startswith(("Founding", "First", "Head of")) for s in signals):
        out.append("New technical leadership: a first/founding/head-of hire will choose tools in their first 90 days. "
                   "Offer a short architecture review or a 'first 30 days' plan.")
    if "Kubernetes" in stack or "Terraform" in stack:
        out.append("Platform engineering: Kubernetes/Terraform in the stack. Offer golden paths, cost controls, on-call load reduction.")
    if int(company.get("openings") or 0) >= 3:
        out.append(f"Capacity: {company['openings']} open roles. Contract or staff-augmentation help while they hire is an easy yes.")
    return out or ["Hiring for this stack right now: offer targeted short-term help on the roles they can't fill fast enough."]


def build(company: dict[str, Any], records_by_niche: dict[str, dict[str, Any]] | None, now: datetime,
          brand: str = "Tech Stack Intel") -> dict[str, Any]:
    """Assemble the dossier content from ``SignalIndex.company()`` output (plus raw radar records
    for the category breakdown and evidence phrases, when available)."""
    raw = next(iter((records_by_niche or {}).values()), {}) if records_by_niche else {}
    tech_stack: dict[str, list[str]] = raw.get("tech_stack") or {}
    stack = company.get("stack") or []
    legacy = sorted({t for t in stack if t in LEGACY} | ({company["migration_path"].split(" → ")[0]}
                                                          if company.get("migration_path") else set()) & LEGACY)
    postings = company.get("active_postings") or []
    titles = [p["title"] for p in postings if p.get("title")] or list(company.get("open_positions") or [])
    depts = Counter(_department(t) for t in titles)
    recent7 = Counter(_department(p["title"]) for p in postings if _within(p.get("first_seen") or p.get("posted_at"), now, 7))
    recent30 = Counter(_department(p["title"]) for p in postings if _within(p.get("first_seen") or p.get("posted_at"), now, 30))
    leaders = sorted({t for t in titles if LEADERSHIP.search(t)})
    history = company.get("history") or []
    evidence = list(raw.get("intent_evidence") or [])
    why = (f"{company.get('intent_tag') or 'Hiring'}: "
           + (f"moving {company['migration_path']}, " if company.get("migration_path") else "")
           + f"{company.get('openings') or len(titles)} open role(s)"
           + (f", {len(leaders)} leadership hire(s)" if leaders else "") + ".")
    return {
        "title": f"Executive Migration Dossier: {company['company']}",
        "company": company["company"], "company_id": company["company_id"], "domain": company.get("domain"),
        "generated_at": now.isoformat(timespec="seconds"), "brand": brand, "why_now": why,
        "summary": {
            "Intent": f"{company.get('intent_tag') or 'n/a'} (score {company.get('intent_score', 0)}/100)",
            "Hiring urgency": f"{company.get('urgency_score', 0)}/100",
            "Open roles": str(company.get("openings") or len(titles)),
            "Niches": ", ".join(company.get("niches") or []) or "n/a",
            "Careers page": (company.get("careers_url") or "n/a") + (" (verified live)" if company.get("careers_url_verified") else ""),
            "Latest posting": (company.get("latest_posted_at") or "n/a")[:10],
        },
        "footprint": {CATEGORY_LABELS.get(k, k): v for k, v in tech_stack.items()} or ({"Stack": stack} if stack else {}),
        "legacy": legacy,
        "migration": {
            "path": company.get("migration_path"),
            "signals": list(company.get("commercial_signals") or []),
            "evidence": evidence,
            "postings_considered": len(titles),
            "history": history,
        },
        "hiring": {
            "roles": [{"title": p.get("title", ""), "location": p.get("location") or "", "posted": (p.get("posted_at") or p.get("first_seen") or "")[:10],
                       "url": p.get("url") or ""} for p in postings] or [{"title": t, "location": "", "posted": "", "url": ""} for t in titles],
            "departments": [{"department": d, "open": n, "new_7d": recent7.get(d, 0), "new_30d": recent30.get(d, 0)}
                            for d, n in depts.most_common()],
            "leadership": leaders,
        },
        "pitch_angles": pitch_angles(company),
    }


def to_markdown(d: dict[str, Any]) -> str:
    m = d["migration"]
    lines = [f"# {d['title']}", "", f"_{d['domain'] or d['company_id']} · generated {d['generated_at'][:10]} by {d['brand']}_", "",
             f"**Why now:** {d['why_now']}", "", "## Summary", ""]
    lines += [f"- **{k}:** {v}" for k, v in d["summary"].items()]
    lines += ["", "## Technology footprint", ""]
    lines += [f"- **{k}:** {', '.join(v)}" for k, v in d["footprint"].items()] or ["- Stack not disclosed in postings."]
    if d["legacy"]:
        lines += ["", f"**Legacy systems in play:** {', '.join(d['legacy'])}"]
    lines += ["", "## Migration path", ""]
    lines.append(f"**{m['path']}** (detected from {m['postings_considered']} public posting(s))" if m["path"]
                 else f"No explicit source → target path; signals: {', '.join(m['signals']) or 'none'}.")
    if m["evidence"]:
        lines += ["", "Phrases it was detected from:", ""] + [f"- \"{e}\"" for e in m["evidence"]]
    if m["history"]:
        lines += ["", "| Date | Intent | Path | Stack |", "|---|---|---|---|"]
        lines += [f"| {h['date']} | {h.get('intent_tag') or '-'} | {h.get('migration_path') or '-'} | {', '.join(h.get('stack') or [])} |"
                  for h in m["history"]]
    h = d["hiring"]
    lines += ["", "## Hiring activity", "", "| Department | Open | New in 7 days | New in 30 days |", "|---|---|---|---|"]
    lines += [f"| {x['department']} | {x['open']} | {x['new_7d']} | {x['new_30d']} |" for x in h["departments"]]
    if h["leadership"]:
        lines += ["", f"**Leadership roles being hired:** {'; '.join(h['leadership'])}"]
    lines += ["", "| Role | Location | Posted | Link |", "|---|---|---|---|"]
    lines += [f"| {r['title']} | {r['location'] or '-'} | {r['posted'] or '-'} | {r['url'] or '-'} |" for r in h["roles"]]
    lines += ["", "## Recommended pitch angles", ""] + [f"{i}. {a}" for i, a in enumerate(d["pitch_angles"], 1)]
    lines += ["", "## Method & sources", "", METHOD]
    return "\n".join(lines) + "\n"


METHOD = ("Every fact above is derived from the company's public job postings (job-board APIs and the company's own careers "
          "board), fingerprinted for technologies and scanned for buying-intent language. Migration paths are detected from "
          "posting text, not confirmed with the company. Roles are listed, never people: this dossier contains no personal data.")


def to_pdf(d: dict[str, Any]) -> bytes:
    doc = PDFDocument(d["title"], f"{d['domain'] or d['company_id']}  ·  generated {d['generated_at'][:10]}  ·  {d['brand']}")
    doc.paragraph(f"Why now: {d['why_now']}", 11, "F2")
    doc.heading("Summary")
    doc.key_values(list(d["summary"].items()))
    doc.heading("Technology footprint")
    if d["footprint"]:
        doc.key_values([(k, ", ".join(v)) for k, v in d["footprint"].items()])
    else:
        doc.paragraph("Stack not disclosed in postings.")
    if d["legacy"]:
        doc.paragraph(f"Legacy systems in play: {', '.join(d['legacy'])}", 10, "F2")
    m = d["migration"]
    doc.heading("Migration path")
    if m["path"]:
        doc.paragraph(m["path"], 13, "F2")
        doc.paragraph(f"Detected from {m['postings_considered']} public posting(s).", 9)
    else:
        doc.paragraph(f"No explicit source-to-target path. Signals: {', '.join(m['signals']) or 'none'}.")
    if m["evidence"]:
        doc.bullets([f'"{e}"' for e in m["evidence"]], 9.5)
    if m["history"]:
        doc.heading("How the signal changed", 2)
        doc.table(["Date", "Intent", "Path", "Stack"], [[x["date"], x.get("intent_tag") or "-", x.get("migration_path") or "-",
                                                        ", ".join(x.get("stack") or [])] for x in m["history"]], [1, 2.2, 1.6, 2.2])
    h = d["hiring"]
    doc.heading("Hiring activity")
    if h["departments"]:
        doc.table(["Department", "Open roles", "New in 7 days", "New in 30 days"],
                  [[x["department"], x["open"], x["new_7d"], x["new_30d"]] for x in h["departments"]], [2.4, 1, 1, 1])
    if h["leadership"]:
        doc.paragraph(f"Leadership roles being hired: {'; '.join(h['leadership'])}", 10, "F2")
    doc.table(["Role", "Location", "Posted", "Link"], [[r["title"], r["location"] or "-", r["posted"] or "-", r["url"] or "-"]
                                                       for r in h["roles"]], [2.6, 1.3, 0.9, 3])
    doc.heading("Recommended pitch angles")
    doc.bullets(d["pitch_angles"])
    doc.heading("Method & sources", 2)
    doc.paragraph(METHOD, 8.5, "F1", (0.38, 0.41, 0.45))
    return doc.to_bytes(datetime.fromisoformat(d["generated_at"]))


REQUIRED_SECTIONS = ("summary", "footprint", "legacy", "migration", "hiring", "pitch_angles", "why_now")
