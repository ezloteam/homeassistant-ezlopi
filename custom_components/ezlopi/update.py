"""Firmware update platform for ezloPi controllers.

One update entity per controller: installed version comes from the hub's
``hub.info.get`` (live over the websocket), latest available comes from the
shared firmware manifest coordinator. Installing triggers the hub's OTA.
"""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EzloConfigEntry
from .const import DOMAIN
from .coordinator import EzloDataUpdateCoordinator
from .firmware import EzloFirmwareCoordinator, select_variant

PARALLEL_UPDATES = 0

_RELEASE_URL = "https://dl.mios.com/ezloPI/"
# The device gives no OTA progress and reboots mid-flash. async_install blocks
# until the hub reconnects on the new version (so HA shows "Installing…" the
# whole time); give up after this long as a backstop.
_INSTALL_TIMEOUT = 600


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzloConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one firmware update entity per controller."""
    runtime = entry.runtime_data
    firmware = runtime.firmware
    async_add_entities(
        EzloFirmwareUpdate(coordinator, firmware)
        for coordinator in runtime.coordinators
    )


class EzloFirmwareUpdate(
    CoordinatorEntity[EzloDataUpdateCoordinator], UpdateEntity
):
    """Firmware update entity for a single ezloPi controller."""

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(
        self,
        coordinator: EzloDataUpdateCoordinator,
        firmware: EzloFirmwareCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._firmware = firmware
        self._serial = coordinator.serial
        self._attr_unique_id = f"{self._serial}_firmware"
        # True while async_install is running; keeps the entity available (and
        # "Installing…") across the mid-OTA reboot instead of going unavailable.
        self._installing = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Re-render when the latest-version manifest refreshes.
        self.async_on_remove(
            self._firmware.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self._installing or self.coordinator.connection.connected

    @property
    def installed_version(self) -> str | None:
        return self.coordinator.connection.firmware

    @property
    def latest_version(self) -> str | None:
        # Fall back to the installed version when the manifest is unavailable,
        # so HA doesn't render a spurious "unknown -> ..." update.
        manifest = self._firmware.data
        return manifest.version if manifest else self.installed_version

    @property
    def release_url(self) -> str:
        return _RELEASE_URL

    @property
    def device_info(self) -> DeviceInfo:
        """The controller itself — distinct from the physical end-devices."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._serial}_controller")},
            name=f"ezloPi {self._serial}",
            manufacturer="ezloPi",
            model="ezloPi controller",
            sw_version=self.installed_version,
        )

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        connection = self.coordinator.connection
        manifest = self._firmware.data
        if manifest is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_manifest_unavailable",
                translation_placeholders={"serial": self._serial},
            )
        # Pick the exact image for this device (chip/version/model), matching
        # the Ezlo app — refuse rather than flash a guessed/wrong binary.
        variant = select_variant(
            manifest, connection.chip, connection.firmware, connection.model
        )
        if variant is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_variant_unknown",
                translation_placeholders={"serial": self._serial},
            )
        target = manifest.version
        # Stay available/"Installing…" for the whole flash+reboot: HA keeps the
        # update entity in progress while this coroutine runs, so we don't return
        # until the hub reconnects reporting the new version (or we time out).
        self._installing = True
        self.async_write_ha_state()
        try:
            try:
                await connection.async_start_firmware_update(target, variant.url)
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="firmware_update_failed",
                    translation_placeholders={"serial": self._serial},
                ) from err
            await self._async_wait_for_version(connection, target)
        finally:
            self._installing = False

    async def _async_wait_for_version(self, connection: Any, target: str) -> None:
        """Block until the hub reports ``target`` firmware, or time out."""
        done = asyncio.Event()

        @callback
        def _check() -> None:
            if connection.firmware == target:
                done.set()

        unsub = self.coordinator.async_add_listener(_check)
        try:
            _check()  # maybe already there
            async with asyncio.timeout(_INSTALL_TIMEOUT):
                await done.wait()
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_update_timeout",
                translation_placeholders={"serial": self._serial},
            ) from err
        finally:
            unsub()
