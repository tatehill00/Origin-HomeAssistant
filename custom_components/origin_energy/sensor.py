"""Sensors for Origin Energy (tariffs, billing cycle totals, water usage, and diagnostics)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_DOLLAR, UnitOfEnergy, UnitOfVolume
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AccountSummary
from .const import DOMAIN
from .coordinator import OriginEnergyConfigEntry, OriginEnergyCoordinator, OriginEnergyData


@dataclass(frozen=True, kw_only=True)
class OriginEnergySensorDescription(SensorEntityDescription):
    """Describes an Origin Energy sensor entity."""

    value_fn: Callable[[OriginEnergyData], Any]
    attributes_fn: Callable[[AccountSummary], dict[str, Any]] | None = None
    exists_fn: Callable[[AccountSummary], bool] = lambda _: True


def _primary_usage_rate(summary: AccountSummary) -> float | None:
    if not summary.usage_rates:
        return None
    return summary.usage_rates[0].get("rate_dollars_per_kwh")


def _tariff_attributes(summary: AccountSummary) -> dict[str, Any]:
    return {
        "plan_name": summary.plan_name,
        "standing_charge_dollars_per_day": summary.standing_charge_dollars,
        "rates": summary.usage_rates,
    }


def _billing_attributes(summary: AccountSummary) -> dict[str, Any]:
    return {
        "account_number": summary.account_number,
        "billing_period_start": summary.billing_period_start,
        "next_bill_date": summary.next_bill_date,
        "due_amount_dollars": summary.due_amount_dollars,
        "due_date": summary.due_date,
        "current_cycle_supply_dollars": summary.current_cycle_supply_dollars,
        "current_cycle_usage_charge_dollars": summary.current_cycle_usage_charge_dollars,
    }


def _hot_water_attributes(summary: AccountSummary) -> dict[str, Any]:
    return {
        "agreement_id": summary.hot_water_agreement_id,
        "meter_identifier": summary.hot_water_identifier,
        "latest_read_at": summary.hot_water_latest_read_at,
        "current_cycle_usage": summary.hot_water_current_cycle_usage,
        "current_cycle_cost_dollars": summary.hot_water_current_cycle_cost_dollars,
        "usage_unit": summary.hot_water_usage_unit,
        "rates": summary.hot_water_rates,
    }


SENSORS: tuple[OriginEnergySensorDescription, ...] = (
    OriginEnergySensorDescription(
        key="last_update",
        translation_key="last_update",
        name="Last update",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.last_updated,
    ),
    OriginEnergySensorDescription(
        key="current_cycle_usage",
        translation_key="current_cycle_usage",
        name="Billing cycle electricity usage",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda data: data.summary.current_cycle_usage_kwh,
        attributes_fn=_billing_attributes,
    ),
    OriginEnergySensorDescription(
        key="current_cycle_cost",
        translation_key="current_cycle_cost",
        name="Billing cycle electricity cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_DOLLAR,
        suggested_display_precision=2,
        value_fn=lambda data: data.summary.current_cycle_cost_dollars,
        attributes_fn=_billing_attributes,
    ),
    OriginEnergySensorDescription(
        key="account_balance",
        translation_key="account_balance",
        name="Account balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_DOLLAR,
        suggested_display_precision=2,
        value_fn=lambda data: data.summary.balance_dollars,
        attributes_fn=_billing_attributes,
    ),
    OriginEnergySensorDescription(
        key="electricity_usage_rate",
        translation_key="electricity_usage_rate",
        name="Electricity tariff rate",
        native_unit_of_measurement=f"{CURRENCY_DOLLAR}/{UnitOfEnergy.KILO_WATT_HOUR}",
        suggested_display_precision=4,
        value_fn=lambda data: _primary_usage_rate(data.summary),
        attributes_fn=_tariff_attributes,
    ),
    OriginEnergySensorDescription(
        key="daily_supply_charge",
        translation_key="daily_supply_charge",
        name="Daily supply charge",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_DOLLAR,
        suggested_display_precision=4,
        value_fn=lambda data: data.summary.standing_charge_dollars,
        attributes_fn=_tariff_attributes,
    ),
    OriginEnergySensorDescription(
        key="hot_water_usage",
        translation_key="hot_water_usage",
        name="Hot water meter reading",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        value_fn=lambda data: data.summary.hot_water_latest_reading,
        attributes_fn=_hot_water_attributes,
        exists_fn=lambda summary: summary.hot_water_agreement_id is not None,
    ),
)


async def async_setup_entry(
    hass,
    entry: OriginEnergyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Origin Energy sensor entities."""
    coordinator = entry.runtime_data
    summary = coordinator.data.summary if coordinator.data else AccountSummary()

    entities = [
        OriginEnergySensor(coordinator, description)
        for description in SENSORS
        if description.exists_fn(summary)
    ]
    async_add_entities(entities)


class OriginEnergySensor(
    CoordinatorEntity[OriginEnergyCoordinator], SensorEntity
):
    """Representation of an Origin Energy sensor."""

    _attr_has_entity_name = True
    entity_description: OriginEnergySensorDescription

    def __init__(
        self,
        coordinator: OriginEnergyCoordinator,
        description: OriginEnergySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.agreement_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.agreement_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Origin Energy",
            name="Origin Energy",
        )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (
            self.coordinator.data is None
            or self.entity_description.attributes_fn is None
        ):
            return None
        return self.entity_description.attributes_fn(self.coordinator.data.summary)

