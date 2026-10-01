"""`automonetize connect-marketing`: keys checked, site created, settings saved, nothing on failure."""

import json

import pytest

from cli import connect_marketing as cm


class FakeAPI:
    def __init__(self, repo_exists=False, pages=None, scopes="repo, gist", devto_ok=True, site_up=True):
        self.repo_exists, self.pages, self.scopes = repo_exists, pages, scopes
        self.devto_ok, self.site_up = devto_ok, site_up
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        if url.startswith(cm.DEVTO):
            return (200, {}, {"username": "andrew"}) if self.devto_ok else (401, {}, {"error": "unauthorized"})
        path = url.replace(cm.GITHUB, "")
        if path == "/user":
            if headers.get("Authorization") != "Bearer ghp_good":
                return 401, {}, {"message": "Bad credentials"}
            return 200, {"x-oauth-scopes": self.scopes}, {"login": "AndrewP"}
        if path == "/repos/AndrewP/andrewp.github.io" and method == "GET":
            return (200, {}, {"default_branch": "main"}) if self.repo_exists else (404, {}, {"message": "Not Found"})
        if path == "/user/repos":
            self.repo_exists = True
            return 201, {}, {"default_branch": "main"}
        if path.endswith("/pages") and method == "GET":
            return (200, {}, self.pages) if self.pages else (404, {}, {})
        if path.endswith("/pages") and method == "POST":
            return 201, {}, {}
        if "/contents/" in path:
            return (404, {}, {}) if method == "GET" else (201, {}, {})
        if url.startswith("https://andrewp.github.io"):
            return (200, {}, None) if self.site_up else (404, {}, None)
        return 500, {}, {"message": f"unexpected {method} {url}"}


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("STRIPE_SECRET_KEY=rk_live_x\n")
    monkeypatch.setattr(cm, "ROOT", tmp_path)
    return tmp_path / ".env"


def answers(*values):
    it = iter(values)
    return lambda prompt: next(it)


def test_creates_the_site_and_saves_everything(checkout):
    api = FakeAPI()
    restarted = {}
    code = cm.main([], call=api, ask=answers("devto_key", "ghp_good"), do_restart=lambda env: restarted.setdefault("env", env) or True,
                   sleep=lambda s: None)
    assert code == 0 and restarted
    text = checkout.read_text()
    for line in ("DEVTO_API_KEY=devto_key", "GITHUB_TOKEN=ghp_good", "AUTOMONETIZE_GITHUB_PAGES_REPO=AndrewP/andrewp.github.io",
                 "AUTOMONETIZE_GITHUB_PAGES_DIR=docs", "AUTOMONETIZE_PAGES_BASE_URL=https://andrewp.github.io"):
        assert line in text
    created = next(b for m, u, b in api.calls if u.endswith("/user/repos"))
    assert created["name"] == "andrewp.github.io" and created["auto_init"] is True
    pages = next(b for m, u, b in api.calls if m == "POST" and u.endswith("/pages"))
    assert pages == {"source": {"branch": "main", "path": "/docs"}}
    assert any(m == "PUT" and u.endswith("/contents/docs/index.html") for m, u, b in api.calls)


def test_existing_pages_site_keeps_its_branch_and_folder(checkout):
    api = FakeAPI(repo_exists=True, pages={"source": {"branch": "gh-pages", "path": "/"}})
    assert cm.main([], call=api, ask=answers("", "ghp_good"), do_restart=lambda env: True, sleep=lambda s: None) == 0
    text = checkout.read_text()
    assert "AUTOMONETIZE_GITHUB_PAGES_BRANCH=gh-pages" in text and "AUTOMONETIZE_GITHUB_PAGES_DIR=\n" in text
    assert not any(m == "POST" for m, u, b in api.calls if "github.com" in u)  # nothing created or reconfigured
    assert "DEVTO_API_KEY" not in text


@pytest.mark.parametrize("api,keys", [
    (FakeAPI(devto_ok=False), ("bad", "ghp_good")),       # Dev.to key rejected
    (FakeAPI(), ("", "ghp_bad")),                          # GitHub token rejected
    (FakeAPI(scopes="gist"), ("", "ghp_good")),            # classic token without 'repo'
    (FakeAPI(), ("", "")),                                 # nothing pasted
])
def test_nothing_is_saved_when_a_key_fails(checkout, api, keys):
    before = checkout.read_text()
    called = []
    assert cm.main([], call=api, ask=answers(*keys), do_restart=lambda env: called.append(1), sleep=lambda s: None) == 1
    assert checkout.read_text() == before and not called


def test_http_call_never_raises(monkeypatch):
    status, _, body = cm.http_call("GET", "http://127.0.0.1:9/nothing", {})
    assert status == 0 and "message" in body
    assert json.dumps(body)
