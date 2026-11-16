#!/usr/bin/env python3
"""Interactive browser login & automatic Home Assistant setup via LLAT.

How it works:
1. Opens a real browser window to https://www.originenergy.com.au so you can
   log in normally with your username/password (and MFA if enabled) without
   manually opening DevTools or hunting for cookies.
2. Automatically detects the `auth0` SSO cookie on `id.originenergy.com.au`
   as soon as your login completes.
3. Validates the cookie against Origin Energy's Auth0 silent PKCE exchange and
   retrieves the freshest rolled cookie.
4. Pushes the configuration directly into your Home Assistant instance using
   your Long-Lived Access Token (LLAT):
   - If a re-authentication flow is waiting, completes it automatically.
   - If the integration isn't configured yet, starts and completes the Config Flow.
   - If the integration is already configured, calls the `origin_energy.set_session_cookie`
     service to update and reload the entry seamlessly.

Requirements:
    pip install playwright aiohttp yarl
    playwright install chromium

Usage:
    export HA_URL="http://homeassistant.local:8123"
    export HA_TOKEN="eyJhbGciOi..."
    python3 tools/login_helper.py

    # Or pass flags / run interactively:
    python3 tools/login_helper.py --ha-url http://192.168.1.50:8123
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path
import types
from typing import Any

pkg = types.ModuleType("origin_energy")
pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components" / "origin_energy")]
sys.modules["origin_energy"] = pkg

import aiohttp

from origin_energy.api import OriginEnergyApiClient, pick_service
from origin_energy.auth import (
    AUTH0_COOKIE_NAMES,
    OriginEnergyAuthError,
    async_login_with_cookie,
    current_session_cookie,
)
from origin_energy.const import AUTH_DOMAIN, DOMAIN, SESSION_COOKIE_NAME

START_URL = "https://www.originenergy.com.au/login.html"
FALLBACK_URL = "https://www.originenergy.com.au/"


async def capture_cookie_via_browser(timeout_seconds: int = 300) -> str:
    """Launch a visible browser, wait for the user to log in, and return the `auth0` cookie."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for interactive browser login. "
            "Install it with:\n  pip install playwright aiohttp yarl && playwright install chromium"
        ) from exc

    print("\n" + "=" * 72)
    print("Opening browser to Origin Energy...")
    print("Please sign in with your Origin username and password in the window.")
    print("As soon as your login succeeds, this window will close automatically.")
    print("=" * 72 + "\n")

    async with async_playwright() as p:
        # Try launching installed Google Chrome first (looks completely native to Origin),
        # falling back to bundled Playwright Chromium if Chrome is not installed.
        try:
            browser = await p.chromium.launch(channel="chrome", headless=False)
        except Exception:  # noqa: BLE001
            browser = await p.chromium.launch(headless=False)

        context = await browser.new_context(viewport={"width": 1280, "height": 860})
        page = await context.new_page()

        try:
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001
            await page.goto(FALLBACK_URL, wait_until="domcontentloaded", timeout=30000)

        cookie_value: str | None = None
        authenticated_on_app = False
        elapsed = 0.0
        poll_interval = 1.0

        while elapsed < timeout_seconds:
            if page.is_closed():
                break

            cookies = await context.cookies(
                [f"https://{AUTH_DOMAIN}", "https://www.originenergy.com.au"]
            )
            found_auth0_cookies: dict[str, str] = {}
            for c in cookies:
                name = c.get("name", "")
                val = c.get("value", "")
                if (
                    name in AUTH0_COOKIE_NAMES
                    and AUTH_DOMAIN in c.get("domain", "")
                    and val
                ):
                    found_auth0_cookies[name] = val
                if name == "auth0.is.authenticated" and val == "true":
                    authenticated_on_app = True

            if SESSION_COOKIE_NAME in found_auth0_cookies:
                cookie_value = "; ".join(
                    f"{k}={v}" for k, v in found_auth0_cookies.items()
                )

            # Wait until both the Auth0 SSO cookie exists AND the SPA has completed callback
            if cookie_value and authenticated_on_app:
                break

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        await context.close()
        await browser.close()

    if not cookie_value:
        raise RuntimeError(
            "Did not detect the `auth0` session cookie before the browser closed or timed out."
        )

    return cookie_value


