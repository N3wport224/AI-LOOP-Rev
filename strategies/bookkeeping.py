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
YEAR_KEY = "books_year"


def tz_of(cfg: Any) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.subscription_timezone or "UTC")
    except Exception:  # noqa: BLE001 - a bad timezone name falls back to UTC
        return ZoneInfo("UTC")


def previous_month(local: datetime) -> str:
    return (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def month_bounds(month: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return period_bounds(month, tz)


def period_bounds(period: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """``YYYY-MM`` (a month) or ``YYYY`` (a year), in local time."""
    if len(period) == 4:
        start = datetime(int(period), 1, 1, tzinfo=tz)
        return start, start.replace(year=start.year + 1)
    start = datetime.strptime(period, "%Y-%m").replace(tzinfo=tz)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def _usd(cents: int) -> str:
    return f"{cents / 100:.2f}"


def describe(state: Any, row: dict[str, Any]) -> str:
    asset = state.asset_for_product(row["product_ref"]) if row.get("product_ref") else None
    parts = [p for p in (asset["title"] if asset else "", row.get("note") or "") if p]
    return " · ".join(parts) or "sale"


def build_books(state: Any, cfg: Any, period: str) -> tuple[str, dict[str, int]]:
    """CSV text for ``period`` (YYYY-MM or YYYY, local time) and its totals in cents.

    Revenue rows (sales, subscriptions, refunds, disputes, with Stripe's fees), then expenses
    (Phase 85), then profit and the suggested tax set-aside (Phase 86)."""
    from agent.state import iso
    from tools import expenses

    tz = tz_of(cfg)
    start, end = period_bounds(period, tz)
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
    spent = expenses.between(state, start.date().isoformat(), end.date().isoformat())
    totals["expense_cents"] = sum(int(e["amount_cents"]) for e in spent)
    if spent:
        writer.writerow([])
        writer.writerow(["date", "expense", "category", "", "", "", "amount_usd"])
        for e in spent:
            writer.writerow([e["spent_on"], e["description"], e["category"], "", "", "", _usd(-int(e["amount_cents"]))])
        writer.writerow(["EXPENSES", "", "", f"{len(spent)} entries", "", "", _usd(-totals["expense_cents"])])
    totals["profit_cents"] = totals["net_cents"] - totals["expense_cents"]
    totals["tax_set_aside_cents"] = max(0, round(totals["profit_cents"] * float(cfg.tax_set_aside_pct) / 100))
    writer.writerow([])
    writer.writerow(["PROFIT", "", "", "net revenue minus expenses", "", "", _usd(totals["profit_cents"])])
    writer.writerow(["SET ASIDE FOR TAX", "", "", f"{cfg.tax_set_aside_pct}% of profit (a rule of thumb, not tax advice)", "", "",
                     _usd(totals["tax_set_aside_cents"])])
    return out.getvalue(), totals


def save_books(state: Any, cfg: Any, files: Any, period: str) -> tuple[Any, str, dict[str, int]]:
    text, totals = build_books(state, cfg, period)
    path = files.write_text(f"exports/books/{period}.csv", text)
    return path, text, totals


def summary_lines(totals: dict[str, int], cfg: Any) -> list[str]:
    return [f"Gross:     ${totals['gross_cents'] / 100:,.2f}",
            f"Fees:      ${totals['fee_cents'] / 100:,.2f}",
            f"Net:       ${totals['net_cents'] / 100:,.2f}",
            f"Expenses:  ${totals['expense_cents'] / 100:,.2f}",
            f"Profit:    ${totals['profit_cents'] / 100:,.2f}",
            f"Set aside about ${totals['tax_set_aside_cents'] / 100:,.2f} ({cfg.tax_set_aside_pct}%) for taxes "
            "(a rule of thumb; your accountant knows your rate)."]


class Bookkeeping(Strategy):
    name = "bookkeeping"
    tasks = ("monthly_books",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.bookkeeping:
            return TaskResult(True, "bookkeeping off", {})
        local = state.clock().astimezone(tz_of(cfg))
        month = previous_month(local)
        year = str(local.year - 1)
        done: list[str] = []
        metrics: dict[str, Any] = {}
        if state.get(KEY) != month:
            if state.get(KEY) is None and not state.first_revenue_at():
                state.set(KEY, month)  # nothing has ever been sold: start with the next month
                state.set(YEAR_KEY, year)
                return TaskResult(True, "no revenue yet: books start next month", {})
            totals = self.send(tools, month, "Books")
            if totals is None:
                return TaskResult(True, f"books for {month} saved; email failed", {})
            state.set(KEY, month)
            done.append(month)
            metrics.update(totals)
        if local.month == 1 and state.get(YEAR_KEY) != year:  # Phase 87: the year-end summary
            if state.get(YEAR_KEY) is None and (state.first_revenue_at() or "9999") >= f"{local.year}":
                state.set(YEAR_KEY, year)
            elif self.send(tools, year, "Year-end books") is not None:
                state.set(YEAR_KEY, year)
                done.append(year)
        if not done:
            return TaskResult(True, f"books for {month} done", {})
        return TaskResult(True, f"books sent for {', '.join(done)}", {"periods": done, **metrics})

    def send(self, tools: Any, period: str, label: str) -> dict[str, int] | None:
        """Save and email the books for ``period``. Returns the totals, or None if the email failed."""
        cfg, state = tools.config, tools.state
        path, text, totals = save_books(state, cfg, tools.files, period)
        to = cfg.owner_email or cfg.sender_email
        if not to:
            return totals
        body = "\n".join([f"Your {label.lower()} for {period} are attached ({totals['rows']} revenue entries).", "",
                          *summary_lines(totals, cfg), "",
                          "Refunds and disputes appear as negative lines. Add expenses with `automonetize expense add`.",
                          "A copy is saved on your Mac at", str(path), "", "AutoMonetize"])
        try:
            tools.dispatcher.send_transactional(
                Email(to=to, subject=f"📒 {label} for {period}: ${totals['profit_cents'] / 100:,.2f} profit", body=body, kind="delivery",
                      attachments=[Attachment(f"automonetize-{period}.csv", text.encode(), "text/csv")]),
                audit_key=f"books:{period}")
        except Exception as exc:  # noqa: BLE001 - retried next cycle (the file is already saved)
            state.log_error("bookkeeping", f"books email for {period} failed: {exc!r}")
            return None
        return totals
