"""Zero-capital monetization strategies."""

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.distribution_engine import DistributionEngine
from strategies.outreach_stager import OutreachStager
from strategies.dossier_engine import DossierEngine
from strategies.bundle_engine import BundleEngine
from strategies.finance import Finance
from strategies.owner_reports import OwnerReports
from strategies.support_desk import SupportDesk
from strategies.retention_engine import RetentionEngine
from strategies.satellite_orchestrator import SatelliteOrchestrator
from strategies.inbound_syndicator import InboundSyndicator
from strategies.lead_magnet import LeadMagnet
from strategies.subscription_engine import SubscriptionEngine
from strategies.tech_stack_intel import TechStackIntel


def default_strategies() -> list[Strategy]:
    from agent.pricing_engine import PricingStrategy
    from agent.source_discovery import SourceDiscovery

    return [
        LeadAggregator(), TechStackIntel(), DigitalAssetPackager(), OutreachStager(), DistributionEngine(),
        InboundSyndicator(), SubscriptionEngine(), LeadMagnet(), PricingStrategy(),
        SourceDiscovery(), SatelliteOrchestrator(), DossierEngine(), RetentionEngine(), OwnerReports(), Finance(), SupportDesk(), BundleEngine(),
    ]


__all__ = [
    "DigitalAssetPackager", "DistributionEngine", "InboundSyndicator", "LeadAggregator", "LeadMagnet", "SatelliteOrchestrator", "DossierEngine", "RetentionEngine", "OwnerReports", "Finance", "SupportDesk", "BundleEngine", "OutreachStager", "Strategy", "SubscriptionEngine", "TaskContext",
    "TaskResult", "TechStackIntel", "default_strategies",
]
