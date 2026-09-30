"""Minimal GitHub REST client: repository files, gists and traffic (the only view counts GitHub exposes)."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from tools.errors import HttpError

if TYPE_CHECKING:  # pragma: no cover
    from tools.http_client import HttpClient

API = "https://api.github.com"


class GitHubClient:
    def __init__(self, http: "HttpClient", token: str):
        self.http = http
        self.token = token

    def configured(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self.http.request(method, API + path, headers=self._headers(), check_robots=False, **kwargs)
        return resp.json() if resp.body else None

    # -- repository contents ----------------------------------------------------
    def get_file(self, repo: str, path: str, branch: str) -> dict[str, Any] | None:
        try:
            return self._call("GET", f"/repos/{repo}/contents/{quote(path)}", params={"ref": branch})
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def put_file(self, repo: str, path: str, content: str, message: str, branch: str) -> dict[str, Any]:
        """Create or update a file. No-op (no commit) when the content is unchanged."""
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        current = self.get_file(repo, path, branch)
        if current and (current.get("content") or "").replace("\n", "") == encoded:
            return {"changed": False, "html_url": current.get("html_url", "")}
        body: dict[str, Any] = {"message": message, "content": encoded, "branch": branch}
        if current:
            body["sha"] = current["sha"]
        result = self._call("PUT", f"/repos/{repo}/contents/{quote(path)}", json_body=body)
        return {"changed": True, "html_url": (result or {}).get("content", {}).get("html_url", "")}

    # -- gists ---------------------------------------------------------------------
    def upsert_gist(self, gist_id: str | None, description: str, files: dict[str, str], public: bool = True) -> dict[str, Any]:
        payload = {"description": description, "files": {name: {"content": body} for name, body in files.items()}}
        if gist_id:
            result = self._call("PATCH", f"/gists/{gist_id}", json_body=payload)
        else:
            payload["public"] = public
            result = self._call("POST", "/gists", json_body=payload)
        return {"id": result["id"], "html_url": result["html_url"]}

    # -- GraphQL ---------------------------------------------------------------------
    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        result = self._call("POST", "/graphql", json_body={"query": query, "variables": variables}) or {}
        if result.get("errors"):
            raise RuntimeError(f"GitHub GraphQL: {result['errors'][0].get('message', result['errors'])}")
        return result.get("data") or {}

    # -- traffic -------------------------------------------------------------------
    def popular_paths(self, repo: str) -> list[dict[str, Any]]:
        """Top 10 paths by views over the last 14 days (needs push access to the repo)."""
        return list(self._call("GET", f"/repos/{repo}/traffic/popular/paths") or [])
