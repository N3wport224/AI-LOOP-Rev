"""`automonetize go-live`: switch to real payments in one step, with checks first.

1. Asks for the live Stripe key (hidden input) and checks it with Stripe: it must be a live key and
   the account must be activated for charges (otherwise nothing is changed).
2. Checks email delivery: the settings are complete and the mailbox login works (buyers must get
   their file).
3. Writes STRIPE_SECRET_KEY, STRIPE_MODE=live and DRY_RUN=false to .env.
4. Restarts the agent (launchd or the background process) so it loads them; the restart runs a
   cycle at once, which creates the live checkout.
5. Waits for the live checkout link and prints it.
"""

from __future__ import annotations

import getpass
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GREEN, RED, YELLOW, BOLD, END = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


def say(colour: str, text: str) -> None:
    print(f"{colour}{text}{END}" if sys.stdout.isatty() else text, flush=True)


def stripe_get(key: str, path: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(f"https://api.stripe.com/v1/{path}", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https URL
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        return exc.code, body


def check_stripe(key: str) -> bool:
    if not key.startswith(("sk_live_", "rk_live_")):
        say(RED, "✘ That isn't a live key. It must start with rk_live_ or sk_live_ (Stripe dashboard with test mode OFF →")
        say(RED, "  Developers → API keys). Nothing was changed.")
        return False
    status, body = stripe_get(key, "balance")
    if status == 401:
        say(RED, "✘ Stripe rejected the key (copied incompletely, or deleted). Nothing was changed.")
        return False
    if status != 200 or body.get("livemode") is not True:
        say(RED, f"✘ Stripe answered {status}: {(body.get('error') or {}).get('message', body)}. Nothing was changed.")
        return False
    status, account = stripe_get(key, "account")
    if status == 200:
        if not account.get("charges_enabled"):
            say(RED, "✘ Your Stripe account isn't activated for real payments yet.")
            say(RED, "  Open https://dashboard.stripe.com → 'Activate payments' (identity + bank account), then run this again.")
            say(RED, "  Nothing was changed.")
            return False
        if not account.get("payouts_enabled"):
            say(YELLOW, "! Payments are on, but payouts aren't yet: add or verify your bank account in Stripe to get paid.")
    else:
        say(YELLOW, "! Couldn't read the account status with this key (permission); continuing.")
    say(GREEN, "✔ Stripe: live key accepted")
    return True


def check_email(env: dict[str, str]) -> bool:
    from agent.config import Config
    from agent.setup_autonomous import check_mailer_connectivity
    from agent.state import StateStore
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker

    config = Config.load(env=env)
    config.dry_run = False
    config.ensure_dirs()
    tools = build_toolkit(config, StateStore(config.db_path), CircuitBreaker(1000, 1000, 1000))
    problems = tools.dispatcher.compliance_problems("delivery")
    if problems:
        say(RED, "✘ Email delivery isn't ready, so buyers couldn't get their file:")
        for p in problems:
            say(RED, f"  - {p}")
        say(RED, "  Fix it in the control panel (Settings → Delivery mailer / Compliance), then run this again. Nothing was changed.")
        return False
    check = check_mailer_connectivity(config, tools.http)
    if check.status == "fail":
        say(RED, f"✘ Couldn't log in to your mailbox: {check.detail}")
        say(RED, "  For Gmail, use an App password (Google Account → Security → App passwords). Nothing was changed.")
        return False
    say(GREEN, f"✔ Email: {check.detail or 'settings complete'}")
    return True


def forget_test_listings(env: dict[str, str]) -> int:
    """Test-mode products and links don't exist in the live account: mark them unpublished so the
    agent recreates them with the live key on its next cycle."""
    from agent.config import Config
    from agent.state import StateStore

    state = StateStore(Config.load(env=env).db_path)
    cur = state._exec("UPDATE assets SET status = 'staged', checkout_url = NULL WHERE provider = 'stripe' AND status = 'published'")
    return cur.rowcount


def restart(env: dict[str, str]) -> bool:
    from agent.config import Config
    from agent.state import StateStore
    from gui.control import ServiceController

    config = Config.load(env=env)
    ctl = ServiceController(config, StateStore(config.db_path), ROOT, ROOT / ".env")
    if ctl.supervisor_pid() or ctl.launchd_managed():
        ctl._engine().resume()  # clear any pause or kill-switch stop
        result = ctl.restart()
    else:
        result = ctl.start()
    say(GREEN if result.get("ok") else RED, ("✔ " if result.get("ok") else "✘ ") + result.get("message", ""))
    return bool(result.get("ok"))


def wait_for_link(env: dict[str, str], minutes: int = 10) -> int:
    from agent.config import Config
    from agent.state import StateStore

    config = Config.load(env=env)
    state = StateStore(config.db_path)
    say(BOLD, f"Waiting for the agent to create your live checkout (up to {minutes} minutes)...")
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        live = [a for a in state.list_assets() if a.get("status") == "published"
                and str(a.get("checkout_url") or "").startswith("https://buy.stripe.com/") and "/test_" not in a["checkout_url"]]
        if live:
            print()
            say(GREEN, "✔ You're live. Share these links. Buyers pay on Stripe and get the file by email automatically:")
            for a in live:
                print(f"   {a['title']}  ${a['price_cents'] / 100:.2f}\n   {a['checkout_url']}\n")
            return 0
        time.sleep(15)
        print(".", end="", flush=True)
    print()
    last = state._one("SELECT detail FROM actions WHERE name = 'publish_listing' ORDER BY id DESC LIMIT 1")
    say(YELLOW, "The link isn't ready yet; the agent may still be busy collecting jobs. Check again in a few minutes with:")
    print("   automonetize go-live --link")
    if last:
        say(YELLOW, f"Latest publish attempt: {last['detail']}")
    return 1


def main(argv: list[str] | None = None) -> int:
    from agent.setup_autonomous import load_env_into
    from agent.tunnel import update_env_file

    import os

    argv = sys.argv[1:] if argv is None else argv
    os.chdir(ROOT)  # automonetize.toml and data/ are relative to the checkout
    env_file = ROOT / ".env"
    if not env_file.exists():
        say(RED, f"✘ {env_file} not found. Enter your settings in the control panel first (automonetize gui).")
        return 1
    if "--link" in argv:
        return wait_for_link(load_env_into(env_file), minutes=1)
    say(BOLD, "Switching AutoMonetize to real payments.")
    key = getpass.getpass("Paste your LIVE Stripe key (rk_live_… or sk_live_…; it stays hidden) and press Return: ").strip()
    if not check_stripe(key):
        return 1
    env = load_env_into(env_file)
    was_test = not str(env.get("STRIPE_SECRET_KEY") or env.get("STRIPE_API_KEY") or "").startswith(("sk_live_", "rk_live_"))
    env.update(STRIPE_SECRET_KEY=key, DRY_RUN="false")
    if not check_email(env):
        return 1
    update_env_file(env_file, {"STRIPE_SECRET_KEY": key, "STRIPE_MODE": "live", "DRY_RUN": "false"})
    say(GREEN, "✔ Saved: live Stripe key, real email delivery")
    if was_test and (n := forget_test_listings(load_env_into(env_file))):
        say(GREEN, f"✔ {n} test-mode listing(s) will be recreated in your live Stripe account")
    if not restart(load_env_into(env_file)):
        return 1
    return wait_for_link(load_env_into(env_file))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
