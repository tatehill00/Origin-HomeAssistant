#!/usr/bin/env python3
"""Standalone CLI tool to verify your Origin Energy session cookie & inspect live data.

Tests:
1. Silent Auth0 PKCE exchange using your imported `auth0` session cookie.
2. Electricity service discovery, tariff rates, and daily supply charges.
3. Current billing cycle totals (usage, cost, balance, next bill date).
4. Recent hourly electricity usage points.
5. Embedded Hot Water (water usage) service discovery, meter readings, and rates.

Usage:
    ORIGIN_SESSION_COOKIE="s%3A..." python3 tools/test_connection.py
    # Or run interactively (prompts securely for cookie):
    python3 tools/test_connection.py
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

pkg = types.ModuleType("origin_energy")
pkg.__path__ = [str((Path(__file__).resolve().parent.parent / "custom_components" / "origin_energy"))]
sys.modules["origin_energy"] = pkg

import aiohttp

from origin_energy.api import OriginEnergyApiClient, pick_service, pick_time_unit
from origin_energy.auth import OriginEnergyAuthError, async_login_with_cookie
from origin_energy.const import SERVICE_TYPES


async def main() -> int:
    cookie = os.environ.get("ORIGIN_SESSION_COOKIE") or getpass.getpass(
        "Paste your id.originenergy.com.au 'auth0' cookie value: "
    ).strip()

    if not cookie:
        print("Error: Empty cookie value provided.")
        return 1

    async with aiohttp.ClientSession() as session:
        print("1. Exchanging Auth0 session cookie via silent PKCE...")
        try:
            tokens = await async_login_with_cookie(session, cookie)
        except OriginEnergyAuthError as err:
            print(f"   FAILED: {err}")
            return 1

        print(f"   SUCCESS! Minted Bearer token (expires in {tokens.expires_in}s).")
        client = OriginEnergyApiClient(session, tokens.access_token)

        print("\n2. Checking Account Billing & Balance...")
        accounts = await client.async_get_accounts()
        for acct in accounts:
            bal = (acct.get("balance") or 0) * 0.01
            print(
                f"   Account {acct.get('accountNumber')} ({acct.get('status')}): "
                f"Balance=${bal:.2f}, PeriodStart={acct.get('currentBillingPeriodStartDate')}, "
                f"NextBill={acct.get('nextBillDate')}"
            )

        print("\n3. Discovering Services across all fuel/water types...")
        for svc_type in SERVICE_TYPES:
            services = await client.async_get_services(svc_type)
            print(f"   - {svc_type}: {len(services)} service(s) found")
            if not services:
                continue

            active_svc = pick_service(services)
            if not active_svc:
                continue

            agreement_id = active_svc.get("agreementId")
            mp = active_svc.get("meterPoint") or {}
            product = active_svc.get("product") or {}
            print(
                f"     Active Agreement: {agreement_id} | Identifier (NMI/Meter): {mp.get('identifier')} "
                f"| Plan: {product.get('name')}"
            )
            for rate in product.get("rates") or []:
                print(
                    f"       Rate [{rate.get('bandCategory')} / {rate.get('timeOfDay')}]: "
                    f"{rate.get('rateIncludingTax')} ({rate.get('description')})"
                )

            if svc_type in ("ELECTRICITY", "EMBEDDED_ELECTRICITY") and agreement_id:
                granularity = pick_time_unit(active_svc.get("availableUsageTimeUnits"))
                if svc_type == "EMBEDDED_ELECTRICITY" and granularity == "DAILY":
                    granularity = "HOURLY"
                now = datetime.now(timezone.utc)
                billing = await client.async_get_usage(
                    agreement_id, now - timedelta(days=90), now, "BILLING", svc_type
                )
                if billing.points:
                    pt = billing.points[-1]
                    print(
                        f"     Current Billing Cycle Usage: {pt.get('totalConsumedEnergy')} "
                        f"{billing.consumption_unit}, Total Cost: {pt.get('totalCost')} {billing.cost_unit}"
                    )

                window = await client.async_get_usage(
                    agreement_id, now - timedelta(days=3), now, granularity, svc_type
                )
                actuals = [p for p in window.points if p.get("totalConsumedEnergy") is not None]
                print(
                    f"     Recent {granularity} Usage: {len(actuals)} non-null points "
                    f"(lastReadingDate={window.last_reading_date})"
                )

            if svc_type == "EMBEDDED_HOT_WATER":
                print("     Hot Water Meter Details:")
                print("     " + json.dumps(mp, indent=2)[:1000])

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

