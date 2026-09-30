"""Satellite niches: up to ``max_active_niches`` (3) worked at once instead of one at a time.

* The engine's primary hypothesis runs exactly as before (full pipeline, pivot rules). Up to
  ``max_active_niches − 1`` **satellites** (hypotheses with status ``satellite``) run a lighter
  pipeline: collect leads, build the radar, package, publish a checkout. That's enough to sell and
  to feed the site, the API and syndication.
* **Capacity follows money.** Each niche's share of capacity is proportional to its verified net
  revenue over the last ``satellite_window_days`` (14), with a floor of ``satellite_min_share`` so
  a new niche still gets a chance. With no revenue anywhere, shares are equal. Shares drive:

  - how often each satellite is refreshed (a bigger share means more frequent refreshes);
  - which niche the next syndicated article is about (the one furthest below its share of
    recent articles);
  - how the SEO matrix page budget (``seo_max_pages``) is split between niches.

* A satellite with no verified revenue and no subscribers after
  ``satellite_max_days_without_revenue`` (21) days is retired and replaced by the next untried
  niche (``formulate_next``, the same demand-ranked exploration the primary uses).
* Satellites share the per-cycle API budget with everything else. When it runs out they stop
  for this cycle and continue next time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools.errors import CircuitOpenError

SATELLITE = "satellite"
PIPELINE = ("aggregate_leads", "build_intel", "package_asset", "publish_listing")
BASE_REFRESH_HOURS = 6.0


def niche_of(h: dict[str, Any]) -> str:
    return (h.get("params") or {}).get("niche") or ""


def satellites(state: Any) -> list[dict[str, Any]]:
    return [h for h in state.list_hypotheses() if h["status"] == SATELLITE]


def working_set(state: Any) -> list[dict[str, Any]]:
    """Primary first, then satellites."""
    primary = state.active_hypothesis()
    return ([primary] if primary else []) + satellites(state)


def revenue_by_niche(state: Any, since: datetime) -> dict[str, int]:
    rows = state._all("SELECT hypothesis_id, COALESCE(SUM(net_cents), 0) AS net FROM revenue "
                      "WHERE verified = 1 AND occurred_at >= ? AND hypothesis_id IS NOT NULL GROUP BY hypothesis_id",
                      (since.isoformat(timespec="seconds"),))
    niches: dict[str, int] = {}
    for r in rows:
        h = state.get_hypothesis(r["hypothesis_id"])
        if h:
            niches[niche_of(h)] = niches.get(niche_of(h), 0) + int(r["net"])
    return niches


def shares_for(niches: list[str], revenue: dict[str, int], min_share: float) -> dict[str, float]:
    """Revenue-proportional shares with a floor; equal when nothing has sold."""
    if not niches:
        return {}
    k = len(niches)
    total = sum(max(0, revenue.get(n, 0)) for n in niches)
    if total <= 0:
        return {n: round(1 / k, 6) for n in niches}
    floor = min(min_share, 1 / k)
    free = 1 - floor * k
    return {n: round(floor + free * max(0, revenue.get(n, 0)) / total, 6) for n in niches}


def allocation(state: Any, config: Any, now: datetime) -> dict[str, Any]:
    niches = list(dict.fromkeys(niche_of(h) for h in working_set(state) if niche_of(h)))
    revenue = revenue_by_niche(state, now - timedelta(days=config.satellite_window_days))
    result = {"shares": shares_for(niches, revenue, config.satellite_min_share),
              "revenue_cents": {n: revenue.get(n, 0) for n in niches}, "window_days": config.satellite_window_days,
              "updated_at": now.isoformat(timespec="seconds")}
    state.set("niche_allocation", result)
    return result


def choose_by_deficit(shares: dict[str, float], counts: dict[str, int], eligible: list[str] | None = None) -> list[str]:
    """Niches ordered by how far they are below their share of past work (largest deficit first)."""
    pool = [n for n in (eligible or shares) if n in shares]
    total = sum(counts.get(n, 0) for n in pool)

    def deficit(n: str) -> float:
        return shares[n] - (counts.get(n, 0) / total if total else 0.0)

    return sorted(pool, key=lambda n: (-deficit(n), -shares[n], n))


def split_quota(total: int, shares: dict[str, float]) -> dict[str, int]:
    """Largest-remainder split of an integer budget by share."""
    if not shares:
        return {}
    raw = {n: total * s for n, s in shares.items()}
    out = {n: int(v) for n, v in raw.items()}
    for n in sorted(raw, key=lambda n: -(raw[n] - out[n]))[: total - sum(out.values())]:
        out[n] += 1
    return out


class SatelliteOrchestrator(Strategy):
    name = "satellite_orchestrator"
    tasks = ("run_satellites",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        slots = max(0, cfg.max_active_niches - 1)
        if slots == 0:
            return TaskResult(True, "satellites disabled (max_active_niches = 1)", {})
        now = state.clock()
        retired = self.retire(tools, now)
        started = self.fill(tools, slots)
        alloc = allocation(state, cfg, now)
        refreshed, skipped = self.refresh(tools, alloc["shares"], now)
        sats = satellites(state)
        shares = ", ".join(f"{n} {s:.0%}" for n, s in alloc["shares"].items())
        return TaskResult(True, f"{len(sats)} satellites; shares {shares}; refreshed {refreshed or 'none'}"
                          + (f"; started {started}" if started else "") + (f"; retired {retired}" if retired else "")
                          + (f"; waiting {skipped}" if skipped else ""),
                          {"satellites": [niche_of(s) for s in sats], "shares": alloc["shares"], "refreshed": refreshed,
                           "started": started, "retired": retired})

    @staticmethod
    def retire(tools: Any, now: datetime) -> list[str]:
        cfg, state = tools.config, tools.state
        out = []
        for s in satellites(state):
            age = now - datetime.fromisoformat(s["created_at"])
            if age < timedelta(days=cfg.satellite_max_days_without_revenue):
                continue
            recent = state.revenue_for_hypothesis_since(s["id"], now - timedelta(days=cfg.satellite_max_days_without_revenue))
            subscribed = state.list_subscribers(("active", "trialing"), niche=niche_of(s))
            if recent <= 0 and not subscribed:
                state.set_hypothesis_status(s["id"], "deprecated",
                                            f"satellite: no verified revenue in {cfg.satellite_max_days_without_revenue} days")
                out.append(niche_of(s))
        return out

    @staticmethod
    def fill(tools: Any, slots: int) -> list[str]:
        from agent.hypotheses import formulate_next

        state, cfg = tools.state, tools.config
        started = []
        while len(satellites(state)) < slots:
            proposal = formulate_next(state, cfg)
            if proposal is None:
                break
            if proposal["params"]["niche"] in {niche_of(h) for h in working_set(state)}:
                break  # would duplicate a niche already being worked
            hid = state.create_hypothesis(proposal["key"], proposal["strategy"], proposal["description"], proposal["params"])
            state.set_hypothesis_status(hid, SATELLITE, "satellite niche")
            started.append(proposal["params"]["niche"])
        return started

    @staticmethod
    def handlers() -> dict[str, Strategy]:
        from strategies import default_strategies

        out: dict[str, Strategy] = {}
        for strategy in default_strategies():
            if isinstance(strategy, SatelliteOrchestrator):
                continue
            for t in strategy.tasks:
                out[t] = strategy
        return out

    def refresh(self, tools: Any, shares: dict[str, float], now: datetime) -> tuple[list[str], list[str]]:
        state = tools.state
        n = max(1, len(shares))
        handlers = self.handlers()
        refreshed, skipped = [], []
        for sat in sorted(satellites(state), key=lambda s: -shares.get(niche_of(s), 0)):
            niche = niche_of(sat)
            interval = BASE_REFRESH_HOURS / max(0.25, shares.get(niche, 1 / n) * n)
            last = state.get(f"satellite_last_run:{sat['id']}")
            if last and now - datetime.fromisoformat(last) < timedelta(hours=interval):
                continue
            ctx = TaskContext(tools, sat, {})
            try:
                for task in PIPELINE:
                    handler = handlers.get(task)
                    if handler is not None:
                        handler.run(task, ctx)
            except CircuitOpenError:
                skipped.append(niche)
                break  # API budget spent: the rest wait for the next cycle
            except Exception as exc:  # noqa: BLE001 - one satellite failing must not stop the others
                state.log_error(f"satellite:{niche}", repr(exc))
                skipped.append(niche)
                continue
            state.set(f"satellite_last_run:{sat['id']}", now.isoformat(timespec="seconds"))
            state.increment_hypothesis_iterations(sat["id"])
            refreshed.append(niche)
        return refreshed, skipped
