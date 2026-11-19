"""DataUpdateCoordinator for Origin Energy.

Writes historical and daily usage/cost directly into Home Assistant's
long-term statistics (`async_add_external_statistics`) for the Energy Dashboard:
- Electricity consumption (`kWh`) & cost (`$`)
- Solar feed-in export (`kWh`) & solar earnings (`$`) when present
- Gas consumption (`kWh`) & cost (`$`) when present
- Hot Water consumption (`L`) & cost (`$`) when present

Also populates live sensors for tariff rates, billing cycle totals, projected
bill costs, yesterday's usage/cost, next bill countdown, account balance,
and Embedded Hot Water readings/rates.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import (
    AccountSummary,
    OriginEnergyApiClient,
    OriginEnergyApiError,
    UsageWindow,
    chunk_days,
    parse_api_datetime,
    pick_service,
    pick_time_unit,
)
from .auth import (
    OriginEnergyAuthError,
    OriginEnergySessionExpired,
    async_get_tokens,
    current_session_cookie,
    seed_session_cookie,
)
from .const import (
    CONF_AGREEMENT_ID,
    CONF_BACKFILL_COMPLETE,
    CONF_OVERRIDE_FEED_IN_TARIFF,
    CONF_OVERRIDE_SUPPLY_CHARGE,
    CONF_OVERRIDE_USAGE_RATE,
    CONF_SERVICE_TYPE,
    CONF_SESSION_COOKIE,
    CONF_TIME_UNIT,
    CONF_TIMEZONE,
    CONF_UPDATE_INTERVAL_HOURS,
    DEFAULT_TIME_UNIT,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    FIRST_RUN_HISTORY_DAYS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_UPDATE_INTERVAL_HOURS,
    OVERLAP_DAYS,
)

_LOGGER = logging.getLogger(__name__)

type OriginEnergyConfigEntry = ConfigEntry["OriginEnergyCoordinator"]

# Unit conversions to kWh (including MJ for Australian Gas meters: 1 MJ = 1/3.6 kWh)
ENERGY_TO_KWH = {
    "WATT_HOUR": 0.001,
    "KILOWATT_HOUR": 1.0,
    "MEGAWATT_HOUR": 1000.0,
    "MEGAJOULE": 1.0 / 3.6,
    "MJ": 1.0 / 3.6,
}
ENERGY_TO_MJ = {
    "MEGAJOULE": 1.0,
    "MJ": 1.0,
    "KILOWATT_HOUR": 3.6,
    "WATT_HOUR": 0.0036,
}
WATER_TO_LITERS = {
    "LITRE": 1.0,
    "LITER": 1.0,
    "KILOLITRE": 1000.0,
}
COST_TO_DOLLARS = {"CENT": 0.01, "DOLLAR": 1.0}

BASELINE_LOOKBACK_DAYS = 30
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def configured_update_interval(config_entry: ConfigEntry) -> timedelta:
    """Poll interval from the entry's options, clamped to [1, 24] hours."""
    hours = config_entry.options.get(
        CONF_UPDATE_INTERVAL_HOURS, DEFAULT_UPDATE_INTERVAL_HOURS
    )
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = DEFAULT_UPDATE_INTERVAL_HOURS
    hours = min(max(hours, MIN_UPDATE_INTERVAL_HOURS), MAX_UPDATE_INTERVAL_HOURS)
    return timedelta(hours=hours)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        val = float(value)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def statistic_slug(value: str) -> str:
    """Reduce an identifier to the character set statistic IDs allow."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "unknown"


@dataclass
class OriginEnergyData:
    """Coordinator payload returned to entities."""

    last_updated: datetime
    summary: AccountSummary = field(default_factory=AccountSummary)


@dataclass
class _ImportCursor:
    """Running totals carried across query windows."""

    consumption_sum: float = 0.0
    cost_sum: float = 0.0
    export_sum: float = 0.0
    earnings_sum: float = 0.0
    last_start: datetime | None = None
    written: int = 0


class OriginEnergyCoordinator(DataUpdateCoordinator[OriginEnergyData]):
    """Fetch usage, tariffs, billing totals, gas, and water services from Origin Energy."""

    config_entry: OriginEnergyConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: OriginEnergyConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=configured_update_interval(config_entry),
        )
        self._session = async_create_clientsession(hass)
        seed_session_cookie(self._session, config_entry.data[CONF_SESSION_COOKIE])
        self.agreement_id: str = config_entry.data[CONF_AGREEMENT_ID]
        self.service_type: str = config_entry.data.get(CONF_SERVICE_TYPE, "ELECTRICITY")
        self.timezone = ZoneInfo(config_entry.data[CONF_TIMEZONE])
        self.time_unit: str = config_entry.data.get(CONF_TIME_UNIT, DEFAULT_TIME_UNIT)

        id_prefix = statistic_slug(self.agreement_id)
        self.consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
        self.cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"
        self.export_statistic_id = f"{DOMAIN}:{id_prefix}_energy_export"
        self.earnings_statistic_id = f"{DOMAIN}:{id_prefix}_energy_earnings"

        self._backfill_task: asyncio.Task[None] | None = None
        self._import_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self.summary: AccountSummary = AccountSummary()
        self._last_options: dict[str, Any] = dict(config_entry.options)

    def async_adopt_options(self) -> bool:
        """Take up newly-configured options (poll interval or tariff overrides). True if changed."""
        changed = False
        interval = configured_update_interval(self.config_entry)
        if interval != self.update_interval:
            self.update_interval = interval
            changed = True
        current_options = dict(self.config_entry.options)
        if current_options != self._last_options:
            self._last_options = current_options
            changed = True
        return changed

    async def async_trigger_backfill(self) -> None:
        """Reset backfill completion flag and start a full historical re-import."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_BACKFILL_COMPLETE: False},
        )
        self._start_backfill(force_restart=True)

    async def async_close(self) -> None:
        """Stop the backfill before the entry finishes unloading."""
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._backfill_task

    def _persist_rolled_cookie(self) -> None:
        """Save the session cookie back if Auth0 rolled its expiry forward."""
        cookie = current_session_cookie(self._session)
        if not cookie or cookie == self.config_entry.data[CONF_SESSION_COOKIE]:
            return
        _LOGGER.debug("Origin Energy session cookie refreshed, persisting")
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_SESSION_COOKIE: cookie},
        )

    async def _async_api(self) -> OriginEnergyApiClient:
        """Return a client holding a currently-valid access token."""
        now = dt_util.utcnow()
        if (
            self._access_token is not None
            and self._token_expires_at is not None
            and self._token_expires_at - now > TOKEN_REFRESH_MARGIN
        ):
            return OriginEnergyApiClient(self._session, self._access_token)

        try:
            tokens = await async_get_tokens(self._session)
        except OriginEnergySessionExpired as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OriginEnergyAuthError as err:
            raise UpdateFailed(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Error connecting to Origin Energy: {err}") from err

        self._access_token = tokens.access_token
        self._token_expires_at = now + timedelta(seconds=tokens.expires_in)
        self._persist_rolled_cookie()
        return OriginEnergyApiClient(self._session, self._access_token)

    async def _async_update_data(self) -> OriginEnergyData:
        api = await self._async_api()

        summary = await self._async_fetch_summary(api)
        self.summary = summary

        if self.config_entry.data.get(CONF_BACKFILL_COMPLETE):
            try:
                async with self._import_lock:
                    await self._async_import_recent()
                    await self._async_import_secondary_services(api)
            except OriginEnergyApiError as err:
                raise UpdateFailed(str(err)) from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise UpdateFailed(f"Error connecting to Origin Energy: {err}") from err
        else:
            self._start_backfill()

        return OriginEnergyData(last_updated=dt_util.utcnow(), summary=summary)

    # ------------------------------------------------------------------
    # Tariff, Billing Cycle, Projections, Yesterday, & Water/Gas Summary
    # ------------------------------------------------------------------

    async def _async_fetch_summary(self, api: OriginEnergyApiClient) -> AccountSummary:
        """Fetch account billing cycle, tariff rates, yesterday's usage, and hot water/gas."""
        summary = AccountSummary()
        now = dt_util.now(self.timezone)

        override_usage_rate = _optional_float(
            self.config_entry.options.get(CONF_OVERRIDE_USAGE_RATE)
        )
        override_supply_charge = _optional_float(
            self.config_entry.options.get(CONF_OVERRIDE_SUPPLY_CHARGE)
        )
        override_fit = _optional_float(
            self.config_entry.options.get(CONF_OVERRIDE_FEED_IN_TARIFF)
        )

        # 1. Electricity service & tariff rates
        with suppress(OriginEnergyApiError, aiohttp.ClientError, TimeoutError):
            services = await api.async_get_services(self.service_type)
            service = pick_service(services, self.agreement_id)
            if service:
                product = service.get("product") or {}
                summary.plan_name = product.get("name") or product.get("description")
                rates = product.get("rates") or []
                for rate in rates:
                    band = (rate.get("bandCategory") or "").upper()
                    val = rate.get("rateIncludingTax")
                    if val is None:
                        continue
                    dollars = round(float(val) * 0.01, 4) if float(val) > 2.0 else round(float(val), 4)
                    if band == "STANDING_CHARGE" and summary.standing_charge_dollars is None:
                        summary.standing_charge_dollars = dollars
                    elif "EXPORT" in band or "FEED" in band or "SOLAR" in band:
                        summary.solar_feed_in_rate_dollars = dollars
                    elif band == "CONSUMPTION_CHARGE":
                        summary.usage_rates.append(
                            {
                                "time_of_day": rate.get("timeOfDay") or "ALLDAY",
                                "description": rate.get("description") or rate.get("chargeDescription"),
                                "rate_inc_tax": val,
                                "rate_dollars_per_kwh": dollars,
                                "network_tariff_code": rate.get("networkTariffCode"),
                                "step_start": rate.get("stepStart"),
                                "step_end": rate.get("stepEnd"),
                            }
                        )

        # 2. Account billing balance & dates
        with suppress(OriginEnergyApiError, aiohttp.ClientError, TimeoutError):
            accounts = await api.async_get_accounts()
            if accounts:
                acct = next(
                    (a for a in accounts if a.get("nextBillDate") and a.get("balance")),
                    next((a for a in accounts if a.get("nextBillDate")), accounts[0]),
                )
                summary.account_number = acct.get("accountNumber")
                if acct.get("balance") is not None:
                    summary.balance_dollars = round(float(acct["balance"]) * 0.01, 2)
                summary.next_bill_date = acct.get("nextBillDate")
                summary.next_bill_datetime = parse_api_datetime(summary.next_bill_date)
                if summary.next_bill_datetime is not None:
                    delta_days = (
                        summary.next_bill_datetime.astimezone(self.timezone).date()
                        - now.date()
                    ).days
                    summary.days_until_next_bill = max(delta_days, 0)
                summary.billing_period_start = acct.get("currentBillingPeriodStartDate")
                due_charges = acct.get("dueCharges") or []
                if due_charges and isinstance(due_charges, list):
                    first_due = due_charges[0]
                    if first_due.get("amount") is not None:
                        summary.due_amount_dollars = round(float(first_due["amount"]) * 0.01, 2)
                    summary.due_date = first_due.get("dueDate")

        # 3. Current billing cycle / monthly usage & cost
        with suppress(OriginEnergyApiError, aiohttp.ClientError, TimeoutError):
            billing_usage = await api.async_get_usage(
                self.agreement_id,
                now - timedelta(days=90),
                now,
                time_unit="BILLING",
                service_type=self.service_type,
            )
            if billing_usage.has_feed_in:
                summary.has_solar_export = True
            if billing_usage.points:
                pt = billing_usage.points[-1]
                e_scale = ENERGY_TO_KWH.get(billing_usage.consumption_unit or "WATT_HOUR", 0.001)
                c_scale = COST_TO_DOLLARS.get(billing_usage.cost_unit or "CENT", 0.01)
                if pt.get("totalConsumedEnergy") is not None:
                    summary.current_cycle_usage_kwh = round(
                        float(pt["totalConsumedEnergy"]) * e_scale, 3
                    )
                if pt.get("totalCost") is not None:
                    summary.current_cycle_cost_dollars = round(
                        float(pt["totalCost"]) * c_scale, 2
                    )
                if pt.get("supplyCost") is not None:
                    summary.current_cycle_supply_dollars = round(
                        float(pt["supplyCost"]) * c_scale, 2
                    )
                if pt.get("usageCost") is not None:
                    summary.current_cycle_usage_charge_dollars = round(
                        float(pt["usageCost"]) * c_scale, 2
                    )
                if (
                    not summary.usage_rates
                    and summary.current_cycle_usage_kwh
                    and summary.current_cycle_usage_charge_dollars
                ):
                    eff_rate = round(
                        summary.current_cycle_usage_charge_dollars
                        / summary.current_cycle_usage_kwh,
                        4,
                    )
                    summary.usage_rates.append(
                        {
                            "time_of_day": "ALLDAY",
                            "description": "Effective embedded network usage rate",
                            "rate_dollars_per_kwh": eff_rate,
                        }
                    )
                if (
                    summary.standing_charge_dollars is None
                    and summary.current_cycle_supply_dollars
                ):
                    p_start = parse_api_datetime(pt.get("startDate"))
                    p_end = parse_api_datetime(pt.get("endDate"))
                    if p_start and p_end:
                        days = max(round((p_end - p_start).total_seconds() / 86400), 1)
                        summary.standing_charge_dollars = round(
                            summary.current_cycle_supply_dollars / days, 4
                        )

            monthly_usage = await api.async_get_usage(
                self.agreement_id,
                now - timedelta(days=35),
                now,
                time_unit="MONTHLY",
                service_type=self.service_type,
            )
            if monthly_usage.points:
                mpt = monthly_usage.points[-1]
                e_scale = ENERGY_TO_KWH.get(monthly_usage.consumption_unit or "WATT_HOUR", 0.001)
                if mpt.get("totalConsumedEnergy") is not None:
                    summary.current_cycle_usage_kwh = round(
                        float(mpt["totalConsumedEnergy"]) * e_scale, 3
                    )

        # Apply manual tariff overrides if configured in Options
        if override_usage_rate is not None:
            summary.usage_rates.insert(
                0,
                {
                    "time_of_day": "ALLDAY",
                    "description": "Configured tariff override",
                    "rate_dollars_per_kwh": round(override_usage_rate, 4),
                },
            )
        if override_supply_charge is not None:
            summary.standing_charge_dollars = round(override_supply_charge, 4)
        if override_fit is not None:
            summary.solar_feed_in_rate_dollars = round(override_fit, 4)

        active_rate = (
            summary.usage_rates[0].get("rate_dollars_per_kwh")
            if summary.usage_rates
            else None
        )
        active_supply = summary.standing_charge_dollars or 0.0

        # 4. Yesterday's Usage & Cost + Current Billing Period Daily Rollup & Projection
        with suppress(OriginEnergyApiError, aiohttp.ClientError, TimeoutError):
            period_start_dt = parse_api_datetime(summary.billing_period_start)
            lookback_start = (
                min(period_start_dt, now - timedelta(days=7))
                if period_start_dt
                else now - timedelta(days=35)
            )
            daily_window = await api.async_get_usage(
                self.agreement_id,
                lookback_start,
                now,
                time_unit="DAILY",
                service_type=self.service_type,
            )
            e_scale = ENERGY_TO_KWH.get(daily_window.consumption_unit or "WATT_HOUR", 0.001)
            c_scale = COST_TO_DOLLARS.get(daily_window.cost_unit or "CENT", 0.01)
            if daily_window.has_feed_in:
                summary.has_solar_export = True

            actual_days = [
                p for p in daily_window.points if p.get("totalConsumedEnergy") is not None
            ]
            if actual_days:
                latest_day = actual_days[-1]
                y_kwh = float(latest_day["totalConsumedEnergy"]) * e_scale
                summary.yesterday_usage_kwh = round(y_kwh, 3)
                summary.yesterday_date = latest_day.get("startDate")
                if latest_day.get("totalCost") is not None:
                    summary.yesterday_cost_dollars = round(
                        float(latest_day["totalCost"]) * c_scale, 2
                    )
                elif active_rate is not None:
                    summary.yesterday_cost_dollars = round(
                        (y_kwh * active_rate) + active_supply, 2
                    )
                if latest_day.get("totalFeedInEnergy") is not None:
                    summary.yesterday_export_kwh = round(
                        float(latest_day["totalFeedInEnergy"]) * e_scale, 3
                    )

            # Calculate active billing cycle running totals and projected bill if period_start_dt is known
            if period_start_dt is not None:
                cycle_days = [
                    p
                    for p in actual_days
                    if (dt := parse_api_datetime(p.get("startDate"))) is not None
                    and dt >= period_start_dt
                ]
                if cycle_days:
                    cycle_kwh = sum(
                        float(p["totalConsumedEnergy"]) * e_scale for p in cycle_days
                    )
                    summary.current_cycle_usage_kwh = round(cycle_kwh, 3)
                    has_api_cycle_costs = any(
                        p.get("totalCost") is not None for p in cycle_days
                    )
                    if has_api_cycle_costs:
                        cycle_cost = sum(
                            float(p.get("totalCost") or 0) * c_scale for p in cycle_days
                        )
                        summary.current_cycle_cost_dollars = round(cycle_cost, 2)
                    elif active_rate is not None:
                        usage_charge = cycle_kwh * active_rate
                        supply_charge = len(cycle_days) * active_supply
                        summary.current_cycle_usage_charge_dollars = round(usage_charge, 2)
                        summary.current_cycle_supply_dollars = round(supply_charge, 2)
                        summary.current_cycle_cost_dollars = round(
                            usage_charge + supply_charge, 2
                        )

                    # Project full billing cycle cost
                    total_cycle_days = 30
                    if summary.next_bill_datetime is not None:
                        span = (
                            summary.next_bill_datetime.astimezone(self.timezone).date()
                            - period_start_dt.astimezone(self.timezone).date()
                        ).days
                        if span > 0:
                            total_cycle_days = span

                    avg_daily_kwh = cycle_kwh / len(cycle_days)
                    proj_kwh = avg_daily_kwh * total_cycle_days
                    summary.projected_bill_usage_kwh = round(proj_kwh, 2)
                    if summary.current_cycle_cost_dollars is not None:
                        if active_rate is not None:
                            summary.projected_bill_cost_dollars = round(
                                (proj_kwh * active_rate)
                                + (total_cycle_days * active_supply),
                                2,
                            )
                        else:
                            avg_daily_cost = summary.current_cycle_cost_dollars / len(
                                cycle_days
                            )
                            summary.projected_bill_cost_dollars = round(
                                avg_daily_cost * total_cycle_days, 2
                            )

        # 5. Embedded Hot Water (Water usage) service if present on the account
        with suppress(OriginEnergyApiError, aiohttp.ClientError, TimeoutError):
            hw_services = await api.async_get_services("EMBEDDED_HOT_WATER")
            hw = pick_service(hw_services)
            if hw:
                summary.hot_water_agreement_id = hw.get("agreementId")
                mp = hw.get("meterPoint") or {}
                summary.hot_water_identifier = mp.get("identifier")
                hw_product = hw.get("product") or {}
                for rate in hw_product.get("rates") or []:
                    summary.hot_water_rates.append(
                        {
                            "band": rate.get("bandCategory"),
                            "description": rate.get("description"),
                            "rate_inc_tax": rate.get("rateIncludingTax"),
                            "step_start": rate.get("stepStart"),
                            "step_end": rate.get("stepEnd"),
                        }
                    )
                for meter in mp.get("meters") or []:
                    edges = (meter.get("readings") or {}).get("edges") or []
                    if edges:
                        latest = edges[0].get("node") or {}
                        if latest.get("value") is not None:
                            summary.hot_water_latest_reading = float(latest["value"])
                            summary.hot_water_latest_read_at = latest.get("readAt")
                            break
                if summary.hot_water_agreement_id:
                    hw_billing = await api.async_get_usage(
                        summary.hot_water_agreement_id,
                        now - timedelta(days=120),
                        now,
                        time_unit="BILLING",
                        service_type="EMBEDDED_HOT_WATER",
                    )
                    if hw_billing.points:
                        hw_pt = hw_billing.points[-1]
                        summary.hot_water_usage_unit = hw_billing.consumption_unit
                        if hw_pt.get("totalConsumedEnergy") is not None:
                            summary.hot_water_current_cycle_usage = float(
                                hw_pt["totalConsumedEnergy"]
                            )
                        if hw_pt.get("totalCost") is not None:
                            c_scale = COST_TO_DOLLARS.get(hw_billing.cost_unit or "CENT", 0.01)
                            summary.hot_water_current_cycle_cost_dollars = round(
                                float(hw_pt["totalCost"]) * c_scale, 2
                            )

        # 6. Gas service if present on the account
        with suppress(OriginEnergyApiError, aiohttp.ClientError, TimeoutError):
            gas_services = await api.async_get_services("GAS")
            gas = pick_service(gas_services)
            if gas:
                summary.gas_agreement_id = gas.get("agreementId")
                mp = gas.get("meterPoint") or {}
                summary.gas_identifier = mp.get("identifier")
                if summary.gas_agreement_id:
                    gas_billing = await api.async_get_usage(
                        summary.gas_agreement_id,
                        now - timedelta(days=120),
                        now,
                        time_unit="BILLING",
                        service_type="GAS",
                    )
                    if gas_billing.points:
                        g_pt = gas_billing.points[-1]
                        mj_scale = ENERGY_TO_MJ.get(
                            gas_billing.consumption_unit or "MEGAJOULE", 1.0
                        )
                        c_scale = COST_TO_DOLLARS.get(
                            gas_billing.cost_unit or "CENT", 0.01
                        )
                        if g_pt.get("totalConsumedEnergy") is not None:
                            summary.gas_current_cycle_usage_mj = round(
                                float(g_pt["totalConsumedEnergy"]) * mj_scale, 2
                            )
                        if g_pt.get("totalCost") is not None:
                            summary.gas_current_cycle_cost_dollars = round(
                                float(g_pt["totalCost"]) * c_scale, 2
                            )

        return summary

    # ------------------------------------------------------------------
    # History backfill
    # ------------------------------------------------------------------

    def _start_backfill(self, force_restart: bool = False) -> None:
        if self._backfill_task is not None and not self._backfill_task.done():
            if not force_restart:
                return
            self._backfill_task.cancel()
        _LOGGER.debug("Starting Origin Energy history backfill in the background")
        self._backfill_task = self.config_entry.async_create_background_task(
            self.hass, self._async_backfill(), name=f"{DOMAIN} history backfill"
        )

    async def _async_backfill(self) -> None:
        try:
            async with self._import_lock:
                start = await self._async_history_start()
                _LOGGER.debug("Backfilling Origin Energy usage from %s", start)
                cursor = await self._async_import(
                    start, dt_util.now(self.timezone), _ImportCursor()
                )
                api = await self._async_api()
                await self._async_import_secondary_services(api)
        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed as err:
            _LOGGER.warning("Origin Energy backfill needs re-authentication: %s", err)
            self.config_entry.async_start_reauth(self.hass)
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Origin Energy history backfill failed, will retry: %s", err
            )
            return

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_BACKFILL_COMPLETE: True},
        )
        _LOGGER.info(
            "Origin Energy history backfill complete, %d points imported",
            cursor.written,
        )

    async def _async_history_start(self) -> datetime:
        cap = dt_util.now(self.timezone) - timedelta(days=FIRST_RUN_HISTORY_DAYS)
        try:
            api = await self._async_api()
            services = await api.async_get_services(self.service_type)
        except OriginEnergyApiError as err:
            _LOGGER.debug("Could not read service validity (%s), using cap", err)
            return cap

        service = pick_service(services, self.agreement_id)
        valid_from = parse_api_datetime((service or {}).get("validFrom"))
        if valid_from is None:
            return cap
        return max(valid_from, cap)

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    async def _async_import_recent(self) -> None:
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, self.consumption_statistic_id, True, set()
        )
        if not last_stat:
            _LOGGER.debug("No Origin Energy statistics yet, re-running backfill")
            self._start_backfill()
            return

        last_point = dt_util.utc_from_timestamp(
            last_stat[self.consumption_statistic_id][0]["start"]
        )

        now = dt_util.now(self.timezone)
        api = await self._async_api()
        probe = await api.async_get_usage(
            self.agreement_id,
            now - timedelta(days=2),
            now,
            self.time_unit,
            self.service_type,
        )
        last_reading = probe.last_reading_date

        anchor = min(last_point, last_reading) if last_reading else last_point
        start = anchor - timedelta(days=OVERLAP_DAYS)
        end = max(last_reading or now, last_point) + timedelta(minutes=1)

        cursor = await self._async_baseline_cursor(start)
        if cursor is None:
            consumption_sum, cost_sum, export_sum, earnings_sum = (
                await self._async_sums_at(last_point)
            )
            cursor = _ImportCursor(
                consumption_sum=consumption_sum,
                cost_sum=cost_sum,
                export_sum=export_sum,
                earnings_sum=earnings_sum,
                last_start=last_point,
            )
            start = last_point

        await self._async_import(start, end, cursor)

    async def _async_baseline_cursor(self, start: datetime) -> _ImportCursor | None:
        stat_ids = {
            self.consumption_statistic_id,
            self.cost_statistic_id,
            self.export_statistic_id,
            self.earnings_statistic_id,
        }
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(days=BASELINE_LOOKBACK_DAYS),
            start,
            stat_ids,
            "hour",
            None,
            {"sum"},
        )
        consumption_rows = rows.get(self.consumption_statistic_id) or []
        if not consumption_rows:
            return None

        cost_rows = rows.get(self.cost_statistic_id) or []
        export_rows = rows.get(self.export_statistic_id) or []
        earnings_rows = rows.get(self.earnings_statistic_id) or []
        return _ImportCursor(
            consumption_sum=float(consumption_rows[-1]["sum"]),
            cost_sum=float(cost_rows[-1]["sum"]) if cost_rows else 0.0,
            export_sum=float(export_rows[-1]["sum"]) if export_rows else 0.0,
            earnings_sum=float(earnings_rows[-1]["sum"]) if earnings_rows else 0.0,
            last_start=None,
        )

    async def _async_sums_at(
        self, instant: datetime
    ) -> tuple[float, float, float, float]:
        stat_ids = {
            self.consumption_statistic_id,
            self.cost_statistic_id,
            self.export_statistic_id,
            self.earnings_statistic_id,
        }
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            instant,
            instant + timedelta(seconds=1),
            stat_ids,
            "hour",
            None,
            {"sum"},
        )

        def _sum(statistic_id: str) -> float:
            entries = rows.get(statistic_id) or []
            return float(entries[0]["sum"]) if entries else 0.0

        return (
            _sum(self.consumption_statistic_id),
            _sum(self.cost_statistic_id),
            _sum(self.export_statistic_id),
            _sum(self.earnings_statistic_id),
        )

    # ------------------------------------------------------------------
    # Secondary Services (Hot Water & Gas External Statistics)
    # ------------------------------------------------------------------

    async def _async_import_secondary_services(
        self, api: OriginEnergyApiClient
    ) -> None:
        """Import Hot Water (L) and Gas (kWh) external statistics for the HA Energy Dashboard."""
        # 1. Hot Water external statistics from meter readings
        if self.summary.hot_water_agreement_id:
            with suppress(OriginEnergyApiError, aiohttp.ClientError, TimeoutError):
                await self._async_import_hot_water_statistics(
                    api, self.summary.hot_water_agreement_id
                )

        # 2. Gas external statistics if a GAS agreement exists
        if self.summary.gas_agreement_id:
            with suppress(OriginEnergyApiError, aiohttp.ClientError, TimeoutError):
                await self._async_import_gas_statistics(
                    api, self.summary.gas_agreement_id
                )

    async def _async_import_hot_water_statistics(
        self, api: OriginEnergyApiClient, hw_agreement_id: str
    ) -> None:
        """Build external water statistics from cumulative meter readings & BILLING usage."""
        hw_services = await api.async_get_services("EMBEDDED_HOT_WATER")
        hw = pick_service(hw_services, hw_agreement_id)
        if not hw:
            return

        mp = hw.get("meterPoint") or {}
        readings: list[tuple[datetime, float]] = []
        for meter in mp.get("meters") or []:
            edges = (meter.get("readings") or {}).get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                dt = parse_api_datetime(node.get("readAt"))
                val = node.get("value")
                if dt is not None and val is not None:
                    try:
                        # Snap timestamp to the top of the clock hour for HA statistics
                        snapped = dt.astimezone(self.timezone).replace(
                            minute=0, second=0, microsecond=0
                        )
                        readings.append((snapped, float(val)))
                    except (TypeError, ValueError):
                        continue

        if not readings:
            return

        readings.sort(key=lambda r: r[0])
        # Deduplicate identical hour timestamps
        deduped: list[tuple[datetime, float]] = []
        for dt, val in readings:
            if deduped and deduped[-1][0] == dt:
                deduped[-1] = (dt, val)
            else:
                deduped.append((dt, val))

        hw_slug = statistic_slug(hw_agreement_id)
        water_stat_id = f"{DOMAIN}:{hw_slug}_water_consumption"
        water_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy hot water consumption",
            source=DOMAIN,
            statistic_id=water_stat_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.LITERS,
        )

        baseline = deduped[0][1]
        prev_val = baseline
        stat_rows: list[StatisticData] = []
        for dt, val in deduped:
            delta = max(val - prev_val, 0.0)
            running_sum = max(val - baseline, 0.0)
            stat_rows.append(StatisticData(start=dt, state=delta, sum=running_sum))
            prev_val = val

        if stat_rows:
            async_add_external_statistics(self.hass, water_meta, stat_rows)

    async def _async_import_gas_statistics(
        self, api: OriginEnergyApiClient, gas_agreement_id: str
    ) -> None:
        """Import daily/hourly gas usage & cost into HA external statistics."""
        gas_services = await api.async_get_services("GAS")
        gas = pick_service(gas_services, gas_agreement_id)
        if not gas:
            return

        granularity = pick_time_unit(gas.get("availableUsageTimeUnits"))
        now = dt_util.now(self.timezone)
        valid_from = parse_api_datetime(gas.get("validFrom"))
        start = max(
            valid_from or (now - timedelta(days=90)),
            now - timedelta(days=90),
        )
        result = await api.async_get_usage(
            gas_agreement_id, start, now, granularity, "GAS"
        )
        if not result.points:
            return

        energy_scale = ENERGY_TO_KWH.get(
            result.consumption_unit or "MEGAJOULE", 1.0 / 3.6
        )
        cost_scale = COST_TO_DOLLARS.get(result.cost_unit or "CENT", 0.01)

        gas_slug = statistic_slug(gas_agreement_id)
        gas_consumption_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy gas consumption",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:{gas_slug}_gas_consumption",
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        gas_cost_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy gas cost",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:{gas_slug}_gas_cost",
            unit_class=None,
            unit_of_measurement=None,
        )

        usable: list[tuple[datetime, dict[str, Any]]] = []
        for pt in result.points:
            if pt.get("totalConsumedEnergy") is None:
                continue
            dt = dt_util.parse_datetime(pt["startDate"])
            if dt is not None:
                usable.append((dt, pt))
        usable.sort(key=lambda item: item[0])

        c_sum = 0.0
        d_sum = 0.0
        c_stats: list[StatisticData] = []
        d_stats: list[StatisticData] = []
        for dt, pt in usable:
            kwh = float(pt["totalConsumedEnergy"]) * energy_scale
            dollars = float(pt.get("totalCost") or 0) * cost_scale
            c_sum += kwh
            d_sum += dollars
            c_stats.append(StatisticData(start=dt, state=kwh, sum=c_sum))
            d_stats.append(StatisticData(start=dt, state=dollars, sum=d_sum))

        if c_stats:
            async_add_external_statistics(self.hass, gas_consumption_meta, c_stats)
            async_add_external_statistics(self.hass, gas_cost_meta, d_stats)

    # ------------------------------------------------------------------
    # Shared Electricity & Solar Export import machinery
    # ------------------------------------------------------------------

    def _statistic_metadata(
        self,
    ) -> tuple[
        StatisticMetaData,
        StatisticMetaData,
        StatisticMetaData,
        StatisticMetaData,
    ]:
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy consumption",
            source=DOMAIN,
            statistic_id=self.consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy cost",
            source=DOMAIN,
            statistic_id=self.cost_statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )
        export_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy solar export",
            source=DOMAIN,
            statistic_id=self.export_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        earnings_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy solar earnings",
            source=DOMAIN,
            statistic_id=self.earnings_statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )
        return (
            consumption_metadata,
            cost_metadata,
            export_metadata,
            earnings_metadata,
        )

    async def _async_import(
        self,
        start: datetime,
        end: datetime,
        cursor: _ImportCursor,
    ) -> _ImportCursor:
        (
            consumption_metadata,
            cost_metadata,
            export_metadata,
            earnings_metadata,
        ) = self._statistic_metadata()
        window = timedelta(days=chunk_days(self.time_unit))
        window_start = start

        while window_start < end:
            window_end = min(window_start + window, end)
            api = await self._async_api()
            result = await api.async_get_usage(
                self.agreement_id,
                window_start,
                window_end,
                self.time_unit,
                self.service_type,
            )

            (
                consumption_stats,
                cost_stats,
                export_stats,
                earnings_stats,
            ) = self._build_statistics(result, cursor)
            if consumption_stats:
                async_add_external_statistics(
                    self.hass, consumption_metadata, consumption_stats
                )
                async_add_external_statistics(self.hass, cost_metadata, cost_stats)
                cursor.written += len(consumption_stats)
            if export_stats:
                async_add_external_statistics(
                    self.hass, export_metadata, export_stats
                )
                async_add_external_statistics(
                    self.hass, earnings_metadata, earnings_stats
                )

            window_start = window_end

        return cursor

    def _build_statistics(
        self, result: UsageWindow, cursor: _ImportCursor
    ) -> tuple[
        list[StatisticData],
        list[StatisticData],
        list[StatisticData],
        list[StatisticData],
    ]:
        energy_scale = _scale(
            ENERGY_TO_KWH, result.consumption_unit, "WATT_HOUR", "energy"
        )
        cost_scale = _scale(COST_TO_DOLLARS, result.cost_unit, "CENT", "cost")

        consumption_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []
        export_stats: list[StatisticData] = []
        earnings_stats: list[StatisticData] = []

        usable: list[tuple[datetime, dict[str, Any]]] = []
        for point in result.points:
            if (
                point.get("totalConsumedEnergy") is None
                and point.get("totalFeedInEnergy") is None
            ):
                continue

            start = dt_util.parse_datetime(point["startDate"])
            if start is None:
                continue
            usable.append((start, point))

        usable.sort(key=lambda item: item[0])

        for start, point in usable:
            if cursor.last_start is not None and start <= cursor.last_start:
                continue

            consumption_kwh = (point.get("totalConsumedEnergy") or 0) * energy_scale
            if point.get("totalCost") is not None:
                cost_dollars = point["totalCost"] * cost_scale
            elif self.summary.usage_rates:
                rate_per_kwh = (
                    self.summary.usage_rates[0].get("rate_dollars_per_kwh") or 0.0
                )
                supply_per_hour = (self.summary.standing_charge_dollars or 0.0) / 24.0
                cost_dollars = (consumption_kwh * rate_per_kwh) + (
                    supply_per_hour
                    if self.time_unit == "HOURLY"
                    else (self.summary.standing_charge_dollars or 0.0)
                )
            else:
                cost_dollars = 0.0

            cursor.consumption_sum += consumption_kwh
            cursor.cost_sum += cost_dollars
            cursor.last_start = start

            consumption_stats.append(
                StatisticData(
                    start=start, state=consumption_kwh, sum=cursor.consumption_sum
                )
            )
            cost_stats.append(
                StatisticData(start=start, state=cost_dollars, sum=cursor.cost_sum)
            )

            # Solar feed-in export & earnings if present on the account
            if result.has_feed_in or point.get("totalFeedInEnergy") is not None:
                export_kwh = abs(float(point.get("totalFeedInEnergy") or 0)) * energy_scale
                if point.get("totalEarnings") is not None:
                    earnings_dollars = abs(float(point["totalEarnings"])) * cost_scale
                elif self.summary.solar_feed_in_rate_dollars is not None:
                    earnings_dollars = export_kwh * self.summary.solar_feed_in_rate_dollars
                else:
                    earnings_dollars = 0.0

                cursor.export_sum += export_kwh
                cursor.earnings_sum += earnings_dollars
                export_stats.append(
                    StatisticData(
                        start=start, state=export_kwh, sum=cursor.export_sum
                    )
                )
                earnings_stats.append(
                    StatisticData(
                        start=start, state=earnings_dollars, sum=cursor.earnings_sum
                    )
                )

        return consumption_stats, cost_stats, export_stats, earnings_stats


def _scale(
    table: dict[str, float], unit: str | None, default: str, label: str
) -> float:
    resolved = unit or default
    if resolved not in table:
        raise UpdateFailed(f"Unexpected {label} unit from Origin Energy: {unit}")
    return table[resolved]
