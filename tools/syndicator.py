"""Backward-compatible import path; the implementation lives in ``tools.syndication``."""

from tools.syndication import (
    Article, DevToPublisher, GitHubDiscussionsPublisher, HashnodePublisher, MIN_INTERVAL_DAYS, Syndicator, build_article,
)

__all__ = [
    "Article", "DevToPublisher", "GitHubDiscussionsPublisher", "HashnodePublisher", "MIN_INTERVAL_DAYS", "Syndicator",
    "build_article",
]
