"""Diagnostics support for the ezloPi integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

TO_REDACT = {
    CONF_USERNAME, CONF_PASSWORD, "token", "local_key", "serial",
    "auth", "sig", "MMSAuth", "MMSAuthSig", "legacy_token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for an ezloPi config entry."""
    runtime = getattr(entry, "runtime_data", None)
    coordinators = runtime.coordinators if runtime is not None else []

    hubs = [
        {
            "serial": coordinator.serial,
            "connected": coordinator.connection.connected,
            "item_count": len(coordinator.data),
            "last_update_success": coordinator.last_update_success,
        }
        for coordinator in coordinators
    ]

    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "hubs": async_redact_data(hubs, TO_REDACT),
    }
