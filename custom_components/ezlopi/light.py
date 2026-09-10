"""Light platform for the ezloPi integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzloConfigEntry
from .coordinator import EzloDataUpdateCoordinator
from .entity import EzloEntity

PARALLEL_UPDATES = 0

_ON_VALUES = ("on", "true", "1")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzloConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one light per dimmer device.

    An ezloPi dimmer is two items — a brightness 'dimmer' item and an on/off
    'switch' item on the same device. They are merged into a single light, so a
    physical dimmer is one HA entity (added dynamically as devices appear).
    """
    for coordinator in entry.runtime_data.coordinators:
        _setup_lights(coordinator, async_add_entities)


def _setup_lights(
    coordinator: EzloDataUpdateCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    added: set[str] = set()  # by hub device id

    @callback
    def _add_new() -> None:
        levels: dict[str, dict[str, Any]] = {}
        onoff: dict[str, dict[str, Any]] = {}
        for element in coordinator.data.values():
            device_id = element.get("deviceId")
            if device_id is None:
                continue
            if element["type"] == "light":
                levels.setdefault(device_id, element)
            elif element["type"] == "switch":
                onoff.setdefault(device_id, element)

        new = []
        for device_id, level in levels.items():
            if device_id in added:
                continue
            added.add(device_id)
            new.append(EzloLight(coordinator, level, onoff.get(device_id)))
        if new:
            async_add_entities(new)

    coordinator.async_add_listener(_add_new)
    _add_new()


class EzloLight(EzloEntity, LightEntity):
    """An ezloPi dimmer: brightness from the level item, on/off from the switch."""

    _attr_translation_key = "dimmer"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(
        self,
        coordinator: EzloDataUpdateCoordinator,
        level_element: dict[str, Any],
        onoff_element: dict[str, Any] | None,
    ) -> None:
        super().__init__(coordinator, level_element)  # _device_id = level item
        self._onoff_id = onoff_element["id"] if onoff_element else None
        # The entity is the device's light, so it takes the device name.
        self._attr_name = None

    @property
    def _level(self) -> int:
        """Current brightness on the ezloPi 0-100 scale."""
        value = self._element.get("value")
        if isinstance(value, bool):
            return 100 if value else 0
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            lowered = value.lower()
            if lowered in ("on", "true"):
                return 100
            if lowered in ("off", "false"):
                return 0
            try:
                return int(value)
            except ValueError:
                return 0
        return 0

    def _onoff_value(self) -> Any:
        if self._onoff_id is None:
            return None
        return self.coordinator.data.get(self._onoff_id, {}).get("value")

    @property
    def is_on(self) -> bool:
        value = self._onoff_value()
        if value is None:  # no separate switch item; infer from brightness
            return self._level > 0
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in _ON_VALUES
        return bool(value)

    @property
    def brightness(self) -> int:
        # ezloPi 0-100 -> Home Assistant 0-255
        return int(self._level * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            # Setting the dimmer level (>=1) turns the load on by itself — the
            # hub flips the on/off item automatically. We must NOT also send the
            # on/off item: on this hardware an explicit "on" snaps the level back
            # to 100%, overriding the requested brightness.
            level = max(1, int(kwargs[ATTR_BRIGHTNESS] * 100 / 255))
            await self._async_set_value(self._device_id, level)
            return
        # Plain on with no target brightness: use the on/off item when present
        # (goes to full), otherwise drive the dimmer to its last level or full.
        if self._onoff_id is not None:
            await self._async_set_value(self._onoff_id, True)
        else:
            await self._async_set_value(self._device_id, self._level or 100)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._onoff_id is not None:
            await self._async_set_value(self._onoff_id, False)
        else:
            await self._async_set_value(self._device_id, 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        element = self._element
        return {
            "device_name": element.get("deviceName"),
            "hubDeviceId": element.get("deviceId"),
            "brightness_percent": self._level,
        }
