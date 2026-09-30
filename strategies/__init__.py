"""Zero-capital monetization strategies."""

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.outreach_stager import OutreachStager


def default_strategies() -> list[Strategy]:
    return [LeadAggregator(), DigitalAssetPackager(), OutreachStager()]


__all__ = [
    "DigitalAssetPackager", "LeadAggregator", "OutreachStager", "Strategy", "TaskContext", "TaskResult",
    "default_strategies",
]
