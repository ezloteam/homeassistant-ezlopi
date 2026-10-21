"""Light platform for the ezloPi integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzloConfigEntry
from .coordinator import EzloDataUpdateCoordinator
from .entity import EzloEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzloConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one light per dimmer device.

    An ezloPi dimmer exposes a brightness 'dimmer' (level) item — the
    authoritative on/off, since level 0 is off and level > 0 is on — plus, on a
    colour strip, an 'rgbcolor' item. They become a single light, so a physical
    dimmer is one HA entity (added dynamically as devices appear).
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
        colors: dict[str, dict[str, Any]] = {}
        for element in coordinator.data.values():
            device_id = element.get("deviceId")
            if device_id is None:
                continue
            if element["type"] == "light":
                levels.setdefault(device_id, element)
            elif element["type"] == "light_color":
                colors.setdefault(device_id, element)

        new = []
        for device_id, level in levels.items():
            if device_id in added:
                continue
            added.add(device_id)
            new.append(EzloLight(coordinator, level, colors.get(device_id)))
        if new:
            async_add_entities(new)

    coordinator.async_add_listener(_add_new)
    _add_new()


class EzloLight(EzloEntity, LightEntity):
    """An ezloPi dimmer light.

    On/off and brightness are both driven by the dimmer *level* item: level 0
    is off, level > 0 is on. The companion "switch" item is deliberately NOT
    used — on some hubs (e.g. the SK6812 RGBW strip) setting it true is a no-op
    and its value never reflects the real on-state, so relying on it made the
    light impossible to turn on from Home Assistant. Driving the level item is
    the reliable primitive across all ezloPi dimmers.
    """

    _attr_translation_key = "dimmer"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(
        self,
        coordinator: EzloDataUpdateCoordinator,
        level_element: dict[str, Any],
        color_element: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(coordinator, level_element)  # _device_id = level item
        self._color_id = color_element["id"] if color_element else None
        # With an rgbcolor item the light is a full RGBW color light (rgb + an
        # independent white channel); otherwise it's brightness-only.
        if self._color_id is not None:
            self._attr_color_mode = ColorMode.RGBW
            self._attr_supported_color_modes = {ColorMode.RGBW}
        # The entity is the device's light, so it takes the device name.
        self._attr_name = None

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        """Current colour as (red, green, blue, white), each 0-255."""
        if self._color_id is None:
            return None
        value = self.coordinator.data.get(self._color_id, {}).get("value")
        if not isinstance(value, dict):
            return None
        return (
            int(value.get("red", 0)),
            int(value.get("green", 0)),
            int(value.get("blue", 0)),
            int(value.get("cwhite", 0)),
        )

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

    @property
    def is_on(self) -> bool:
        # The dimmer level is the source of truth: level > 0 means on. The
        # "switch" item is not consulted (it is unreliable on some hubs).
        return self._level > 0

    @property
    def brightness(self) -> int:
        # ezloPi 0-100 -> Home Assistant 0-255
        return int(self._level * 255 / 100)

    def _on_value(self) -> Any:
        """Value to drive the level item on to, matching its value type."""
        if isinstance(self._element.get("value"), bool):
            return True
        return self._level or 100

    def _off_value(self) -> Any:
        """Value to drive the level item off, matching its value type."""
        if isinstance(self._element.get("value"), bool):
            return False
        return 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Colour is independent of power — write it any time; it's visible once
        # brightness > 0. (cwhite is a no-op on non-RGBW strips, real on RGBW.)
        if self._color_id is not None and ATTR_RGBW_COLOR in kwargs:
            red, green, blue, white = kwargs[ATTR_RGBW_COLOR]
            await self._async_set_value(self._color_id, {
                "red": int(red), "green": int(green),
                "blue": int(blue), "cwhite": int(white),
            })

        if ATTR_BRIGHTNESS in kwargs:
            # Setting the dimmer level (>=1) turns the load on at that level.
            level = max(1, int(kwargs[ATTR_BRIGHTNESS] * 100 / 255))
            await self._async_set_value(self._device_id, level)
        elif not self.is_on:
            # Plain on (no brightness): drive the level to its last value or
            # full. This is the reliable on primitive — the switch item is not
            # used because setting it true is a no-op on some strips.
            await self._async_set_value(self._device_id, self._on_value())

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Level 0 is off on every ezloPi dimmer (the switch item's "false" also
        # just zeroes the level), so drive the level item directly.
        await self._async_set_value(self._device_id, self._off_value())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        element = self._element
        return {
            "device_name": element.get("deviceName"),
            "hubDeviceId": element.get("deviceId"),
            "brightness_percent": self._level,
        }
