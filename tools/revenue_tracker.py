"""Revenue ledger: verified storefront sales plus manual entries, measured against the daily target."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from agent.state import StateStore
    from tools.http_client import HttpClient

GUMROAD_API = "https://api.gumroad.com/v2"


@dataclass
class SyncReport:
    source: str
    fetched: int = 0
    new: int = 0
    skipped: int = 0
    net_cents_added: int = 0


class RevenueTracker:
    """Only *verified* revenue (pulled from a payment provider API, or manually attested with
    ``verified=True``) counts toward the daily target. Unverified entries are shown separately."""

    def __init__(
        self,
        state: "StateStore",
        daily_target_cents: int = 1000,
        http: "HttpClient | None" = None,
        gumroad_token: str = "",
        fee_pct: float = 10.0,
        fee_fixed_cents: int = 50,
    ):
        self.state = state
        self.daily_target_cents = daily_target_cents
        self.http = http
        self.gumroad_token = gumroad_token
        self.fee_pct = fee_pct
        self.fee_fixed_cents = fee_fixed_cents

    def compute_net(self, gross_cents: int) -> tuple[int, int]:
        """Estimate platform fees when the provider does not report them. Returns (fee, net)."""
        if gross_cents <= 0:
            return 0, gross_cents
        fee = min(gross_cents, round(gross_cents * self.fee_pct / 100) + self.fee_fixed_cents)
        return fee, gross_cents - fee

    def record_manual(
        self,
        amount_cents: int,
        *,
        verified: bool = False,
        note: str = "",
        hypothesis_id: int | None = None,
        source: str = "manual",
        external_id: str | None = None,
        fees_included: bool = True,
        occurred_at: str | None = None,
    ) -> bool:
        """Record revenue you received outside a synced storefront.

        ``amount_cents`` is net when ``fees_included`` (the default), else gross and fees are estimated.
        """
        if fees_included:
            gross, fee, net = amount_cents, 0, amount_cents
        else:
            gross = amount_cents
            fee, net = self.compute_net(amount_cents)
        return self.state.record_revenue(
            source=source,
            external_id=external_id or uuid.uuid4().hex,
            gross_cents=gross,
            fee_cents=fee,
            net_cents=net,
            verified=verified,
            occurred_at=occurred_at,
            hypothesis_id=hypothesis_id,
            note=note,
        )

    # -- Gumroad ---------------------------------------------------------------
    def gumroad_enabled(self) -> bool:
        return bool(self.gumroad_token and self.http is not None)

    def sync_gumroad(self, days_back: int = 3, max_pages: int = 10) -> SyncReport:
        """Pull recent sales from the Gumroad API and record them as verified revenue.

        Idempotent: each sale id is recorded at most once. Refunded / charged-back sales are skipped.
        """
        report = SyncReport(source="gumroad")
        if not self.gumroad_enabled():
            return report
        assert self.http is not None
        after = (self.state.clock() - timedelta(days=days_back)).date().isoformat()
        params: dict[str, Any] = {"access_token": self.gumroad_token, "after": after}
        for _ in range(max_pages):
            payload = self.http.get_json(f"{GUMROAD_API}/sales", params=params, check_robots=False)
            if not payload or not payload.get("success", False):
                raise RuntimeError(f"Gumroad API returned unsuccessful payload: {str(payload)[:200]}")
            for sale in payload.get("sales", []):
                report.fetched += 1
                if not self._record_gumroad_sale(sale, report):
                    report.skipped += 1
            next_key = payload.get("next_page_key")
            if not next_key:
                break
            params = {"access_token": self.gumroad_token, "after": after, "page_key": next_key}
        return report

    def _record_gumroad_sale(self, sale: dict[str, Any], report: SyncReport) -> bool:
        if sale.get("refunded") or sale.get("chargedback") or sale.get("disputed"):
            return False
        sale_id = str(sale.get("id") or "")
        if not sale_id:
            return False
        gross = int(sale.get("price") or 0)
        fee, net = self.compute_net(gross)
        product_ref = str(sale.get("product_id") or "") or None
        hypothesis_id = self.state.hypothesis_for_product(product_ref) if product_ref else None
        occurred = _parse_ts(sale.get("created_at")) or self.state.now()
        inserted = self.state.record_revenue(
            source="gumroad",
            external_id=sale_id,
            gross_cents=gross,
            fee_cents=fee,
            net_cents=net,
            verified=True,
            occurred_at=occurred,
            hypothesis_id=hypothesis_id,
            product_ref=product_ref,
            note=str(sale.get("product_name") or ""),
        )
        if inserted:
            report.new += 1
            report.net_cents_added += net
        return inserted

    def list_gumroad_products(self) -> list[dict[str, Any]]:
        if not self.gumroad_enabled():
            return []
        assert self.http is not None
        payload = self.http.get_json(
            f"{GUMROAD_API}/products", params={"access_token": self.gumroad_token}, check_robots=False
        )
        return list(payload.get("products", [])) if payload and payload.get("success") else []

    # -- storefront orders (Stripe, Lemon Squeezy) ---------------------------------------
    def record_orders(self, orders: list[Any], fee_pct: float, fee_fixed_cents: int) -> SyncReport:
        """Record polled orders as verified revenue and queue them for delivery.

        Orders are attributed to an asset by product ref (Stripe Payment Link id, dedicated
        Lemon Squeezy variant) or, failing that, by exact product name. Unattributable orders still
        count as verified revenue but are flagged ``needs_manual_delivery``.
        """
        report = SyncReport(source="storefront")
        for order in orders:
            report.fetched += 1
            if order.refunded or not order.order_id:
                report.skipped += 1
                continue
            asset = self.state.get_asset(order.asset_id) if getattr(order, "asset_id", None) else None
            if asset is None and order.product_ref:
                asset = self.state.asset_for_product(order.product_ref)
            if asset is None and order.product_name:
                asset = self.state.asset_by_title(order.product_name)
            fee = min(order.gross_cents, round(order.gross_cents * fee_pct / 100) + fee_fixed_cents) if order.gross_cents > 0 else 0
            hypothesis_id = asset["hypothesis_id"] if asset else None
            inserted = self.state.record_revenue(
                source=order.provider,
                external_id=order.order_id,
                gross_cents=order.gross_cents,
                fee_cents=fee,
                net_cents=order.gross_cents - fee,
                verified=True,
                occurred_at=order.occurred_at or None,
                hypothesis_id=hypothesis_id,
                product_ref=order.product_ref,
                note=order.product_name or (asset["title"] if asset else ""),
            )
            if not inserted:
                report.skipped += 1
                continue
            status = getattr(order, "status", None) or ("paid" if asset and order.email else "needs_manual_delivery")
            self.state.record_order(
                order.provider, order.order_id, order.email, order.gross_cents, order.product_ref,
                asset["id"] if asset else None, hypothesis_id, order.occurred_at or None,
                status=status, channel=getattr(order, "channel", None), campaign=getattr(order, "campaign", None),
                kind=getattr(order, "kind", "one_off"),
            )
            report.new += 1
            report.net_cents_added += order.gross_cents - fee
        return report

    def sync_storefronts(self, storefronts: list[Any], days_back: int = 3) -> list[SyncReport]:
        since = self.state.clock() - timedelta(days=days_back)
        refs = self.state.all_product_refs()
        reports = []
        for sf in storefronts:
            if sf.name == "gumroad":
                continue
            rep = self.record_orders(sf.fetch_orders(refs, since), sf.fee_pct, sf.fee_fixed_cents)
            rep.source = sf.name
            reports.append(rep)
        return reports

    # -- reporting ---------------------------------------------------------------
    def daily_summary(self, day: datetime | None = None) -> dict[str, Any]:
        verified = self.state.revenue_for_day(day, verified_only=True)
        everything = self.state.revenue_for_day(day, verified_only=False)
        net = verified["net"]
        target = self.daily_target_cents
        return {
            "net_cents": net,
            "gross_cents": verified["gross"],
            "fee_cents": verified["fees"],
            "sales": verified["n"],
            "unverified_cents": everything["net"] - net,
            "target_cents": target,
            "progress": (net / target) if target else 0.0,
            "target_met": net >= target,
        }

    def history(self, days: int = 7) -> list[dict[str, Any]]:
        today = self.state.clock().astimezone(timezone.utc)
        rows = []
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            summary = self.daily_summary(day)
            summary["date"] = day.date().isoformat()
            rows.append(summary)
        return rows


def _parse_ts(value: Any) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
