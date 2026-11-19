"""GraphQL client for Origin Energy's Kraken-backed gateway."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import ClientSession

from .const import (
    BROWSER_HEADERS,
    DEFAULT_QUERY_DAYS,
    DEFAULT_TIME_UNIT,
    GRAPHQL_URL,
    MAX_QUERY_DAYS,
    PREFERRED_TIME_UNITS,
    REQUEST_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Fetches services across Electricity, Gas, Embedded Electricity, and Embedded Hot Water,
# matching the exact schema fields for each union member.
ACCOUNT_DETAILS_QUERY = """
query AccountDetailsQuery($type: ServiceType!) {
  viewer {
    kraken {
      services(type: $type) {
        __typename
        ... on ElectricityService {
          agreementId
          type
          active
          validFrom
          validTo
          serviceState
          availableUsageTimeUnits
          product {
            code
            name
            description
            rates(filter: {active_equal: true}) {
              rateIncludingTax
              rateExcludingTax
              description
              timeOfDay
              bandCategory
              networkTariffCode
              chargeDescription
              stepStart
              stepEnd
              active
            }
          }
          meterPoint {
            identifier
            meters {
              serialNumber
              active
              nextScheduledReadDate
              registers {
                identifier
                timeOfDay
                networkTariffCode
                typeOfMeasure
                energyDirection
              }
            }
          }
          property {
            address {
              components {
                timezone
              }
            }
          }
        }
        ... on EmbeddedElectricityService {
          agreementId
          type
          active
          validFrom
          validTo
          serviceState
          availableUsageTimeUnits
          product {
            code
            name
            description
            rates(filter: {active_equal: true}) {
              rateIncludingTax
              rateExcludingTax
              description
              timeOfDay
              bandCategory
              stepStart
              stepEnd
              active
            }
          }
          meterPoint {
            identifier
          }
          property {
            address {
              components {
                timezone
              }
            }
          }
        }
        ... on EmbeddedHotWaterService {
          agreementId
          type
          active
          validFrom
          validTo
          serviceState
          availableUsageTimeUnits
          product {
            code
            name
            description
            rates(filter: {active_equal: true}) {
              rateIncludingTax
              rateExcludingTax
              description
              timeOfDay
              bandCategory
              stepStart
              stepEnd
              active
            }
          }
          meterPoint {
            identifier
            meters {
              serialNumber
              active
              nextScheduledReadDate
              readings(first: 25) {
                edges {
                  node {
                    value
                    readAt
                    type
                  }
                }
              }
            }
          }
          property {
            address {
              components {
                timezone
              }
            }
          }
        }
        ... on GasService {
          agreementId
          type
          active
          validFrom
          validTo
          serviceState
          availableUsageTimeUnits
          product {
            code
            name
            description
            rates(filter: {active_equal: true}) {
              rateIncludingTax
              rateExcludingTax
              description
              timeOfDay
              bandCategory
              stepStart
              stepEnd
              active
            }
          }
          meterPoint {
            identifier
          }
          property {
            address {
              components {
                timezone
              }
            }
          }
        }
      }
    }
  }
}
"""

ACCOUNT_BILLING_QUERY = """
query AccountBillingQuery {
  viewer {
    kraken {
      accounts {
        accountNumber
        status
        accountType
        balance
        nextBillDate
        currentBillingPeriodStartDate
        dueCharges {
          amount
          dueDate
        }
        bills(first: 6) {
          edges {
            node {
              billId
              billType
              fromDate
              toDate
              issuedDate
              paymentDueDate
              isFinal
            }
          }
        }
      }
    }
  }
}
"""

# Note the deliberate absence of a `filter` argument: Origin's backend crashes
# on an explicit `filter: null`.
ACCOUNT_USAGE_QUERY = """
query AccountUsageQuery($agreementId: String!, $type: ServiceType!, $timeUnit: UsageTimeUnit!, $startDate: DateTime!, $endDate: DateTime!) {
  viewer {
    kraken {
      service(type: $type, agreementId: $agreementId) {
        ... on ElectricityService {
          usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
            lastReadingDate
            hasFeedIn
            hasMissingDataPoints
            unitsOfMeasure {
              totalConsumedEnergy
              totalCost
            }
            dataPoints {
              type
              startDate
              endDate
              totalConsumedEnergy
              totalCost
              usageCost
              supplyCost
              totalFeedInEnergy
              totalEarnings
            }
          }
        }
        ... on EmbeddedElectricityService {
          usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
            lastReadingDate
            hasFeedIn
            hasMissingDataPoints
            unitsOfMeasure {
              totalConsumedEnergy
              totalCost
            }
            dataPoints {
              type
              startDate
              endDate
              totalConsumedEnergy
              totalCost
              usageCost
              supplyCost
            }
          }
        }
        ... on EmbeddedHotWaterService {
          usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
            lastReadingDate
            hasFeedIn
            hasMissingDataPoints
            unitsOfMeasure {
              totalConsumedEnergy
              totalCost
            }
            dataPoints {
              type
              startDate
              endDate
              totalConsumedEnergy
              totalCost
              usageCost
              supplyCost
            }
          }
        }
        ... on GasService {
          usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
            lastReadingDate
            hasFeedIn
            hasMissingDataPoints
            unitsOfMeasure {
              totalConsumedEnergy
              totalCost
            }
            dataPoints {
              type
              startDate
              endDate
              totalConsumedEnergy
              totalCost
              usageCost
              supplyCost
            }
          }
        }
      }
    }
  }
}
"""

RETRYABLE_ERROR_CODES = frozenset({"INTERNAL_SERVER_ERROR", "EXTERNAL_API_ERROR"})
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


class OriginEnergyApiError(Exception):
    """Raised on transport or GraphQL-level errors."""


class _RetryableError(OriginEnergyApiError):
    """A failure that is worth another attempt."""


@dataclass(frozen=True)
class UsageWindow:
    """One usage query's data points plus the envelope metadata."""

    points: list[dict[str, Any]]
    last_reading_date: datetime | None
    consumption_unit: str | None
    cost_unit: str | None
    has_feed_in: bool
    has_missing_points: bool


