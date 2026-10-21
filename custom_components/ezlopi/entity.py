"""Base entity for the ezloPi integration.

Every platform entity is a :class:`CoordinatorEntity` keyed by its hub item id.
The shared device identity (name, unique id, device_info), availability and the
small battery helper live here so the platforms only carry their own value logic.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EzloDataUpdateCoordinator


def async_setup_ezlo_platform(
    coordinator: EzloDataUpdateCoordinator,
    item_type: str,
    factory: Callable[[EzloDataUpdateCoordinator, dict[str, Any]], Any],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add entities of ``item_type`` now and whenever new items appear.

    Supports dynamic-devices: items the hub reports after setup are picked up on
    the next coordinator push and added automatically.
    """
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for item_id, element in coordinator.data.items():
            if item_id in known or element.get("type") != item_type:
                continue
            entity = factory(coordinator, element)
            if entity is None:  # factory chose to skip this item
                continue
            known.add(item_id)
            new.append(entity)
        if new:
            async_add_entities(new)

    coordinator.async_add_listener(_add_new)
    _add_new()


class EzloEntity(CoordinatorEntity[EzloDataUpdateCoordinator]):
    """Common base for all ezloPi entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EzloDataUpdateCoordinator, element: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._serial = coordinator.serial
        self._device_id = str(element["id"])
        self._device_name = str(element["name"])
        # The parent physical device this item belongs to.
        self._hub_device_id = element.get("deviceId")
        # NB: not `_name_internal` — that name collides with HA's Entity method.
        self._ezlo_device_name = element.get("deviceName") or ""
        self._attr_unique_id = f"{self._serial}_{self._device_id}"
        # With has_entity_name, the friendly name is "<device> <this>". The item
        # name (e.g. "dimmer") is the per-entity part; the device carries the
        # physical device name (e.g. "Frankever Dimmer").
        self._attr_name = (element.get("name") or "").replace("_", " ").title() or None

    @property
    def _element(self) -> dict[str, Any]:
        """The current processed item for this entity (from the coordinator)."""
        element: dict[str, Any] = self.coordinator.data.get(self._device_id, {})
        return element

    @property
    def available(self) -> bool:
        """Available only while the hub is connected and still reports this item."""
        return (
            super().available
            and self.coordinator.connection.connected
            and self._device_id in self.coordinator.data
        )

    @property
    def device_info(self) -> DeviceInfo:
        """One HA device per ezloPi controller — every entity (lights, switches,
        sensors and the firmware update) lives on it, keyed by the controller
        serial, so an ezloPi shows up as a single device with all its entities.
        """
        connection = self.coordinator.connection
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=connection.hub_name or f"ezloPi {self._serial}",
            manufacturer="ezloPi",
            model="ezloPi",
            sw_version=connection.firmware,
        )

    async def _async_set_value(self, item_id: str, value: Any) -> None:
        """Send a value to the hub, surfacing failures as a translatable error."""
        try:
            await self.coordinator.connection.async_set_item_value(item_id, value)
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"name": self._device_name},
            ) from err

    def get_device_name(self) -> str:
        return self._device_name

    def get_device_id(self) -> str:
        return self._device_id

    @staticmethod
    def _get_battery_status(battery_level: Any) -> str:
        """Map a battery percentage to a coarse status string."""
        if battery_level is None:
            return "unknown"
        try:
            level = float(battery_level)
        except (ValueError, TypeError):
            return "unknown"
        if level > 75:
            return "good"
        elif level > 50:
            return "fair"
        elif level > 25:
            return "low"
        elif level > 10:
            return "critical"
        else:
            return "very_low"
