"""Customer requests log (Phase 79): what buyers ask for, in one place.

When a customer's reply asks for something ("can you add salaries?", "do you have one for Rust?",
"would love a Europe-only version"), the support desk still passes the email to you as before, and
also records the request here with any technologies it mentions. Monday's report shows how many
came in and the latest ones, so repeated asks (a niche, a column) stand out. kv
``customer_requests`` (newest 200) and ``requested_topics`` (mention counts).

Phase 99: when the agent picks its next niche (``agent/hypotheses.py``), a technology asked for by
``REQUESTED_MIN`` (2) or more customers goes first: the configured niche it belongs to, or a niche
built around that tag.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from typing import Any

KEY = "customer_requests"
REQUESTED_MIN = 2
TOPICS = "requested_topics"
REQUEST_RE = re.compile(r"\b(can|could|would) you (add|include|make|do|build)\b|\bplease add\b|\bwould love\b|\bwish (it|you) had\b|"
                        r"\bdo you have (one|a dataset|a version|anything|data) for\b|\bany plans? (to|for)\b|\bmissing (a |the )?"
                        r"(field|column|niche|country|region)\b|\bfeature request\b|\bit would be (great|nice|useful)\b", re.I)


def topics(cfg: Any, text: str, vocab: Any = ()) -> list[str]:
    """Niche names and keywords from the configuration, plus ``vocab`` (tags seen in collected job
    postings), that the request mentions."""
    words = {str(v).lower() for v in vocab if len(str(v)) >= 2}
    for n in getattr(cfg, "niches", []) or []:
        words.add(str(n.get("name", "")).lower())
        words.update(str(k).lower() for k in n.get("keywords", []) or [])
    low = text.lower()
    return sorted(w for w in words if w and re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low))


def record(state: Any, cfg: Any, email: str, text: str) -> bool:
    if not REQUEST_RE.search(text):
        return False
    from agent.hypotheses import GENERIC_TAGS

    vocab = [t for t, _ in state.tag_frequencies(limit=200) if t not in GENERIC_TAGS]
    found = topics(cfg, text, vocab)
    items = list(state.get(KEY) or [])
    items.append({"at": state.now(), "email": email.lower(), "text": " ".join(text.split())[:300], "topics": found})
    state.set(KEY, items[-200:])
    counts = Counter(state.get(TOPICS) or {})
    counts.update(found)
    state.set(TOPICS, dict(counts))
    return True


def summary(state: Any, days: int = 30) -> str:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    recent = [r for r in (state.get(KEY) or []) if r["at"] >= since]
    if not recent:
        return ""
    top = Counter(t for r in recent for t in r["topics"]).most_common(3)
    lines = [f"Customer requests in the last {days} days: {len(recent)}"
             + (" (most asked: " + ", ".join(f"{t} ×{n}" for t, n in top) + ")" if top else "")]
    lines += [f"- \"{r['text'][:140]}\"" for r in recent[-3:]]
    return "\n".join(lines)
