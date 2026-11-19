"""Button entities for Origin Energy (Refresh now & Re-run history backfill)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OriginEnergyConfigEntry, OriginEnergyCoordinator


@dataclass(frozen=True, kw_only=True)
class OriginEnergyButtonDescription(ButtonEntityDescription):
    """Describes an Origin Energy button entity."""

    press_fn: Callable[[OriginEnergyCoordinator], Awaitable[None]]


async def _async_refresh_now(coordinator: OriginEnergyCoordinator) -> None:
    await coordinator.async_request_refresh()


async def _async_rebackfill(coordinator: OriginEnergyCoordinator) -> None:
    await coordinator.async_trigger_backfill()
    await coordinator.async_request_refresh()


BUTTONS: tuple[OriginEnergyButtonDescription, ...] = (
    OriginEnergyButtonDescription(
        key="refresh_usage",
        translation_key="refresh_usage",
        name="Refresh usage now",
        icon="mdi:refresh",
        entity_category=EntityCategory.CONFIG,
        press_fn=_async_refresh_now,
    ),
    OriginEnergyButtonDescription(
        key="backfill_history",
        translation_key="backfill_history",
        name="Re-run history backfill",
        icon="mdi:database-sync",
        entity_category=EntityCategory.CONFIG,
        press_fn=_async_rebackfill,
    ),
)


async def async_setup_entry(
    hass,
    entry: OriginEnergyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Origin Energy button entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        [OriginEnergyButton(coordinator, description) for description in BUTTONS]
    )


class OriginEnergyButton(
    CoordinatorEntity[OriginEnergyCoordinator], ButtonEntity
):
    """Representation of an Origin Energy action button."""

    _attr_has_entity_name = True
    entity_description: OriginEnergyButtonDescription

    def __init__(
        self,
        coordinator: OriginEnergyCoordinator,
        description: OriginEnergyButtonDescription,
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

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.entity_description.press_fn(self.coordinator)

