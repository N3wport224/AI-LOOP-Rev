"""First buyers (Phases 422-425): keep track of the people you message yourself.

The agent never contacts anyone for you. This is a notebook for the messages you send by hand
(``docs/first-sales-playbook.md``): who, where, which kind of buyer, which product, and what
happened. After 20-30 messages it answers the question the business depends on: **which kind of
buyer, for which technology, says yes**.

* **Phase 422, the log:** kv ``prospects``. Each entry: name (as you know them; no email needed),
  where you reached them, buyer type, product, status (messaged → replied → interested → bought,
  or not interested), a note, and dates.
* **Phase 423, what it teaches:** per buyer type and per product, how many you messaged and how
  many replied, were interested or bought (``summary``).
* **Phase 424, in the control panel:** the Marketing tab's "First buyers" card.
* **Phase 425, a nudge:** someone who replied or was interested and hasn't been followed up for
  3 days appears as "follow up" in the list.

It's your private notebook: it stays on this Mac (it's part of `automonetize export-all`, which is yours too).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

KEY = "prospects"
BUYERS = ("dev agency", "freelancer", "dev-tool sales", "recruiter", "other")
STATUSES = ("messaged", "replied", "interested", "bought", "not interested")
FOLLOW_UP_DAYS = 3
MAX = 2000


def _clean(text: Any, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def entries(state: Any) -> list[dict[str, Any]]:
    return list(state.get(KEY) or [])


def add(state: Any, name: str, where: str, buyer: str, product: str = "", note: str = "") -> dict[str, Any]:
    name = _clean(name, 80)
    if not name:
        raise ValueError("who did you message? (a name or handle)")
    if buyer not in BUYERS:
        raise ValueError(f"buyer type is one of: {', '.join(BUYERS)}")
    items = entries(state)
    entry = {"id": max([int(e["id"]) for e in items] or [0]) + 1, "name": name, "where": _clean(where, 60),
             "buyer": buyer, "product": _clean(product, 120), "status": "messaged", "note": _clean(note, 300),
             "at": state.now(), "updated": state.now()}
    state.set(KEY, (items + [entry])[-MAX:])
    return entry


def update(state: Any, entry_id: int, status: str | None = None, note: str | None = None) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise ValueError(f"status is one of: {', '.join(STATUSES)}")
    items = entries(state)
    for e in items:
        if int(e["id"]) == int(entry_id):
            if status is not None:
                e["status"] = status
            if note is not None:
                e["note"] = _clean(note, 300)
            e["updated"] = state.now()
            state.set(KEY, items)
            return e
    raise ValueError("no such entry")


def remove(state: Any, entry_id: int) -> bool:
    items = entries(state)
    kept = [e for e in items if int(e["id"]) != int(entry_id)]
    state.set(KEY, kept)
    return len(kept) != len(items)


def follow_up(state: Any, e: dict[str, Any]) -> bool:
    """Phase 425: replied or interested, and quiet for FOLLOW_UP_DAYS."""
    if e["status"] not in ("replied", "interested"):
        return False
    return state.clock() - datetime.fromisoformat(e["updated"]) >= timedelta(days=FOLLOW_UP_DAYS)


def summary(state: Any) -> dict[str, Any]:
    """Phase 423: per buyer type and per product: messaged, replied (or further), interested (or bought), bought."""
    def tally(key: str) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, int]] = {}
        for e in entries(state):
            g = groups.setdefault(e.get(key) or "-", {"messaged": 0, "replied": 0, "interested": 0, "bought": 0})
            g["messaged"] += 1
            if e["status"] != "messaged":
                g["replied"] += 1  # any answer, a "no" included
            if e["status"] in ("interested", "bought"):
                g["interested"] += 1
            if e["status"] == "bought":
                g["bought"] += 1
        return sorted(({"name": k, **v} for k, v in groups.items()), key=lambda r: (-r["bought"], -r["interested"], -r["replied"],
                                                                                   r["name"]))

    items = entries(state)
    return {"total": len(items), "by_buyer": tally("buyer"), "by_product": tally("product"),
            "follow_up": sum(1 for e in items if follow_up(state, e))}


def describe(s: dict[str, Any]) -> list[str]:
    if not s["total"]:
        return ["No messages logged yet. Send 3-5 a day (docs/first-sales-playbook.md) and log each one here."]
    lines = [f"{s['total']} people messaged; {s['follow_up']} waiting for your follow-up."]
    for r in s["by_buyer"]:
        lines.append(f"{r['name']}: {r['messaged']} messaged, {r['replied']} replied, {r['interested']} interested, "
                     f"{r['bought']} bought")
    return lines