async def verify_cookie_with_origin(cookie: str) -> tuple[str, str | None, str | None]:
    """Validate the cookie via silent PKCE and return (rolled_cookie, agreement_id, nmi)."""
    async with aiohttp.ClientSession() as session:
        tokens = await async_login_with_cookie(session, cookie)
        rolled_cookie = current_session_cookie(session) or cookie

        client = OriginEnergyApiClient(session, tokens.access_token)
        service = None
        for svc_type in ("ELECTRICITY", "EMBEDDED_ELECTRICITY"):
            services = await client.async_get_services(svc_type)
            service = pick_service(services)
            if service:
                break
        agreement_id = service.get("agreementId") if service else None
        nmi = (service.get("meterPoint") or {}).get("identifier") if service else None
        return rolled_cookie, agreement_id, nmi


async def push_to_home_assistant(
    ha_url: str,
    ha_token: str,
    cookie: str,
    agreement_id: str | None = None,
) -> dict[str, Any]:
    """Push the session cookie directly to Home Assistant using a Long-Lived Access Token."""
    base_url = ha_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        # 1. Verify connectivity & token validity against /api/
        async with session.get(f"{base_url}/api/") as resp:
            if resp.status == 401:
                raise RuntimeError("Home Assistant rejected the Long-Lived Access Token (HTTP 401).")
            if resp.status != 200:
                raise RuntimeError(f"Could not reach Home Assistant API at {base_url}/api/ (HTTP {resp.status}).")

        # 2. Check if there is an active reauth flow in progress for origin_energy
        async with session.get(f"{base_url}/api/config/config_entries/flow") as resp:
            if resp.status == 200:
                flows = await resp.json()
                reauth_flows = [
                    f for f in flows
                    if f.get("handler") == DOMAIN and f.get("step_id") == "reauth_confirm"
                ]
                if reauth_flows:
                    flow_id = reauth_flows[0]["flow_id"]
                    print(f"Found pending Home Assistant re-auth flow ({flow_id}), completing...")
                    async with session.post(
                        f"{base_url}/api/config/config_entries/flow/{flow_id}",
                        json={"session_cookie": cookie},
                    ) as step_resp:
                        result = await step_resp.json()
                        return {"action": "reauth_completed", "result": result}

        # 3. Attempt to start a new Config Flow for origin_energy
        async with session.post(
            f"{base_url}/api/config/config_entries/flow",
            json={"handler": DOMAIN, "show_advanced_options": False},
        ) as resp:
            if resp.status == 400 or resp.status == 404:
                text = await resp.text()
                raise RuntimeError(
                    f"Home Assistant could not start the '{DOMAIN}' config flow (HTTP {resp.status}): {text}\n"
                    "Make sure you have copied `custom_components/origin_energy` to your Home Assistant "
                    "`config/custom_components/` directory and restarted Home Assistant."
                )
            flow_init = await resp.json()

        flow_id = flow_init["flow_id"]
        async with session.post(
            f"{base_url}/api/config/config_entries/flow/{flow_id}",
            json={"session_cookie": cookie},
        ) as resp:
            result = await resp.json()

        result_type = result.get("type")
        if result_type == "create_entry":
            return {"action": "created_entry", "result": result}

        # 4. If already configured, update the existing entry via our service call
        if result_type == "abort" and result.get("reason") == "already_configured":
            print("Origin Energy entry already exists in Home Assistant; updating cookie via service...")
            service_payload: dict[str, Any] = {"session_cookie": cookie}
            if agreement_id:
                service_payload["agreement_id"] = agreement_id
            async with session.post(
                f"{base_url}/api/services/{DOMAIN}/set_session_cookie",
                json=service_payload,
            ) as svc_resp:
                if svc_resp.status != 200:
                    text = await svc_resp.text()
                    raise RuntimeError(
                        f"Failed to call {DOMAIN}.set_session_cookie (HTTP {svc_resp.status}): {text}"
                    )
                return {"action": "updated_existing_entry", "result": await svc_resp.json()}

        if result_type == "form" and result.get("errors"):
            raise RuntimeError(f"Home Assistant config flow reported errors: {result['errors']}")

        return {"action": result_type or "unknown", "result": result}


