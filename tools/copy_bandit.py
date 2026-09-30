"""Landing-page copy optimisation: a multi-armed bandit per copy slot.

**Parameter space** (``SLOTS``). Each slot is its own bandit; a visitor gets one arm from each:

* headline: ``migration_urgency`` · ``hiring_stack`` (control) · ``verified_leads``
* cta: ``free_sample`` · ``instant_feed`` (control) · ``developer_api``

Copy only states what the data supports: an arm whose claim isn't true for a page (e.g.
"verified leads" with no verified careers page, "Query the API" when the API tier isn't live) is
not offered on that page.

**Reward = conversions per view**, where a conversion is a free-sample signup or a purchase. Clicks
are recorded but not rewarded: the CTAs lead to different places (a click on "free sample" costs
the visitor nothing, a checkout click does), so optimising clicks would just learn that free things
get clicked.

**Allocation**

* **Prior**: empirical Bayes. Each arm starts at the slot's pooled conversion rate (2% before
  any data) worth ``PRIOR_VIEWS`` (50) pseudo-views: Beta(p0·50 + conversions,
  (1 − p0)·50 + views − conversions). A flat Beta(1, 1) would give an untested arm a 50%
  expected rate, and it would "win" against real arms converting at 1-5%.
* ``epsilon_greedy`` (default): the arm with the best posterior mean conversion rate gets
  ``1 − ε`` = 80% of traffic; the other active arms split ε = 20% evenly (3 arms: 80/10/10).
* ``thompson``: each arm gets traffic in proportion to the probability that it's the best,
  estimated by sampling the Beta posteriors (seeded, so a given day's data gives one answer).

**Deprecation**: the baseline is the slot's **control** arm (as in a classic A/B test). Once an
arm has ``copy_bandit_min_views`` views, it's retired when it falls ``copy_bandit_deprecate_sd``
(2) standard errors below the control in a two-proportion test:
``z = (p_arm − p_ctrl) / sqrt(p_ctrl(1 − p_ctrl)/n_ctrl + p_arm(1 − p_arm)/n_arm) < −2``.
The control, the current winner and the last active arm are never retired. Measuring against
the pooled rate instead would eventually retire every non-winner (the pool is dominated by the
winner's traffic) and exploration would stop.

**Serving**: the landers are static, so assignment happens in the visitor's browser from the
allocation baked into the page, kept per visitor in ``localStorage`` so copy doesn't flicker. Views,
clicks and signups arrive as beacons at ``/t/e`` through the tunnel, deduplicated per visitor, page,
event and day. Purchases are counted from Stripe: the checkout's ``client_reference_id`` carries the
variant. Crawlers and visitors without JavaScript see the current winner, rendered server-side.
"""

from __future__ import annotations

import hashlib
import math
import random
import json
import re
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SLOTS: dict[str, dict[str, dict[str, str]]] = {
    "headline": {
        "migration_urgency": {"code": "m", "text": "{hot} {label} companies are migrating or hiring urgently right now"},
        "hiring_stack": {"code": "h", "text": "{label} hiring & stack intelligence: {companies} companies, updated daily"},
        "verified_leads": {"code": "v", "text": "{verified} verified {label} developer leads with live careers pages"},
    },
    "cta": {
        "free_sample": {"code": "s", "text": "Download the free sample"},
        "instant_feed": {"code": "f", "text": "Get instant access: {price}"},
        "developer_api": {"code": "a", "text": "Query the Developer API: {api_price}/month"},
    },
}
CONTROL = {"headline": "hiring_stack", "cta": "instant_feed"}

# Extra headline arms the agent wrote itself when revenue plateaued (agent/evolution). The file is
# evolvable data; this loader is not, and it drops anything that could make a false claim: unknown
# placeholders, a claim without its ``requires`` fact, clashing names or codes, overlong text.
SEED_FILE = Path(__file__).resolve().parents[1] / "seeds" / "copy_variants.json"
SAFE_PLACEHOLDERS = {"label", "companies", "hot", "verified", "price"}
REQUIRABLE = {"hot", "verified"}


