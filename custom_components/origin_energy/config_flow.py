"""Config flow for Origin Energy."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    pick_service,
    pick_time_unit,
)
from .auth import (
    OriginEnergyAuthError,
    OriginEnergyBlocked,
    OriginEnergyMfaRequired,
    async_login_with_cookie,
    current_session_cookie,
)
from .const import (
    AUTH_DOMAIN,
    CONF_AGREEMENT_ID,
    CONF_SERVICE_TYPE,
    CONF_SESSION_COOKIE,
    CONF_TIME_UNIT,
    CONF_TIMEZONE,
    CONF_UPDATE_INTERVAL_HOURS,
    DEFAULT_TIMEZONE,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_UPDATE_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_PLACEHOLDERS = {
    "jwks_url": f"https://{AUTH_DOMAIN}/.well-known/jwks.json",
    "cookie_origin": f"https://{AUTH_DOMAIN}",
}

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_SESSION_COOKIE): str})


class OriginEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Origin Energy."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OriginEnergyOptionsFlow:
        """Expose the Configure dialog on the integration entry."""
        return OriginEnergyOptionsFlow()

    def _configured_agreement_id(self) -> str | None:
        if self.source != SOURCE_REAUTH:
            return None
        return self._get_reauth_entry().data.get(CONF_AGREEMENT_ID)

    async def _async_validate(
        self, cookie: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Exchange the cookie for tokens and read back the live service."""
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            tokens = await async_login_with_cookie(session, cookie)
            client = OriginEnergyApiClient(session, tokens.access_token)

            service = None
            service_type = "ELECTRICITY"
            for candidate_type in ("ELECTRICITY", "EMBEDDED_ELECTRICITY"):
                services = await client.async_get_services(candidate_type)
                service = pick_service(services, self._configured_agreement_id())
                if service is not None:
                    service_type = candidate_type
                    break

            if service is None:
                return None, "no_services"
            timezone = (
                ((service.get("property") or {}).get("address") or {}).get(
                    "components", {}
                ).get("timezone")
                or DEFAULT_TIMEZONE
            )
            # Even when EmbeddedElectricityService only advertises ["BILLING"] in
            # availableUsageTimeUnits, probe if HOURLY is actually supported.
            time_unit = pick_time_unit(service.get("availableUsageTimeUnits"))
            if service_type == "EMBEDDED_ELECTRICITY" and time_unit == "DAILY":
                time_unit = "HOURLY"
            _LOGGER.debug(
                "Origin Energy service=%s granularity=%s", service_type, time_unit
            )

            return {
                CONF_SESSION_COOKIE: current_session_cookie(session) or cookie,
                CONF_AGREEMENT_ID: service["agreementId"],
                CONF_SERVICE_TYPE: service_type,
                CONF_TIMEZONE: timezone,
                CONF_TIME_UNIT: time_unit,
                "nmi": (service.get("meterPoint") or {}).get("identifier"),
            }, None
        except OriginEnergyBlocked:
            return None, "blocked"
        except OriginEnergyMfaRequired:
            return None, "mfa_required"
        except OriginEnergyAuthError:
            return None, "invalid_auth"
        except OriginEnergyApiError:
            return None, "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error setting up Origin Energy")
            return None, "unknown"
        finally:
            session.detach()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            data, error = await self._async_validate(
                user_input[CONF_SESSION_COOKIE].strip()
            )
            if error:
                errors["base"] = error
            else:
                assert data is not None
                await self.async_set_unique_id(data[CONF_AGREEMENT_ID])
                self._abort_if_unique_id_configured()
                nmi = data.pop("nmi", None)
                return self.async_create_entry(
                    title=f"Origin Energy ({nmi})" if nmi else "Origin Energy",
                    data=data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders=STEP_USER_PLACEHOLDERS,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when the stored session cookie stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            data, error = await self._async_validate(
                user_input[CONF_SESSION_COOKIE].strip()
            )
            if error:
                errors["base"] = error
            else:
                assert data is not None
                data.pop("nmi", None)
                await self.async_set_unique_id(data[CONF_AGREEMENT_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class OriginEnergyOptionsFlow(OptionsFlow):
    """Let the poll interval be changed from the integration Configure dialog."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_UPDATE_INTERVAL_HOURS: int(
                        user_input[CONF_UPDATE_INTERVAL_HOURS]
                    )
                }
            )

        current = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL_HOURS, DEFAULT_UPDATE_INTERVAL_HOURS
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_INTERVAL_HOURS, default=current
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_UPDATE_INTERVAL_HOURS,
                            max=MAX_UPDATE_INTERVAL_HOURS,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="hours",
                        )
                    )
                }
            ),
        )

