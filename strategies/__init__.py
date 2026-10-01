"""Zero-capital monetization strategies."""

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.distribution_engine import DistributionEngine
from strategies.outreach_stager import OutreachStager
from strategies.dossier_engine import DossierEngine
from strategies.bundle_engine import BundleEngine
from strategies.finance import Finance
from strategies.refunds import Refunds
from strategies.bookkeeping import Bookkeeping
from strategies.buyer_followup import BuyerFollowup
from strategies.share_kit import ShareKit
from strategies.storefront_health import StorefrontHealth
from strategies.release_announcer import ReleaseAnnouncer
from strategies.goal_pacing import GoalPacing
from strategies.freshness_guard import FreshnessGuard
from strategies.launch_promos import LaunchPromos
from strategies.refresh_offers import RefreshOffers
from strategies.referrals import Referrals
from strategies.winback import WinBack
from strategies.product_factory import ProductFactory
from strategies.revenue_models import RevenueModels
from strategies.upsells import Upsells
from strategies.sales_channels import SalesChannels
from strategies.marketing_engine import MarketingEngine
from strategies.marketing_optimizer import MarketingOptimizer
from strategies.bundle_upgrade import BundleUpgrade
from strategies.sample_offer import SampleOffer
from strategies.seasonal_sale import SeasonalSale
from strategies.bounce_guard import BounceGuard
from strategies.offer_tuner import OfferTuner
from strategies.webhook_health import WebhookHealth
from strategies.payment_guard import PaymentGuard
from strategies.plans import Plans
from strategies.payout_watch import PayoutWatch
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
        SourceDiscovery(), SatelliteOrchestrator(), DossierEngine(), RetentionEngine(), OwnerReports(), Finance(), SupportDesk(), BundleEngine(), Refunds(),
        ShareKit(), BuyerFollowup(), Bookkeeping(), StorefrontHealth(), ReleaseAnnouncer(), GoalPacing(), FreshnessGuard(),
        LaunchPromos(), RefreshOffers(), Referrals(), WinBack(),
        BundleUpgrade(), SampleOffer(), SeasonalSale(), BounceGuard(), OfferTuner(),
        WebhookHealth(), PaymentGuard(), Plans(), PayoutWatch(), ProductFactory(), RevenueModels(), Upsells(), SalesChannels(), MarketingEngine(), MarketingOptimizer(),
    ]


__all__ = [
    "DigitalAssetPackager", "DistributionEngine", "InboundSyndicator", "LeadAggregator", "LeadMagnet", "SatelliteOrchestrator", "DossierEngine", "RetentionEngine", "OwnerReports", "Finance", "SupportDesk", "BundleEngine", "Refunds", "ShareKit", "BuyerFollowup", "Bookkeeping", "StorefrontHealth", "ReleaseAnnouncer", "GoalPacing", "FreshnessGuard", "LaunchPromos", "RefreshOffers", "Referrals", "WinBack", "BundleUpgrade", "SampleOffer", "SeasonalSale", "BounceGuard", "OfferTuner", "WebhookHealth", "PaymentGuard", "Plans", "PayoutWatch", "ProductFactory", "RevenueModels", "Upsells", "SalesChannels", "MarketingEngine", "MarketingOptimizer", "OutreachStager", "Strategy", "SubscriptionEngine", "TaskContext",
    "TaskResult", "TechStackIntel", "default_strategies",
]
