"""Mint Origin Energy API tokens from an imported browser session cookie.

Origin places Kasada bot protection in front of the interactive password login
endpoint (`POST /usernamepassword/login`), but leaves the Auth0 silent PKCE
authorization (`/authorize?prompt=none`), token exchange (`/oauth/token`), and
GraphQL API open to standard HTTP clients with browser headers.

Supports both:
- Single `auth0` cookie value (`s%3A...`) for accounts without MFA.
- Full cookie header string or multiple Auth0 cookies (`auth0`, `auth0-mf`, `did`)
  for accounts with Multi-Factor Authentication (MFA) enabled.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass

from aiohttp import ClientSession
from yarl import URL

from .const import (
    AUDIENCE,
    AUTH0_SPA_CLIENT,
    AUTH_DOMAIN,
    AUTHORIZE_URL,
    BROWSER_HEADERS,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPE,
    SESSION_COOKIE_NAME,
    TOKEN_URL,
)

AUTH_BASE = URL(f"https://{AUTH_DOMAIN}/")

# Cookies on id.originenergy.com.au that Auth0 uses for SSO + MFA device trust
AUTH0_COOKIE_NAMES = (
    SESSION_COOKIE_NAME,
    "auth0_compat",
    "auth0-mf",
    "auth0-mf_compat",
    "did",
    "did_compat",
)


class OriginEnergyAuthError(Exception):
    """Raised when tokens could not be obtained."""


class OriginEnergyBlocked(OriginEnergyAuthError):
    """Origin's WAF refused the request before it reached Auth0."""


class OriginEnergyMfaRequired(OriginEnergyAuthError):
    """Auth0 requires the device trust cookie (`did`) alongside `auth0`."""


class OriginEnergySessionExpired(OriginEnergyAuthError):
    """The imported browser session cookie has expired and needs re-authentication."""


@dataclass
class OriginEnergyTokens:
    """Tokens returned by the silent PKCE code exchange."""

    access_token: str
    id_token: str
    expires_in: int


def _code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def parse_cookie_input(raw_input: str) -> dict[str, str]:
    """Parse either a bare `auth0` cookie value or a `name=value; name2=value2` cookie string."""
    cleaned = raw_input.strip()
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned.split(":", 1)[1].strip()

    # If the user pasted a semicolon-separated Cookie header or `auth0=...` pairs
    if ";" in cleaned or any(f"{name}=" in cleaned for name in AUTH0_COOKIE_NAMES):
        cookies: dict[str, str] = {}
        for part in cleaned.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k in AUTH0_COOKIE_NAMES and v:
                    cookies[k] = v
            elif part.startswith("s%3A") and SESSION_COOKIE_NAME not in cookies:
                cookies[SESSION_COOKIE_NAME] = part
        if cookies:
            return cookies

    return {SESSION_COOKIE_NAME: cleaned}


def seed_session_cookie(session: ClientSession, cookie_input: str) -> None:
    """Load the imported Auth0 SSO (and MFA) cookies into the session's cookie jar."""
    cookies = parse_cookie_input(cookie_input)
    session.cookie_jar.update_cookies(cookies, response_url=AUTH_BASE)


def current_session_cookie(session: ClientSession) -> str | None:
    """Serialize all active Auth0 SSO/MFA cookies back out so rolled expiries are persisted."""
    jar_cookies = session.cookie_jar.filter_cookies(AUTH_BASE)
    parts: list[str] = []
    for name in AUTH0_COOKIE_NAMES:
        morsel = jar_cookies.get(name)
        if morsel and morsel.value:
            parts.append(f"{name}={morsel.value}")

    if not parts:
        return None
    if len(parts) == 1 and parts[0].startswith(f"{SESSION_COOKIE_NAME}="):
        return parts[0].split("=", 1)[1]
    return "; ".join(parts)


async def async_get_tokens(session: ClientSession) -> OriginEnergyTokens:
    """Run the silent PKCE exchange using the session cookies in the jar."""
    verifier = _code_verifier()
    async with session.get(
        AUTHORIZE_URL,
        headers=BROWSER_HEADERS,
        params={
            "client_id": CLIENT_ID,
            "audience": AUDIENCE,
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "issuer": AUTH_DOMAIN,
            "auth0Client": AUTH0_SPA_CLIENT,
            "remember_me": "true",
            "response_type": "code",
            "response_mode": "web_message",
            "state": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24),
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "none",
        },
    ) as resp:
        if resp.status in (403, 429):
            raise OriginEnergyBlocked(
                f"Origin refused the sign-in request (HTTP {resp.status}) before it "
                "reached Auth0. This is bot protection, not an invalid session cookie."
            )
        if resp.status != 200:
            raise OriginEnergyAuthError(f"Silent auth failed: HTTP {resp.status}")
        text = await resp.text()

    match = re.search(r'"code"\s*:\s*"([^"]+)"', text)
    if not match:
        error_match = re.search(r'"error"\s*:\s*"([^"]+)"', text)
        desc_match = re.search(r'"error_description"\s*:\s*"([^"]+)"', text)
        error = error_match.group(1) if error_match else None
        desc = desc_match.group(1) if desc_match else ""

        if "multifactor" in desc.lower() or error == "mfa_required":
            raise OriginEnergyMfaRequired(
                "Auth0 requires both the `auth0` and `did` cookies from "
                "id.originenergy.com.au (e.g. `auth0=s%3A...; did=s%3A...`)."
            )
        if error in ("login_required", "consent_required", "interaction_required"):
            raise OriginEnergySessionExpired(
                f"Origin Energy session is no longer valid ({error}: {desc}). Sign in again "
                "in your browser and import a fresh session cookie."
            )
        raise OriginEnergyAuthError(
            f"Silent auth did not return a code ({error or 'no code in response'}: {desc})"
        )

    async with session.post(
        TOKEN_URL,
        headers=BROWSER_HEADERS,
        json={
            "redirect_uri": REDIRECT_URI,
            "remember_me": True,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
            "code": match.group(1),
            "grant_type": "authorization_code",
        },
    ) as resp:
        if resp.status != 200:
            raise OriginEnergyAuthError(f"Token exchange failed: HTTP {resp.status}")
        token_data = await resp.json()

    return OriginEnergyTokens(
        access_token=token_data["access_token"],
        id_token=token_data["id_token"],
        expires_in=token_data["expires_in"],
    )


async def async_login_with_cookie(
    session: ClientSession, cookie_value: str
) -> OriginEnergyTokens:
    """Seed imported session cookie(s) and mint tokens from them."""
    seed_session_cookie(session, cookie_value)
    return await async_get_tokens(session)