@dataclass
class AccountSummary:
    """Snapshot of billing, tariff rates, daily/projected usage, solar, gas, and hot water."""

    account_number: str | None = None
    balance_dollars: float | None = None
    next_bill_date: str | None = None
    next_bill_datetime: datetime | None = None
    days_until_next_bill: int | None = None
    billing_period_start: str | None = None
    due_amount_dollars: float | None = None
    due_date: str | None = None
    plan_name: str | None = None
    standing_charge_dollars: float | None = None
    solar_feed_in_rate_dollars: float | None = None
    usage_rates: list[dict[str, Any]] = field(default_factory=list)
    current_cycle_usage_kwh: float | None = None
    current_cycle_cost_dollars: float | None = None
    current_cycle_supply_dollars: float | None = None
    current_cycle_usage_charge_dollars: float | None = None
    projected_bill_cost_dollars: float | None = None
    projected_bill_usage_kwh: float | None = None
    yesterday_usage_kwh: float | None = None
    yesterday_cost_dollars: float | None = None
    yesterday_export_kwh: float | None = None
    yesterday_date: str | None = None
    has_solar_export: bool = False
    hot_water_agreement_id: str | None = None
    hot_water_identifier: str | None = None
    hot_water_latest_reading: float | None = None
    hot_water_latest_read_at: str | None = None
    hot_water_current_cycle_usage: float | None = None
    hot_water_current_cycle_cost_dollars: float | None = None
    hot_water_usage_unit: str | None = None
    hot_water_rates: list[dict[str, Any]] = field(default_factory=list)
    gas_agreement_id: str | None = None
    gas_identifier: str | None = None
    gas_current_cycle_usage_mj: float | None = None
    gas_current_cycle_cost_dollars: float | None = None


def pick_time_unit(available: list[str] | None) -> str:
    """Choose the finest usage granularity a service supports."""
    offered = set(available or ())
    return next(
        (unit for unit in PREFERRED_TIME_UNITS if unit in offered), DEFAULT_TIME_UNIT
    )


