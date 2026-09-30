"""Hashnode publisher. Hashnode's public API is GraphQL only (``https://gql.hashnode.com``, a POST
over HTTPS with the ``Authorization`` token); the old REST API is retired."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from tools.syndication.timeutil import parse_ts

if TYPE_CHECKING:  # pragma: no cover
    from tools.http_client import HttpClient
    from tools.syndication import Article


class HashnodePublisher:
    name = "hashnode"
    channel = "hashnode"
    URL = "https://gql.hashnode.com"
    MUTATION = "mutation PublishPost($input: PublishPostInput!) { publishPost(input: $input) { post { id url } } }"

    def __init__(self, http: "HttpClient", token: str, publication_id: str):
        self.http, self.token, self.publication_id = http, token, publication_id

    def configured(self) -> bool:
        return bool(self.token and self.publication_id)

    def payload(self, article: "Article") -> dict[str, Any]:
        inp: dict[str, Any] = {
            "title": article.title,
            "subtitle": article.description[:250],
            "publicationId": self.publication_id,
            "contentMarkdown": article.markdown,
            "tags": [{"slug": t, "name": t} for t in article.tags],
        }
        if article.canonical_url.startswith("http"):
            inp["originalArticleURL"] = article.canonical_url
        return {"query": self.MUTATION, "variables": {"input": inp}}

    LAST_POST = "query($id: ObjectId!) { publication(id: $id) { posts(first: 1) { edges { node { publishedAt } } } } }"

    def last_published_at(self) -> "datetime | None":
        resp = self.http.post(self.URL, json_body={"query": self.LAST_POST, "variables": {"id": self.publication_id}},
                              headers={"Authorization": self.token}, check_robots=False)
        data = resp.json() or {}
        if data.get("errors"):
            raise RuntimeError(f"hashnode: {data['errors'][0].get('message', data['errors'])}")
        edges = (((data.get("data") or {}).get("publication") or {}).get("posts") or {}).get("edges") or []
        return parse_ts(edges[0]["node"].get("publishedAt")) if edges else None

    def publish(self, article: "Article", published: bool) -> str:
        if not published:
            return ""  # publish-only: Hashnode drafts use a different mutation and never go live by themselves
        resp = self.http.post(self.URL, json_body=self.payload(article), headers={"Authorization": self.token}, check_robots=False)
        data = resp.json() or {}
        if data.get("errors"):
            raise RuntimeError(f"hashnode: {data['errors'][0].get('message', data['errors'])}")
        return str(((data.get("data") or {}).get("publishPost") or {}).get("post", {}).get("url", ""))
