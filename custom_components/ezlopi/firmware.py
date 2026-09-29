"""Firmware release tracking for ezloPi controllers.

Polls the public ezloPi firmware manifest (the same source the hubs use for
OTA) and exposes the latest available version. Kept separate from the per-hub
websocket coordinators because it is account-wide, not hub-specific.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, EZLOPI_FIRMWARE_MANIFEST_URL

_LOGGER = logging.getLogger(__name__)

_REFRESH_INTERVAL = timedelta(hours=12)
_FETCH_TIMEOUT = 15


class EzloFirmwareCoordinator(DataUpdateCoordinator[str | None]):
    """Fetches the ezloPi firmware manifest and holds the latest version string.

    The value is the manifest's top-level ``version`` (e.g. ``"5.7.15"``). Fetch
    failures are non-fatal: the last known value is retained (``None`` until the
    first successful fetch), so a transient dl.mios.com outage never breaks the
    integration or the update entities.
    """

    def __init__(self, hass: HomeAssistant, session: aiohttp.ClientSession) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} firmware",
            update_interval=_REFRESH_INTERVAL,
        )
        self._session = session

    async def _async_update_data(self) -> str | None:
        try:
            async with self._session.get(
                EZLOPI_FIRMWARE_MANIFEST_URL,
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT),
            ) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "firmware manifest HTTP %s; keeping last known version",
                        response.status,
                    )
                    return self.data
                # dl.mios.com serves application/json, but tolerate mislabeling.
                payload = await response.json(content_type=None)
        except Exception as err:  # noqa: BLE001 - optional data, never fatal
            _LOGGER.debug("firmware manifest fetch failed: %s", err)
            return self.data

        version = payload.get("version") if isinstance(payload, dict) else None
        if not version:
            _LOGGER.debug("firmware manifest had no version field")
            return self.data
        return str(version)
