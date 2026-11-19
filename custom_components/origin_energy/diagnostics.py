"""Diagnostics support for Origin Energy."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_AGREEMENT_ID, CONF_SESSION_COOKIE
from .coordinator import OriginEnergyConfigEntry

TO_REDACT = {
    CONF_SESSION_COOKIE,
    CONF_AGREEMENT_ID,
    "account_number",
    "agreement_id",
    "hot_water_agreement_id",
    "hot_water_identifier",
    "gas_agreement_id",
    "gas_identifier",
    "meter_identifier",
    "nmi",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OriginEnergyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry with personal identifiers and cookies redacted."""
    coordinator = entry.runtime_data
    summary_dict: dict[str, Any] = {}
    if coordinator.data and coordinator.data.summary:
        summary_dict = asdict(coordinator.data.summary)
        if summary_dict.get("next_bill_datetime") is not None:
            summary_dict["next_bill_datetime"] = str(summary_dict["next_bill_datetime"])

    payload = {
        "entry_data": dict(entry.data),
        "entry_options": dict(entry.options),
        "service_type": coordinator.service_type,
        "time_unit": coordinator.time_unit,
        "timezone": str(coordinator.timezone),
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
        "last_updated": (
            str(coordinator.data.last_updated) if coordinator.data else None
        ),
        "statistic_ids": {
            "consumption": coordinator.consumption_statistic_id,
            "cost": coordinator.cost_statistic_id,
            "export": coordinator.export_statistic_id,
            "earnings": coordinator.earnings_statistic_id,
        },
        "summary": summary_dict,
    }
    return async_redact_data(payload, TO_REDACT)