def evolved_arms(path: Path = SEED_FILE) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    used = {a["code"] for a in SLOTS["headline"].values()}
    out: dict[str, dict[str, Any]] = {}
    for name, arm in sorted(((data.get("headline") or {}) if isinstance(data, dict) else {}).items()):
        if not (isinstance(arm, dict) and re.fullmatch(r"[a-z][a-z0-9_]{2,40}", str(name)) and name not in SLOTS["headline"]):
            continue
        code, text, requires = arm.get("code"), arm.get("text"), arm.get("requires") or []
        if not (isinstance(code, str) and re.fullmatch(r"[a-z]", code) and code not in used):
            continue
        if not (isinstance(text, str) and 10 <= len(text) <= 140 and isinstance(requires, list) and set(requires) <= REQUIRABLE):
            continue
        try:
            fields = {f for _, f, _, _ in string.Formatter().parse(text) if f is not None}
        except ValueError:
            continue
        if not fields <= SAFE_PLACEHOLDERS or (fields & REQUIRABLE) - set(requires):
            continue  # every number it states must be a fact the page has
        used.add(code)
        out[name] = {"code": code, "text": text, "requires": list(requires), "evolved": True}
    return out


SLOTS["headline"].update(evolved_arms())
EVENTS = ("view", "click", "signup", "purchase")
REWARD_EVENTS = ("signup", "purchase")
PRIOR_VIEWS = 50
DEFAULT_RATE = 0.02
WINDOW_DAYS = 30
CODE_TO_VARIANT = {slot: {v["code"]: name for name, v in arms.items()} for slot, arms in SLOTS.items()}
VARIANT_REF = re.compile(r"^h([a-z])_c([a-z])$")


def variant_ref(headline: str, cta: str) -> str:
    """Compact variant tag carried in Stripe's client_reference_id: ``hm_cs``."""
    return f"h{SLOTS['headline'][headline]['code']}_c{SLOTS['cta'][cta]['code']}"


def parse_variant_ref(ref: str | None) -> dict[str, str] | None:
    """``am--devto--w40--hm_cs`` → {"headline": "migration_urgency", "cta": "free_sample"}."""
    parts = (ref or "").split("--")
    if len(parts) < 4:
        return None
    m = VARIANT_REF.match(parts[3])
    if not m:
        return None
    h, c = CODE_TO_VARIANT["headline"].get(m.group(1)), CODE_TO_VARIANT["cta"].get(m.group(2))
    return {"headline": h, "cta": c} if h and c else None


