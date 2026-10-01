"""What's on sale right now: every published product with a live checkout link (one row per link)."""

from __future__ import annotations

from typing import Any

NOT_DIRECTLY_BUYABLE = {"dossier"}  # sold per company from the API and the Pulse, not from one link


def live_products(state: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in sorted(state.list_assets(), key=lambda a: -int(a["id"])):  # newest version first
        url = str(a.get("checkout_url") or "")
        if a.get("status") != "published" or not url.startswith("https://") or "/test_" in url:
            continue
        if a.get("kind") in NOT_DIRECTLY_BUYABLE or url in seen:
            continue
        seen.add(url)
        out.append({"id": a["id"], "title": a["title"], "kind": a["kind"], "price_cents": int(a["price_cents"] or 0), "url": url,
                    "lander_url": a.get("lander_url") or ""})
    return sorted(out, key=lambda p: (p["kind"] != "lead_directory", p["title"]))
