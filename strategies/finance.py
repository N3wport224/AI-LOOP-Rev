"""Stripe money: your balance and payouts, so you know what's coming without opening Stripe.

The ``sync_finance`` task reads Stripe each cycle (read-only; the key has no payout rights) and keeps
kv ``stripe_finance``:

* ``available_cents``: ready to pay out;
* ``pending_cents``: paid by customers, still settling (new accounts: the first payout usually
  arrives 7-14 days after the first sale, then on a rolling schedule);
* ``last_payout``: the most recent payout (amount, status, arrival date);
* ``paid_out_cents``: everything paid out to your bank so far (from the payouts listed).

Shown in the daily report, the control panel, ``automonetize doctor`` and ``automonetize status``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

STRIPE_API = "https://api.stripe.com/v1"


def _sum(entries: list[dict[str, Any]], currency: str) -> int:
    return sum(int(e.get("amount") or 0) for e in entries or [] if (e.get("currency") or "").lower() == currency.lower())


def read_finance(tools: Any) -> dict[str, Any]:
    cfg = tools.config
    headers = {"Authorization": f"Bearer {cfg.stripe_secret_key}"}
    balance = tools.http.get(f"{STRIPE_API}/balance", headers=headers, check_robots=False, attempts=1).json()
    payouts = tools.http.get(f"{STRIPE_API}/payouts", params={"limit": 20}, headers=headers, check_robots=False,
                             attempts=1).json().get("data") or []
    cur = cfg.currency or "usd"
    last = payouts[0] if payouts else None
    return {
        "at": tools.state.now(), "currency": cur, "livemode": bool(balance.get("livemode")),
        "available_cents": _sum(balance.get("available"), cur), "pending_cents": _sum(balance.get("pending"), cur),
        "paid_out_cents": sum(int(p.get("amount") or 0) for p in payouts if p.get("status") == "paid"),
        "last_payout": {"amount_cents": int(last.get("amount") or 0), "status": last.get("status"),
                        "arrival_date": datetime.fromtimestamp(int(last.get("arrival_date") or 0), tz=timezone.utc).date().isoformat()}
        if last else None,
        "error": "",
    }


def describe(fin: dict[str, Any] | None) -> str:
    """One line for reports: '$12.00 ready, $19.00 on the way; last payout $30.00 on 2026-10-09 (paid)'."""
    if not fin:
        return "Stripe balance: not read yet"
    if fin.get("error"):
        return f"Stripe balance: couldn't read it ({fin['error']})"
    line = f"Stripe balance: ${fin['available_cents'] / 100:,.2f} ready to pay out, ${fin['pending_cents'] / 100:,.2f} on the way"
    last = fin.get("last_payout")
    if last:
        line += f"; last payout ${last['amount_cents'] / 100:,.2f} ({last['status']}, arrives {last['arrival_date']})"
    elif fin.get("pending_cents"):
        line += "; the first payout usually arrives 7-14 days after the first sale"
    return line


class Finance(Strategy):
    name = "finance"
    tasks = ("sync_finance",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        if not tools.config.stripe_secret_key:
            return TaskResult(True, "no Stripe key", {})
        try:
            fin = read_finance(tools)
        except Exception as exc:  # noqa: BLE001 - e.g. a key without balance access: report, don't fail the cycle
            fin = {**(tools.state.get("stripe_finance") or {}), "at": tools.state.now(), "error": str(exc)[:200]}
        tools.state.set("stripe_finance", fin)
        return TaskResult(True, describe(fin), {k: fin.get(k) for k in ("available_cents", "pending_cents", "paid_out_cents")})
