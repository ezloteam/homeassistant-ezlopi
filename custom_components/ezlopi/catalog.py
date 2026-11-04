"""Server-side firmware decision client (cloud/firmware-catalog).

This is the default OTA path, replacing the client-side manifest + variant
picker (``firmware.select_variant``): the hub reports ``model``/``hardware``/
``firmware`` over the local websocket, and the catalog's ``GET /decision``
returns the single image to flash — or says there is nothing to offer. The
server does the version compare and never offers a downgrade, and an unknown
product is never offered an image (there is no generic fallback any more).

The legacy manifest path is kept behind the ``firmware_catalog`` feature flag
(see :mod:`.flags`); this coordinator only fetches while that flag is ON.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .connection import EzloHubConnection
from .const import DOMAIN, EZLOPI_FIRMWARE_CATALOG_BASE

_LOGGER = logging.getLogger(__name__)

_REFRESH_INTERVAL = timedelta(hours=12)
_FETCH_TIMEOUT = 15


@dataclass(frozen=True)
class FirmwareDecision:
    """The catalog's answer for one controller.

    ``update_available`` False means "offer nothing" (unknown product, already
    current, or downgrade suppressed); the other fields are then empty.
    """

    update_available: bool
    target_version: str | None
    file_url: str | None
    file_md5: str | None
    reason: str


def parse_decision(payload: object) -> FirmwareDecision | None:
    """Parse a ``GET /decision`` body. Returns None if it isn't a JSON object."""
    if not isinstance(payload, dict):
        return None
    reason = str(payload.get("reason") or "")
    if not payload.get("update_available"):
        return FirmwareDecision(False, None, None, None, reason)
    file = payload.get("file")
    file = file if isinstance(file, dict) else {}
    url = file.get("url")
    # target_version is top-level; fall back to the file's own version field.
    target = payload.get("target_version") or file.get("version")
    if not url or not target:
        # Flagged available but no usable image — treat as nothing to offer
        # rather than hand the hub a half-formed OTA target.
        return FirmwareDecision(False, None, None, None, reason or "incomplete")
    return FirmwareDecision(
        update_available=True,
        target_version=str(target),
        file_url=str(url),
        file_md5=str(file.get("md5") or "") or None,
        reason=reason,
    )


class EzloFirmwareDecisionCoordinator(DataUpdateCoordinator[FirmwareDecision | None]):
    """Per-hub firmware decision from the catalog's ``GET /decision``.

    Fetches only while ``is_active`` (the ``firmware_catalog`` flag) is True;
    otherwise the hub uses the legacy manifest path and this stays idle, keeping
    its last known decision. Fetch/parse failures are non-fatal and also keep the
    last known decision, so a catalog outage never breaks the update entity.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        connection: EzloHubConnection,
        is_active: Callable[[], bool],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} firmware decision {connection.serial}",
            update_interval=_REFRESH_INTERVAL,
        )
        self._session = session
        self._connection = connection
        self._is_active = is_active

    async def _async_update_data(self) -> FirmwareDecision | None:
        if not self._is_active():
            return self.data
        version = self._connection.firmware
        if not version:
            # Always send version; a missing version makes the server answer
            # update_available=false, so wait until the hub has reported it.
            return self.data
        params = {"version": version}
        if self._connection.model:
            params["model"] = self._connection.model
        if self._connection.chip:  # hub.info.get `hardware` (esp32/esp32s3/…)
            params["hardware"] = self._connection.chip
        try:
            async with self._session.get(
                f"{EZLOPI_FIRMWARE_CATALOG_BASE}/decision",
                params=params,
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT),
            ) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "firmware decision HTTP %s for %s; keeping last known",
                        response.status, self._connection.serial,
                    )
                    return self.data
                payload = await response.json(content_type=None)
        except Exception as err:  # noqa: BLE001 - optional data, never fatal
            _LOGGER.debug(
                "firmware decision fetch failed for %s: %s",
                self._connection.serial, err,
            )
            return self.data
        decision = parse_decision(payload)
        if decision is None:
            _LOGGER.debug(
                "firmware decision unparseable for %s", self._connection.serial
            )
            return self.data
        return decision
