"""Zero-capital monetization strategies."""

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.distribution_engine import DistributionEngine
from strategies.outreach_stager import OutreachStager
from strategies.inbound_syndicator import InboundSyndicator
from strategies.subscription_engine import SubscriptionEngine
from strategies.tech_stack_intel import TechStackIntel


def default_strategies() -> list[Strategy]:
    from agent.pricing_engine import PricingStrategy

    return [
        LeadAggregator(), TechStackIntel(), DigitalAssetPackager(), OutreachStager(), DistributionEngine(),
        InboundSyndicator(), SubscriptionEngine(), PricingStrategy(),
    ]


__all__ = [
    "DigitalAssetPackager", "DistributionEngine", "InboundSyndicator", "LeadAggregator", "OutreachStager", "Strategy", "SubscriptionEngine", "TaskContext",
    "TaskResult", "TechStackIntel", "default_strategies",
]