def pick_service(
    services: list[dict[str, Any]], agreement_id: str | None = None
) -> dict[str, Any] | None:
    """Choose the live agreement from an account's services."""
    if not services:
        return None
    if agreement_id is not None:
        matched = next(
            (s for s in services if s.get("agreementId") == agreement_id), None
        )
        if matched is not None:
            return matched

    pool = [s for s in services if s.get("active")] or services
    return max(pool, key=lambda s: s.get("validFrom") or "")


def chunk_days(time_unit: str) -> int:
    """Days per query that stay inside Origin's range limit."""
    return MAX_QUERY_DAYS.get(time_unit, DEFAULT_QUERY_DAYS)


def parse_api_datetime(value: str | None) -> datetime | None:
    """Parse one of Origin's timestamps, tolerating the trailing Z."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_api_datetime(value: datetime) -> str:
    """Render an instant in UTC ISO format as expected by Origin's DateTime scalar."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _dig(data: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


class OriginEnergyApiClient:
    """GraphQL client for api.rx.originenergy.com.au."""

    def __init__(self, session: ClientSession, access_token: str) -> None:
        self._session = session
        self._headers = {
            **BROWSER_HEADERS,
            "Authorization": f"Bearer {access_token}",
        }

    async def _attempt(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One round trip, raising _RetryableError for transient failures."""
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        try:
            async with self._session.post(
                GRAPHQL_URL,
                json=body,
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status >= 500:
                    raise _RetryableError(f"GraphQL server error: {resp.status}")
                if resp.status != 200:
                    raise OriginEnergyApiError(f"GraphQL request failed: {resp.status}")
                payload = await resp.json()
        except OriginEnergyApiError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _RetryableError(f"Error connecting to Origin Energy: {err}") from err
        except Exception as err:  # noqa: BLE001
            raise OriginEnergyApiError(str(err)) from err

        if errors := payload.get("errors"):
            codes = {(err.get("extensions") or {}).get("code") for err in errors}
            if codes & RETRYABLE_ERROR_CODES:
                raise _RetryableError(str(errors))
            raise OriginEnergyApiError(str(errors))

        return payload["data"]

    async def _graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a query, retrying transient server errors."""
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            try:
                return await self._attempt(query, variables)
            except _RetryableError as err:
                if attempt == REQUEST_ATTEMPTS:
                    raise
                delay = RETRY_BACKOFF_SECONDS * attempt
                _LOGGER.debug(
                    "Origin Energy request failed (attempt %d/%d), retrying in %ds: %s",
                    attempt,
                    REQUEST_ATTEMPTS,
                    delay,
                    err,
                )
                await asyncio.sleep(delay)

        raise OriginEnergyApiError("Request failed")

    async def async_get_services(
        self, service_type: str = "ELECTRICITY"
    ) -> list[dict[str, Any]]:
        """Return the account's services of the given type."""
        data = await self._graphql(ACCOUNT_DETAILS_QUERY, {"type": service_type})
        return _dig(data, "viewer", "kraken", "services", default=[])

    async def async_get_accounts(self) -> list[dict[str, Any]]:
        """Return the account's billing and balance records."""
        data = await self._graphql(ACCOUNT_BILLING_QUERY)
        return _dig(data, "viewer", "kraken", "accounts", default=[])

    async def async_get_usage(
        self,
        agreement_id: str,
        start_date: datetime,
        end_date: datetime,
        time_unit: str = "HOURLY",
        service_type: str = "ELECTRICITY",
    ) -> UsageWindow:
        """Return usage data points and metadata for one service agreement."""
        data = await self._graphql(
            ACCOUNT_USAGE_QUERY,
            {
                "agreementId": agreement_id,
                "type": service_type,
                "timeUnit": time_unit,
                "startDate": format_api_datetime(start_date),
                "endDate": format_api_datetime(end_date),
            },
        )
        usage = _dig(data, "viewer", "kraken", "service", "usage", default={})
        units = usage.get("unitsOfMeasure") or {}

        return UsageWindow(
            points=usage.get("dataPoints") or [],
            last_reading_date=parse_api_datetime(usage.get("lastReadingDate")),
            consumption_unit=units.get("totalConsumedEnergy"),
            cost_unit=units.get("totalCost"),
            has_feed_in=bool(usage.get("hasFeedIn")),
            has_missing_points=bool(usage.get("hasMissingDataPoints")),
        )
