"""Monthly bookkeeping: a spreadsheet of last month's money, emailed to you on the 1st.

``monthly_books`` runs every cycle and does its work once a month: on or after the 1st (in
``subscription_timezone``) it writes every verified revenue row of the previous month (sales,
subscription payments, refunds and disputes, with Stripe's fees) to
``data/exports/books/YYYY-MM.csv``, with a totals line, and emails it to ``owner_email`` as an
attachment. Hand it to your accountant or import it into a spreadsheet.

Any month can be (re)built by hand with ``automonetize books [YYYY-MM]``. Off with
``bookkeeping = false``.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Attachment, Email

FIELDS = ("date", "source", "reference", "description", "gross_usd", "fee_usd", "net_usd")
KEY = "books_month"


def tz_of(cfg: Any) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.subscription_timezone or "UTC")
    except Exception:  # noqa: BLE001 - a bad timezone name falls back to UTC
        return ZoneInfo("UTC")


def previous_month(local: datetime) -> str:
    return (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def month_bounds(month: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=tz)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def _usd(cents: int) -> str:
    return f"{cents / 100:.2f}"


def describe(state: Any, row: dict[str, Any]) -> str:
    asset = state.asset_for_product(row["product_ref"]) if row.get("product_ref") else None
    parts = [p for p in (asset["title"] if asset else "", row.get("note") or "") if p]
    return " · ".join(parts) or "sale"


def build_books(state: Any, cfg: Any, month: str) -> tuple[str, dict[str, int]]:
    """CSV text for ``month`` (YYYY-MM, local time) and its totals in cents."""
    tz = tz_of(cfg)
    start, end = month_bounds(month, tz)
    from agent.state import iso

    rows = state._all("SELECT * FROM revenue WHERE verified = 1 AND occurred_at >= ? AND occurred_at < ? ORDER BY occurred_at, id",
                      (iso(start), iso(end)))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(FIELDS)
    totals = {"gross_cents": 0, "fee_cents": 0, "net_cents": 0, "rows": len(rows)}
    for r in rows:
        when = datetime.fromisoformat(r["occurred_at"]).astimezone(tz).date().isoformat()
        writer.writerow([when, r["source"], r["external_id"], describe(state, r),
                         _usd(int(r["gross_cents"])), _usd(int(r["fee_cents"])), _usd(int(r["net_cents"]))])
        for k in ("gross_cents", "fee_cents", "net_cents"):
            totals[k] += int(r[k])
    writer.writerow(["TOTAL", "", "", f"{len(rows)} entries", _usd(totals["gross_cents"]), _usd(totals["fee_cents"]),
                     _usd(totals["net_cents"])])
    return out.getvalue(), totals


def save_books(state: Any, cfg: Any, files: Any, month: str) -> tuple[Any, str, dict[str, int]]:
    text, totals = build_books(state, cfg, month)
    path = files.write_text(f"exports/books/{month}.csv", text)
    return path, text, totals


class Bookkeeping(Strategy):
    name = "bookkeeping"
    tasks = ("monthly_books",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.bookkeeping:
            return TaskResult(True, "bookkeeping off", {})
        month = previous_month(state.clock().astimezone(tz_of(cfg)))
        if state.get(KEY) == month:
            return TaskResult(True, f"books for {month} done", {})
        if state.get(KEY) is None and not state.first_revenue_at():
            state.set(KEY, month)  # nothing has ever been sold: start with the next month
            return TaskResult(True, "no revenue yet: books start next month", {})
        path, text, totals = save_books(state, cfg, tools.files, month)
        to = cfg.owner_email or cfg.sender_email
        if to:
            body = "\n".join([
                f"Your books for {month} are attached ({totals['rows']} entries).", "",
                f"Gross: ${totals['gross_cents'] / 100:,.2f}",
                f"Fees:  ${totals['fee_cents'] / 100:,.2f}",
                f"Net:   ${totals['net_cents'] / 100:,.2f}", "",
                "Refunds and disputes appear as negative lines. A copy is saved on your Mac at",
                str(path), "", "AutoMonetize",
            ])
            try:
                tools.dispatcher.send_transactional(
                    Email(to=to, subject=f"📒 Books for {month}: ${totals['net_cents'] / 100:,.2f} net", body=body, kind="delivery",
                          attachments=[Attachment(f"automonetize-{month}.csv", text.encode(), "text/csv")]),
                    audit_key=f"books:{month}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle (the file is already saved)
                state.log_error("bookkeeping", f"books email for {month} failed: {exc!r}")
                return TaskResult(True, f"books for {month} saved; email failed", {"path": str(path)})
        state.set(KEY, month)
        return TaskResult(True, f"books for {month}: {totals['rows']} entries, ${totals['net_cents'] / 100:,.2f} net",
                          {"path": str(path), **totals})
