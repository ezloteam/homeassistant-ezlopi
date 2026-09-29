"""Firmware update platform for ezloPi controllers.

One update entity per controller: installed version comes from the hub's
``hub.info.get`` (live over the websocket), latest available comes from the
shared firmware manifest coordinator. Installing triggers the hub's OTA.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EzloConfigEntry
from .const import DOMAIN
from .coordinator import EzloDataUpdateCoordinator
from .firmware import EzloFirmwareCoordinator

PARALLEL_UPDATES = 0

_RELEASE_URL = "https://dl.mios.com/ezloPI/"


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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Re-render when the latest-version manifest refreshes.
        self.async_on_remove(
            self._firmware.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        # Installed version and the install action both need the hub online.
        return self.coordinator.connection.connected

    @property
    def installed_version(self) -> str | None:
        return self.coordinator.connection.firmware

    @property
    def latest_version(self) -> str | None:
        # Fall back to the installed version when the manifest is unavailable,
        # so HA doesn't render a spurious "unknown -> ..." update.
        return self._firmware.data or self.installed_version

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
        try:
            await self.coordinator.connection.async_start_firmware_update()
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_update_failed",
                translation_placeholders={"serial": self._serial},
            ) from err
