"""Number platform for ezloPi LED-strip configuration items.

Exposes the device-0009 strip tunables the firmware ships as NVS-persisted
settable items: the LED count and the power-on indicator (boot colour +
brightness). These are configuration values (not live controls), so they are
grouped under the device's configuration category.

The boot colour is a single ``rgbcolor`` item ({red,green,blue,cwhite}); it is
presented as one number entity per channel, each doing a read-modify-write of
the whole colour object so the firmware's single-item model is preserved.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzloConfigEntry
from .coordinator import EzloDataUpdateCoordinator
from .entity import EzloEntity

PARALLEL_UPDATES = 0

# Order + display label for the boot-colour channels of the rgbcolor object.
_COLOR_CHANNELS = (("red", "red"), ("green", "green"), ("blue", "blue"),
                   ("cwhite", "white"))

_FRIENDLY_NAMES = {"led_count": "LED count", "boot_bri": "Boot brightness"}


def _mode(name: str | None) -> NumberMode:
    return NumberMode.BOX if name == "box" else NumberMode.SLIDER


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzloConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities for the strip config items as they appear."""
    for coordinator in entry.runtime_data.coordinators:
        _setup_numbers(coordinator, async_add_entities)


def _setup_numbers(
    coordinator: EzloDataUpdateCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    added: set[str] = set()  # by item id

    @callback
    def _add_new() -> None:
        new: list[NumberEntity] = []
        for item_id, element in coordinator.data.items():
            if item_id in added:
                continue
            if element.get("type") == "number":
                added.add(item_id)
                new.append(EzloNumber(coordinator, element))
            elif element.get("type") == "number_color":
                added.add(item_id)
                new.extend(
                    EzloColorChannelNumber(coordinator, element, key, label)
                    for key, label in _COLOR_CHANNELS
                )
        if new:
            async_add_entities(new)

    coordinator.async_add_listener(_add_new)
    _add_new()


class _EzloNumberBase(EzloEntity, NumberEntity):
    """Shared config-number wiring (category, range, step, slider/box mode)."""

    _attr_entity_category = EntityCategory.CONFIG

    def _init_bounds(self, element: dict[str, Any]) -> None:
        self._attr_native_min_value = float(element.get("native_min", 0))
        self._attr_native_max_value = float(element.get("native_max", 255))
        self._attr_native_step = float(element.get("native_step", 1))
        self._attr_mode = _mode(element.get("mode"))
        if element.get("icon"):
            self._attr_icon = element["icon"]


class EzloNumber(_EzloNumberBase):
    """A single integer strip setting (led_count, boot_bri)."""

    def __init__(
        self, coordinator: EzloDataUpdateCoordinator, element: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, element)
        self._init_bounds(element)
        self._attr_name = _FRIENDLY_NAMES.get(element.get("name", ""), self._attr_name)

    @property
    def native_value(self) -> float | None:
        value = self._element.get("value")
        if isinstance(value, (int, float)):
            return float(value)
        return None

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set_value(self._device_id, int(value))


class EzloColorChannelNumber(_EzloNumberBase):
    """One channel (red/green/blue/white) of the boot-colour rgbcolor object."""

    def __init__(
        self,
        coordinator: EzloDataUpdateCoordinator,
        element: dict[str, Any],
        channel: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, element)
        self._channel = channel
        self._init_bounds(element)
        # Fixed 0-255 range per channel regardless of element metadata.
        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 255.0
        self._attr_native_step = 1.0
        self._attr_mode = NumberMode.SLIDER
        self._attr_name = f"Boot color {label}"
        # Distinct unique id per channel (the base id is shared by all four).
        self._attr_unique_id = f"{self._serial}_{self._device_id}_{channel}"

    def _color(self) -> dict[str, Any]:
        value = self._element.get("value")
        return value if isinstance(value, dict) else {}

    @property
    def native_value(self) -> float | None:
        value = self._color().get(self._channel)
        return float(value) if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        # Read-modify-write the whole colour object (the firmware item is one
        # rgbcolor value, not per-channel items).
        color = dict(self._color())
        color[self._channel] = int(value)
        color.setdefault("red", 0)
        color.setdefault("green", 0)
        color.setdefault("blue", 0)
        color.setdefault("cwhite", 0)
        await self._async_set_value(self._device_id, color)
