"""Light platform for the ezloPi integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzloConfigEntry
from .coordinator import EzloDataUpdateCoordinator
from .entity import EzloEntity

PARALLEL_UPDATES = 0

# The device-0009 on-device effect engine's effect names (firmware token-enum).
# Rendered locally on the strip; Home Assistant only selects the effect name.
EFFECT_LIST = [
    "solid", "rainbow", "color_wipe", "breathe", "theater_chase", "gradient",
]


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
    added: set[str] = set()  # by hub item id (unique per light)

    @callback
    def _add_new() -> None:
        # Two kinds of light live on a strip device, kept apart so they never
        # merge into one entity:
        #   - the runtime light (dimmer level 0-100 + rgbcolor), and
        #   - the boot colour (boot_bri 0-255 + boot_rgbcolor), a
        #     config-category light so the power-on colour/brightness is set
        #     with Home Assistant's native colour wheel instead of raw sliders.
        levels: dict[str, dict[str, Any]] = {}
        colors: dict[str, dict[str, Any]] = {}
        effects: dict[str, dict[str, Any]] = {}
        config_levels: dict[str, dict[str, Any]] = {}
        config_colors: dict[str, dict[str, Any]] = {}
        for element in coordinator.data.values():
            device_id = element.get("deviceId")
            if device_id is None:
                continue
            if element["type"] == "light":
                levels.setdefault(device_id, element)
            elif element["type"] == "light_color":
                colors.setdefault(device_id, element)
            elif element["type"] == "light_effect":
                effects.setdefault(device_id, element)
            elif element["type"] == "config_light":
                config_levels.setdefault(device_id, element)
            elif element["type"] == "config_light_color":
                config_colors.setdefault(device_id, element)

        new = []
        for device_id, level in levels.items():
            if level["id"] in added:
                continue
            added.add(level["id"])
            new.append(EzloLight(
                coordinator, level, colors.get(device_id),
                effect_element=effects.get(device_id),
            ))
        for device_id, level in config_levels.items():
            if level["id"] in added:
                continue
            added.add(level["id"])
            new.append(EzloLight(
                coordinator, level, config_colors.get(device_id),
                scale=255, entity_category=EntityCategory.CONFIG,
                name="Boot color", translation_key=None,
                icon="mdi:lightbulb-on-outline",
            ))
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

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(
        self,
        coordinator: EzloDataUpdateCoordinator,
        level_element: dict[str, Any],
        color_element: dict[str, Any] | None = None,
        *,
        effect_element: dict[str, Any] | None = None,
        scale: int = 100,
        entity_category: EntityCategory | None = None,
        name: str | None = None,
        translation_key: str | None = "dimmer",
        icon: str | None = None,
    ) -> None:
        super().__init__(coordinator, level_element)  # _device_id = level item
        self._color_id = color_element["id"] if color_element else None
        self._effect_id = effect_element["id"] if effect_element else None
        # Full-brightness value on the ezloPi side: the runtime dimmer runs
        # 0-100, the boot-brightness item 0-255. HA brightness is always 0-255.
        self._scale = scale
        # With an rgbcolor item the light is a full RGBW color light (rgb + an
        # independent white channel); otherwise it's brightness-only.
        if self._color_id is not None:
            self._attr_color_mode = ColorMode.RGBW
            self._attr_supported_color_modes = {ColorMode.RGBW}
        # With an effect item the strip renders animations on-device; expose the
        # firmware's effect names via Home Assistant's native effect selector.
        if self._effect_id is not None:
            self._attr_supported_features = LightEntityFeature.EFFECT
            self._attr_effect_list = EFFECT_LIST
        self._attr_entity_category = entity_category
        self._attr_translation_key = translation_key
        if icon is not None:
            self._attr_icon = icon
        # The runtime light is the device's primary light (name=None -> device
        # name); the boot light carries an explicit sub-name.
        self._attr_name = name

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

    def _effect_value(self) -> dict[str, Any]:
        """The current effect item object ({effect, speed, intensity})."""
        if self._effect_id is None:
            return {}
        value = self.coordinator.data.get(self._effect_id, {}).get("value")
        return value if isinstance(value, dict) else {}

    @property
    def effect(self) -> str | None:
        """Currently selected on-device effect name."""
        effect = self._effect_value().get("effect")
        return effect if isinstance(effect, str) else None

    @property
    def _level(self) -> int:
        """Current brightness on the ezloPi scale (0-100 dimmer, 0-255 boot)."""
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
        # ezloPi scale -> Home Assistant 0-255
        return min(255, round(self._level * 255 / self._scale))

    def _on_value(self) -> Any:
        """Value to drive the level item on to, matching its value type."""
        if isinstance(self._element.get("value"), bool):
            return True
        return self._level or self._scale

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

        # Effect is independent of power/colour: select it on the effect item,
        # preserving the current speed/intensity (one dictionary item).
        if self._effect_id is not None and ATTR_EFFECT in kwargs:
            effect = dict(self._effect_value())
            effect["effect"] = kwargs[ATTR_EFFECT]
            effect.setdefault("speed", 50)
            effect.setdefault("intensity", 50)
            await self._async_set_value(self._effect_id, effect)

        if ATTR_BRIGHTNESS in kwargs:
            # Setting the level (>=1) turns the load on at that brightness.
            level = max(1, round(kwargs[ATTR_BRIGHTNESS] * self._scale / 255))
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
