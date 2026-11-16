"""The Origin Energy integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import CONF_AGREEMENT_ID, CONF_SESSION_COOKIE, DOMAIN
from .coordinator import OriginEnergyConfigEntry, OriginEnergyCoordinator

PLATFORMS = ["sensor"]
SERVICE_SET_SESSION_COOKIE = "set_session_cookie"

SERVICE_SET_COOKIE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SESSION_COOKIE): cv.string,
        vol.Optional(CONF_AGREEMENT_ID): cv.string,
    }
)


async def _async_entry_updated(
    hass: HomeAssistant, entry: OriginEnergyConfigEntry
) -> None:
    """Apply a poll interval changed from the Configure dialog without reloading."""
    coordinator = entry.runtime_data
    if coordinator.async_adopt_update_interval():
        await coordinator.async_request_refresh()


def _register_services(hass: HomeAssistant) -> None:
    """Register the `origin_energy.set_session_cookie` service once."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SESSION_COOKIE):
        return

    async def _async_handle_set_cookie(call: ServiceCall) -> None:
        cookie = call.data[CONF_SESSION_COOKIE].strip()
        target_agreement = call.data.get(CONF_AGREEMENT_ID)

        entries = hass.config_entries.async_entries(DOMAIN)
        if target_agreement:
            entries = [
                e for e in entries if e.data.get(CONF_AGREEMENT_ID) == target_agreement
            ]

        for entry in entries:
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_SESSION_COOKIE: cookie},
            )
            await hass.config_entries.async_reload(entry.entry_id)

            # Dismiss any active reauth flows for this entry
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
                if (
                    flow.get("context", {}).get("source") == SOURCE_REAUTH
                    and flow.get("context", {}).get("entry_id") == entry.entry_id
                ):
                    hass.config_entries.flow.async_abort(flow["flow_id"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SESSION_COOKIE,
        _async_handle_set_cookie,
        schema=SERVICE_SET_COOKIE_SCHEMA,
    )


async def async_setup_entry(hass: HomeAssistant, entry: OriginEnergyConfigEntry) -> bool:
    """Set up Origin Energy from a config entry."""
    _register_services(hass)

    coordinator = OriginEnergyCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OriginEnergyConfigEntry) -> bool:
    """Unload an Origin Energy config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
