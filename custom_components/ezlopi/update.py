"""Firmware update platform for ezloPi controllers.

One update entity per controller: installed version comes from the hub's
``hub.info.get`` (live over the websocket); the latest available version and the
image to flash come from the server-side firmware catalog (``GET /decision``)
when the ``firmware_catalog`` feature flag is ON (the default), or from the
legacy dl.mios.com manifest + client-side variant picker when it is OFF.
Installing triggers the hub's OTA.
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
from .catalog import EzloFirmwareDecisionCoordinator
from .const import DOMAIN
from .coordinator import EzloDataUpdateCoordinator
from .firmware import EzloFirmwareCoordinator, select_variant
from .flags import FLAG_FIRMWARE_CATALOG, EzloFeatureFlags

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
    async_add_entities(
        EzloFirmwareUpdate(
            coordinator,
            runtime.firmware,
            runtime.firmware_decisions[coordinator.serial],
            runtime.flags,
        )
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
        decision: EzloFirmwareDecisionCoordinator,
        flags: EzloFeatureFlags,
    ) -> None:
        super().__init__(coordinator)
        self._firmware = firmware
        self._decision = decision
        self._flags = flags
        self._serial = coordinator.serial
        self._attr_unique_id = f"{self._serial}_firmware"
        # True while async_install is running; keeps the entity available (and
        # "Installing…") across the mid-OTA reboot instead of going unavailable.
        self._installing = False
        # Last installed version we saw, to re-query the catalog decision when
        # the hub reboots onto a different firmware.
        self._last_seen_version = coordinator.connection.firmware

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Re-render when either firmware source refreshes.
        self.async_on_remove(
            self._firmware.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            self._decision.async_add_listener(self.async_write_ha_state)
        )
        # Kick off the first catalog decision now that the hub has (likely)
        # reported its firmware; harmless no-op while the flag is OFF.
        if self._flags.is_enabled(FLAG_FIRMWARE_CATALOG):
            await self._decision.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        # When the hub reboots onto a new firmware version, the catalog's answer
        # for this device changes, so re-query it.
        version = self.coordinator.connection.firmware
        if version != self._last_seen_version:
            self._last_seen_version = version
            if self._flags.is_enabled(FLAG_FIRMWARE_CATALOG):
                self.hass.async_create_background_task(
                    self._decision.async_request_refresh(),
                    "ezlopi-fw-decision-refresh",
                )
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        return self._installing or self.coordinator.connection.connected

    @property
    def installed_version(self) -> str | None:
        return self.coordinator.connection.firmware

    @property
    def latest_version(self) -> str | None:
        # Fall back to the installed version when nothing is offered (or the
        # source is unavailable), so HA doesn't render a spurious update.
        if self._flags.is_enabled(FLAG_FIRMWARE_CATALOG):
            decision = self._decision.data
            if decision is not None and decision.update_available and decision.target_version:
                return decision.target_version
            return self.installed_version
        manifest = self._firmware.data
        return manifest.version if manifest else self.installed_version

    @property
    def release_url(self) -> str:
        return _RELEASE_URL

    @property
    def device_info(self) -> DeviceInfo:
        """The controller device — the SAME single device that carries this
        controller's lights/switches/sensors, so firmware is just one more
        entity on the ezloPi device rather than a separate controller device.
        """
        connection = self.coordinator.connection
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=connection.hub_name or f"ezloPi {self._serial}",
            manufacturer="ezloPi",
            model="ezloPi",
            sw_version=self.installed_version,
        )

    def _catalog_image(self) -> tuple[str, str]:
        """(version, url) to flash from the catalog decision, or raise."""
        decision = self._decision.data
        if (
            decision is None
            or not decision.update_available
            or not decision.file_url
            or not decision.target_version
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_no_update_offered",
                translation_placeholders={"serial": self._serial},
            )
        return decision.target_version, decision.file_url

    def _manifest_image(self) -> tuple[str, str]:
        """(version, url) to flash from the legacy manifest path, or raise."""
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
        return manifest.version, variant.url

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        if self._flags.is_enabled(FLAG_FIRMWARE_CATALOG):
            target, url = self._catalog_image()
        else:
            target, url = self._manifest_image()
        await self._async_run_install(target, url)

    async def _async_run_install(self, target: str, url: str) -> None:
        connection = self.coordinator.connection
        # Stay available/"Installing…" for the whole flash+reboot: HA keeps the
        # update entity in progress while this coroutine runs, so we don't return
        # until the hub reconnects reporting the new version (or we time out).
        self._installing = True
        self.async_write_ha_state()
        try:
            try:
                await connection.async_start_firmware_update(target, url)
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
