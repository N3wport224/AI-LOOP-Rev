"""Zero-capital monetization strategies."""

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.distribution_engine import DistributionEngine
from strategies.outreach_stager import OutreachStager
from strategies.tech_stack_intel import TechStackIntel


def default_strategies() -> list[Strategy]:
    return [LeadAggregator(), TechStackIntel(), DigitalAssetPackager(), OutreachStager(), DistributionEngine()]


__all__ = [
    "DigitalAssetPackager", "DistributionEngine", "LeadAggregator", "OutreachStager", "Strategy", "TaskContext",
    "TaskResult", "TechStackIntel", "default_strategies",
]
