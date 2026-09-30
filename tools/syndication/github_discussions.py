"""GitHub Discussions publisher (GraphQL ``createDiscussion``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from tools.storefront.github import GitHubClient
    from tools.syndication import Article


class GitHubDiscussionsPublisher:
    name = "github_discussions"
    channel = "github"
    IDS = (
        "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id "
        "discussionCategories(first: 25) { nodes { id name } } } }"
    )
    CREATE = "mutation($input: CreateDiscussionInput!) { createDiscussion(input: $input) { discussion { url } } }"

    def __init__(self, github: "GitHubClient", repo: str, category: str):
        self.github, self.repo, self.category = github, repo, category

    def configured(self) -> bool:
        return bool(self.github.configured() and self.repo)

    def publish(self, article: "Article", published: bool) -> str:
        if not published:
            return ""
        owner, name = self.repo.split("/", 1)
        repo = self.github.graphql(self.IDS, {"owner": owner, "name": name})["repository"]
        cats = {c["name"].lower(): c["id"] for c in repo["discussionCategories"]["nodes"]}
        cat_id = cats.get(self.category.lower())
        if not cat_id:
            raise LookupError(f"discussion category {self.category!r} not found in {self.repo}")
        result = self.github.graphql(
            self.CREATE, {"input": {"repositoryId": repo["id"], "categoryId": cat_id, "title": article.title, "body": article.markdown}}
        )
        return result["createDiscussion"]["discussion"]["url"]
