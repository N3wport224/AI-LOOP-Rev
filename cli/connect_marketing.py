"""`automonetize connect-marketing`: switch on autonomous marketing in one step.

You paste two keys; everything else is automatic:

1. **Dev.to API key** (optional): checked with ``GET /api/users/me``. The agent then publishes a
   data article every few days with links to your products.
2. **GitHub token** (classic, scopes ``repo`` + ``gist``): checked with ``GET /user``. Then:
   - ``<you>.github.io`` is created if you don't have it (public, initialised);
   - a placeholder ``docs/index.html`` is committed and GitHub Pages is switched on for ``/docs``
     (an existing Pages site keeps its own branch and folder);
3. saves DEVTO_API_KEY, GITHUB_TOKEN, the Pages repo/branch/folder and the site URL to .env;
4. restarts the agent, which publishes the product pages, sitemap and feed on its next cycle;
5. waits until ``https://<you>.github.io/`` answers and prints your public addresses.

Nothing is saved unless the keys check out.
"""

from __future__ import annotations

import base64
import getpass
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from cli.go_live import BOLD, GREEN, RED, ROOT, YELLOW, restart, say

GITHUB = "https://api.github.com"
DEVTO = "https://dev.to/api"
TOKEN_URL = "https://github.com/settings/tokens/new?scopes=repo,gist&description=AutoMonetize"
PLACEHOLDER = ("<!doctype html><meta charset=utf-8><title>Tech Stack Intel</title>"
               "<p>Live hiring and tech-stack datasets. Pages are being published; check back in a minute.</p>\n")

Call = Callable[[str, str, dict[str, str], Any], tuple[int, dict[str, str], Any]]


