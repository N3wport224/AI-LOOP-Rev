"""Dev.to (Forem) publisher: ``POST https://dev.to/api/articles`` with the ``api-key`` header."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from tools.syndication.timeutil import parse_ts

if TYPE_CHECKING:  # pragma: no cover
    from tools.http_client import HttpClient
    from tools.syndication import Article

API = "https://dev.to/api"
ACCEPT = "application/vnd.forem.api-v1+json"


class DevToPublisher:
    name = "devto"
    channel = "devto"
    URL = f"{API}/articles"

    def __init__(self, http: "HttpClient", api_key: str):
        self.http, self.api_key = http, api_key

    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"api-key": self.api_key, "Accept": ACCEPT}

    @staticmethod
    def payload(article: "Article", published: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": article.title[:128],
            "body_markdown": article.markdown,
            "published": published,
            "tags": article.tags[:4],  # Forem allows at most 4 tags
            "description": article.description[:150],
        }
        if article.canonical_url.startswith("http"):
            body["canonical_url"] = article.canonical_url  # search credit stays with the lander
        return {"article": body}

    def existing_titles(self) -> set[str]:
        """Titles already on the account (drafts included), a guard against duplicate posts."""
        rows = self.http.get_json(f"{API}/articles/me/all", params={"per_page": 100}, headers=self._headers(),
                                  check_robots=False) or []
        return {str(r.get("title", "")).strip().lower() for r in rows if isinstance(r, dict)}

    def last_published_at(self) -> "datetime | None":
        """When this account last published anything: the platform's record, which survives a
        local state reset. Raises if the history can't be fetched (callers fail closed)."""
        rows = self.http.get_json(f"{API}/articles/me/all", params={"per_page": 100}, headers=self._headers(),
                                  check_robots=False)
        stamps = [parse_ts(r.get("published_at")) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        stamps = [s for s in stamps if s]
        return max(stamps) if stamps else None

    def publish(self, article: "Article", published: bool) -> str:
        if article.title[:128].strip().lower() in self.existing_titles():
            return "already published"
        resp = self.http.post(self.URL, json_body=self.payload(article, published), headers=self._headers(), check_robots=False)
        data = resp.json() or {}
        return str(data.get("url") or data.get("id") or "")