async def async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign in to Origin Energy in a browser and push the session straight into Home Assistant."
    )
    parser.add_argument(
        "--ha-url",
        default=os.environ.get("HA_URL"),
        help="Home Assistant base URL (e.g. http://homeassistant.local:8123). Can also set HA_URL.",
    )
    parser.add_argument(
        "--ha-token",
        default=os.environ.get("HA_TOKEN"),
        help="Home Assistant Long-Lived Access Token. Can also set HA_TOKEN.",
    )
    parser.add_argument(
        "--cookie",
        default=os.environ.get("ORIGIN_SESSION_COOKIE"),
        help="Optional existing auth0 cookie (skips browser login if provided).",
    )
    parser.add_argument(
        "--no-ha-push",
        action="store_true",
        help="Only capture and validate the Origin cookie without pushing to Home Assistant.",
    )
    args = parser.parse_args()

    ha_url = args.ha_url
    ha_token = args.ha_token

    if not args.no_ha_push:
        if not ha_url:
            ha_url = input("Home Assistant URL [http://homeassistant.local:8123]: ").strip() or "http://homeassistant.local:8123"
        if not ha_token:
            ha_token = getpass.getpass("Home Assistant Long-Lived Access Token (LLAT): ").strip()
            if not ha_token:
                print("Error: A Long-Lived Access Token is required to push config to Home Assistant.")
                return 1

    # Step 1: Capture cookie via interactive browser window (unless already supplied)
    try:
        cookie = args.cookie or await capture_cookie_via_browser()
    except Exception as err:  # noqa: BLE001
        print(f"\n[ERROR] Browser login failed: {err}")
        return 1

    # Step 2: Verify cookie against Origin Energy and get rolled cookie + NMI
    print("Verifying session against Origin Energy GraphQL API...")
    try:
        rolled_cookie, agreement_id, nmi = await verify_cookie_with_origin(cookie)
    except OriginEnergyAuthError as err:
        print(f"\n[ERROR] Origin Energy authentication failed: {err}")
        return 1

    print(
        f"Verified! Active Electricity Agreement: {agreement_id} (NMI: {nmi})"
    )

    if args.no_ha_push:
        print("\nCaptured `auth0` session cookie:")
        print(rolled_cookie)
        return 0

    # Step 3: Push directly into Home Assistant via LLAT
    print(f"\nPushing configuration to Home Assistant ({ha_url})...")
    try:
        outcome = await push_to_home_assistant(
            ha_url=ha_url,
            ha_token=ha_token,
            cookie=rolled_cookie,
            agreement_id=agreement_id,
        )
    except Exception as err:  # noqa: BLE001
        print(f"\n[ERROR] Failed to push to Home Assistant: {err}")
        print("\nFallback — you can paste this cookie manually into the HA UI:")
        print(rolled_cookie)
        return 1

    action = outcome["action"]
    if action == "created_entry":
        title = outcome["result"].get("title", "Origin Energy")
        print(f"\nSUCCESS! Created new Home Assistant integration entry: '{title}'.")
    elif action == "reauth_completed":
        print("\nSUCCESS! Completed pending Home Assistant re-authentication.")
    elif action == "updated_existing_entry":
        print("\nSUCCESS! Updated session cookie and reloaded existing Home Assistant entry.")
    else:
        print(f"\nCompleted with status '{action}': {outcome['result']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))