def day_of(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


# ----------------------------------------------------------------------------- storage
def record_event(state: Any, slot: str, variant: str, event: str, now: datetime, n: int = 1) -> None:
    if slot not in SLOTS or variant not in SLOTS[slot] or event not in EVENTS:
        raise ValueError(f"unknown copy event {slot}/{variant}/{event}")
    state._exec("INSERT INTO copy_events (day, slot, variant, event, count) VALUES (?,?,?,?,?) "
                "ON CONFLICT(day, slot, variant, event) DO UPDATE SET count = count + excluded.count",
                (day_of(now), slot, variant, event, n))


def record_purchase_from_ref(state: Any, ref: str | None, now: datetime) -> bool:
    variants = parse_variant_ref(ref)
    if not variants:
        return False
    for slot, variant in variants.items():
        record_event(state, slot, variant, "purchase", now)
    return True


def first_time(state: Any, key_material: str, now: datetime) -> bool:
    """Dedupe: True only the first time this (visitor, page, event, variant) is seen today."""
    key = hashlib.sha256(f"{key_material}|{day_of(now)}".encode()).hexdigest()[:32]
    cur = state._exec("INSERT OR IGNORE INTO telemetry_seen (key, day) VALUES (?, ?)", (key, day_of(now)))
    return cur.rowcount == 1


def purge_seen(state: Any, now: datetime, keep_days: int = 2) -> int:
    cutoff = day_of(now - timedelta(days=keep_days))
    return state._exec("DELETE FROM telemetry_seen WHERE day < ?", (cutoff,)).rowcount


def stats(state: Any, now: datetime, window_days: int = WINDOW_DAYS) -> dict[str, dict[str, dict[str, int]]]:
    since = day_of(now - timedelta(days=window_days - 1))
    out = {slot: {v: {e: 0 for e in EVENTS} for v in arms} for slot, arms in SLOTS.items()}
    for r in state._all("SELECT slot, variant, event, SUM(count) AS n FROM copy_events WHERE day >= ? GROUP BY slot, variant, event",
                        (since,)):
        if r["slot"] in out and r["variant"] in out[r["slot"]]:
            out[r["slot"]][r["variant"]][r["event"]] = int(r["n"])
    return out


# ----------------------------------------------------------------------------- the math
def conversions(s: dict[str, int]) -> int:
    return sum(s.get(e, 0) for e in REWARD_EVENTS)


def prior(arm_stats: dict[str, dict[str, int]], active: list[str]) -> float:
    """The slot's pooled conversion rate (DEFAULT_RATE before any views)."""
    views = sum(arm_stats[v].get("view", 0) for v in active)
    conv = sum(conversions(arm_stats[v]) for v in active)
    return min(0.99, max(0.001, conv / views)) if views else DEFAULT_RATE


def beta_params(s: dict[str, int], p0: float = DEFAULT_RATE) -> tuple[float, float]:
    views = max(s.get("view", 0), conversions(s))
    return p0 * PRIOR_VIEWS + conversions(s), (1 - p0) * PRIOR_VIEWS + views - conversions(s)


def posterior_mean(s: dict[str, int], p0: float = DEFAULT_RATE) -> float:
    a, b = beta_params(s, p0)
    return a / (a + b)


def rate(s: dict[str, int]) -> float:
    return conversions(s) / s["view"] if s.get("view") else 0.0


def winner(slot: str, arm_stats: dict[str, dict[str, int]], active: list[str]) -> str:
    declared = list(SLOTS[slot])
    rank = {v: (v != CONTROL[slot], declared.index(v)) for v in declared}  # ties: control first, then declaration order
    p0 = prior(arm_stats, active)
    return max(sorted(active, key=rank.__getitem__), key=lambda v: round(posterior_mean(arm_stats[v], p0), 12))


def epsilon_greedy(slot: str, arm_stats: dict[str, dict[str, int]], active: list[str], epsilon: float) -> dict[str, float]:
    if len(active) == 1:
        return {active[0]: 1.0}
    best = winner(slot, arm_stats, active)
    share = epsilon / (len(active) - 1)
    return {v: round(1 - epsilon if v == best else share, 6) for v in active}


def thompson(slot: str, arm_stats: dict[str, dict[str, int]], active: list[str], seed: str, draws: int = 4000) -> dict[str, float]:
    rng = random.Random(seed)
    wins = dict.fromkeys(active, 0)
    p0 = prior(arm_stats, active)
    params = {v: beta_params(arm_stats[v], p0) for v in active}
    for _ in range(draws):
        best = max(active, key=lambda v: rng.betavariate(*params[v]))
        wins[best] += 1
    return {v: round(wins[v] / draws, 6) for v in active}


def should_deprecate(arm: dict[str, int], baseline: dict[str, int], min_views: int, sd: float) -> tuple[bool, float]:
    """(retire?, z) for an arm against the baseline (control) arm, two-proportion z-test."""
    n, n0 = arm.get("view", 0), baseline.get("view", 0)
    if n < min_views or n0 < min_views:
        return False, 0.0
    p, p0 = rate(arm), rate(baseline)
    se = math.sqrt(p0 * (1 - p0) / n0 + p * (1 - p) / n)
    if se <= 0:
        return False, 0.0
    z = (p - p0) / se
    return z < -sd, round(z, 3)


def tune(state: Any, config: Any, now: datetime) -> dict[str, Any]:
    """Recompute the allocation (and retire losers). Stored in kv ``copy_policy``; ``version``
    only moves when an allocation shifts by ≥ 5 points or an arm is retired, so pages aren't
    republished for noise."""
    data = stats(state, now)
    previous = state.get("copy_policy") or {}
    status: dict[str, dict[str, dict[str, Any]]] = previous.get("status") or {}
    changes = []
    policy: dict[str, dict[str, float]] = {}
    for slot, arms in SLOTS.items():
        slot_status = status.setdefault(slot, {})
        for v in arms:
            slot_status.setdefault(v, {"state": "active"})
        slot_status[CONTROL[slot]] = {"state": "active"}  # the baseline is never retired
        active = [v for v in arms if slot_status[v]["state"] == "active"]
        control = data[slot][CONTROL[slot]]
        best = winner(slot, data[slot], active)
        for v in list(active):
            if v in (best, CONTROL[slot]) or len(active) <= 1:
                continue
            retire, z = should_deprecate(data[slot][v], control, config.copy_bandit_min_views, config.copy_bandit_deprecate_sd)
            if retire:
                slot_status[v] = {"state": "deprecated", "at": now.isoformat(timespec="seconds"), "z": z,
                                  "reason": f"{rate(data[slot][v]):.2%} vs control {rate(control):.2%} "
                                            f"over {data[slot][v]['view']} views (z = {z})"}
                active.remove(v)
                changes.append(f"{slot}/{v} retired (z = {z})")
        if config.copy_bandit_algorithm == "thompson":
            policy[slot] = thompson(slot, data[slot], active, seed=f"{day_of(now)}:{slot}")
        else:
            policy[slot] = epsilon_greedy(slot, data[slot], active, config.copy_bandit_epsilon)
    old = previous.get("allocation") or {}
    moved = any(abs(policy[s].get(v, 0) - old.get(s, {}).get(v, 0)) >= 0.05 for s in policy for v in SLOTS[s])
    version = int(previous.get("version", 0)) + (1 if (moved or changes or not previous) else 0)
    result = {"version": version, "updated_at": now.isoformat(timespec="seconds"), "algorithm": config.copy_bandit_algorithm,
              "allocation": policy, "status": status,
              "winners": {s: max(policy[s], key=policy[s].get) for s in policy}, "changes": changes}
    state.set("copy_policy", result)
    return result


def report(state: Any, now: datetime) -> list[dict[str, Any]]:
    """Per-arm table for the dashboards."""
    data = stats(state, now)
    policy = state.get("copy_policy") or {}
    rows = []
    for slot, arms in SLOTS.items():
        for v in arms:
            s = data[slot][v]
            rows.append({"slot": slot, "variant": v, "views": s["view"], "clicks": s["click"], "signups": s["signup"],
                         "purchases": s["purchase"], "conversion_rate": round(rate(s), 4),
                         "allocation": (policy.get("allocation") or {}).get(slot, {}).get(v, 0.0),
                         "state": ((policy.get("status") or {}).get(slot, {}).get(v) or {}).get("state", "active"),
                         "control": v == CONTROL[slot]})
    return rows


# ----------------------------------------------------------------------------- rendering
def render_copy(slot: str, variant: str, facts: dict[str, Any]) -> str | None:
    """The text for an arm on one page, or None when the page's data doesn't support its claim."""
    if slot == "headline" and variant == "migration_urgency" and not facts.get("hot"):
        return None
    if slot == "headline" and variant == "verified_leads" and not facts.get("verified"):
        return None
    if slot == "cta" and variant == "free_sample" and not facts.get("lead_form"):
        return None
    if slot == "cta" and variant == "developer_api" and not facts.get("api_url"):
        return None
    if slot == "cta" and variant == "instant_feed" and not facts.get("checkout_url"):
        return None
    if any(not facts.get(r) for r in SLOTS[slot][variant].get("requires", ())):
        return None
    return SLOTS[slot][variant]["text"].format(**{k: v for k, v in facts.items() if v is not None})


def page_arms(facts: dict[str, Any], policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The arms available on this page with renormalised allocation, for the page's JSON blob."""
    out: dict[str, dict[str, Any]] = {}
    alloc = policy.get("allocation") or {}
    status = policy.get("status") or {}
    for slot, arms in SLOTS.items():
        choices = {}
        for v in arms:
            if ((status.get(slot) or {}).get(v) or {}).get("state") == "deprecated":
                continue
            text = render_copy(slot, v, facts)
            if text is not None:
                weight = (alloc.get(slot) or {}).get(v, 1.0 / len(arms))
                choices[v] = {"text": text, "w": weight}
        total = sum(c["w"] for c in choices.values()) or 1.0
        for c in choices.values():
            c["w"] = round(c["w"] / total, 6)
        if choices:
            out[slot] = choices
    return out


# ----------------------------------------------------------------------------- beacon endpoint
BEACON_EVENTS = ("view", "click")  # signups come from the form itself, purchases from Stripe
_VISITOR = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_PAGE = re.compile(r"^[a-z0-9][a-z0-9/._-]{0,99}$")


def handle_beacon(state: Any, body: bytes, now: datetime) -> tuple[int, str]:
    """``POST /t/e`` (sendBeacon, text/plain JSON). Returns (status, reason). Unknown variants,
    events or malformed ids are dropped; each (visitor, page, event, arms) counts once a day."""
    import json

    if len(body) > 1024:
        return 413, "too large"
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, "not JSON"
    if not isinstance(data, dict):
        return 400, "not an object"
    event, h, c = data.get("e"), data.get("h"), data.get("c")
    visitor, page = str(data.get("v") or ""), str(data.get("p") or "")
    if event not in BEACON_EVENTS or h not in SLOTS["headline"] or c not in SLOTS["cta"]:
        return 400, "unknown event or variant"
    if not _VISITOR.match(visitor) or not _PAGE.match(page):
        return 400, "bad visitor or page"
    if not first_time(state, f"{visitor}|{page}|{event}|{h}|{c}", now):
        return 204, "duplicate"
    record_event(state, "headline", h, event, now)
    record_event(state, "cta", c, event, now)
    return 204, "recorded"


def record_signup(state: Any, copy_tag: str | None, now: datetime) -> bool:
    """A free-sample signup from a form tagged by the copy script (hidden ``copy`` field)."""
    variants = parse_variant_ref(f"x--x--x--{copy_tag or ''}")
    if not variants:
        return False
    for slot, variant in variants.items():
        record_event(state, slot, variant, "signup", now)
    return True