def http_call(method: str, url: str, headers: dict[str, str], body: Any = None) -> tuple[int, dict[str, str], Any]:
    """(status, headers, parsed JSON or None). Never raises for HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": "AutoMonetize", **({"Content-Type": "application/json"} if data else {}), **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https URLs
            raw = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, parsed
    except (urllib.error.URLError, OSError) as exc:
        return 0, {}, {"message": str(exc)}


def gh(call: Call, token: str, method: str, path: str, body: Any = None) -> tuple[int, dict[str, str], Any]:
    return call(method, GITHUB + path, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                        "X-GitHub-Api-Version": "2022-11-28"}, body)


def check_devto(call: Call, key: str) -> str | None:
    status, _, body = call("GET", f"{DEVTO}/users/me", {"api-key": key, "Accept": "application/vnd.forem.api-v1+json"}, None)
    if status == 200 and isinstance(body, dict):
        say(GREEN, f"✔ Dev.to: key works (@{body.get('username', '?')}); articles will be posted automatically")
        return body.get("username") or "?"
    say(RED, f"✘ Dev.to rejected the key ({status}). Generate a new one at dev.to/settings/extensions.")
    return None


def check_github(call: Call, token: str) -> str | None:
    status, headers, body = gh(call, token, "GET", "/user")
    if status != 200 or not isinstance(body, dict):
        say(RED, f"✘ GitHub rejected the token ({status}). Create one at {TOKEN_URL}")
        return None
    scopes = {s.strip() for s in headers.get("x-oauth-scopes", "").split(",") if s.strip()}
    if token.startswith(("ghp_", "gho_")) and "repo" not in scopes:
        say(RED, f"✘ The token needs the 'repo' scope (it has: {', '.join(sorted(scopes)) or 'none'}).")
        say(RED, f"  Create a new one with 'repo' and 'gist' ticked: {TOKEN_URL}")
        return None
    say(GREEN, f"✔ GitHub: token works (@{body['login']})")
    return str(body["login"])


def setup_pages(call: Call, token: str, login: str) -> dict[str, str] | None:
    """Create (or reuse) <login>.github.io with Pages on. Returns the settings to save, or None."""
    repo_name = f"{login.lower()}.github.io"
    repo = f"{login}/{repo_name}"
    status, _, info = gh(call, token, "GET", f"/repos/{repo}")
    if status == 404:
        status, _, info = gh(call, token, "POST", "/user/repos", {
            "name": repo_name, "description": "Live hiring & tech-stack datasets", "auto_init": True, "has_issues": False,
            "homepage": f"https://{repo_name}"})
        if status not in (200, 201):
            say(RED, f"✘ Couldn't create {repo} ({status}): {(info or {}).get('message', '')}")
            return None
        say(GREEN, f"✔ Created your website repository {repo}")
        time.sleep(2)  # the initial commit lands asynchronously
    elif status != 200:
        say(RED, f"✘ Couldn't read {repo} ({status}): {(info or {}).get('message', '')}")
        return None
    else:
        say(GREEN, f"✔ Using your existing repository {repo}")
    branch = (info or {}).get("default_branch") or "main"

    status, _, pages = gh(call, token, "GET", f"/repos/{repo}/pages")
    if status == 200 and isinstance(pages, dict):
        src = pages.get("source") or {}
        branch = src.get("branch") or branch
        folder = (src.get("path") or "/").strip("/")
        say(GREEN, f"✔ GitHub Pages already on (branch {branch}, folder /{folder})")
    else:
        folder = "docs"
        path = f"/repos/{repo}/contents/{folder}/index.html"
        status, _, existing = gh(call, token, "GET", f"{path}?ref={branch}")
        if status == 404:
            status, _, res = gh(call, token, "PUT", path, {
                "message": "Set up the site", "branch": branch, "content": base64.b64encode(PLACEHOLDER.encode()).decode()})
            if status not in (200, 201):
                say(RED, f"✘ Couldn't write the first page ({status}): {(res or {}).get('message', '')}")
                return None
        status, _, res = gh(call, token, "POST", f"/repos/{repo}/pages", {"source": {"branch": branch, "path": f"/{folder}"}})
        if status not in (200, 201, 409):  # 409: already enabled meanwhile
            say(RED, f"✘ Couldn't switch on GitHub Pages ({status}): {(res or {}).get('message', '')}")
            say(RED, f"  Switch it on by hand: github.com/{repo}/settings/pages → Branch: {branch}, folder /docs → Save.")
            return None
        say(GREEN, f"✔ GitHub Pages switched on (branch {branch}, folder /{folder})")
    return {"AUTOMONETIZE_GITHUB_PAGES_REPO": repo, "AUTOMONETIZE_GITHUB_PAGES_BRANCH": branch,
            "AUTOMONETIZE_GITHUB_PAGES_DIR": folder, "AUTOMONETIZE_PAGES_BASE_URL": f"https://{repo_name}"}


def wait_for_site(call: Call, url: str, minutes: int = 10, sleep: Callable[[float], None] = time.sleep) -> bool:
    say(BOLD, f"Waiting for {url} to go live (GitHub takes a minute or two)...")
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        status, _, _ = call("GET", url, {"Accept": "text/html"}, None)
        if status == 200:
            return True
        sleep(15)
        print(".", end="", flush=True)
    print()
    return False


def main(argv: list[str] | None = None, call: Call = http_call, ask: Callable[[str], str] = getpass.getpass,
         do_restart: Callable[[dict[str, str]], bool] | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    import os

    from agent.setup_autonomous import load_env_into
    from agent.tunnel import update_env_file

    os.chdir(ROOT)
    env_file = ROOT / ".env"
    if not env_file.exists():
        say(RED, f"✘ {env_file} not found. Run `automonetize go-live` first.")
        return 1
    say(BOLD, "Connecting autonomous marketing. Keys stay hidden while you paste them.")
    updates: dict[str, str] = {}

    print("\n1) Dev.to API key: dev.to/settings/extensions → 'Generate API Key'. Press Return to skip.")
    devto = ask("   Paste the Dev.to key: ").strip()
    if devto:
        if check_devto(call, devto) is None:
            return 1
        updates["DEVTO_API_KEY"] = devto

    print(f"\n2) GitHub token: open {TOKEN_URL}")
    print("   (expiration: 'No expiration' or 1 year; 'repo' and 'gist' are pre-ticked) → Generate token → copy it.")
    token = ask("   Paste the GitHub token: ").strip()
    site: dict[str, str] | None = None
    if token:
        login = check_github(call, token)
        if login is None:
            return 1
        site = setup_pages(call, token, login)
        if site is None:
            return 1
        updates["GITHUB_TOKEN"] = token
        updates.update(site)
    if not updates:
        say(YELLOW, "Nothing to connect. Run this again when you have a key.")
        return 1

    update_env_file(env_file, updates)
    say(GREEN, "✔ Saved. Restarting the agent so it starts publishing...")
    if not (do_restart or restart)(load_env_into(env_file)):
        return 1
    print()
    if site:
        base = site["AUTOMONETIZE_PAGES_BASE_URL"]
        if wait_for_site(call, base + "/", sleep=sleep):
            say(GREEN, f"✔ Your public site is live: {base}/")
        else:
            say(YELLOW, f"Your site will appear at {base}/ within a few minutes (GitHub is still building it).")
        say(GREEN, f"  Product pages are published on the agent's next cycle, e.g. {base}/python-remote/")
    if "DEVTO_API_KEY" in updates:
        say(GREEN, "  Dev.to: the first data article goes out on the agent's next syndication run (then every 5 days).")
    say(BOLD, "\nFrom now on the agent publishes and refreshes your pages and articles by itself.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
