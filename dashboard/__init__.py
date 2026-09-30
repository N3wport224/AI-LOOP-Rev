"""Telemetry and terminal dashboard."""

from dashboard.render import render_dashboard
from dashboard.snapshot import collect_snapshot

__all__ = ["collect_snapshot", "render_dashboard"]
