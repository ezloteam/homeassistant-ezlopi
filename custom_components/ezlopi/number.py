"""Number platform for ezloPi LED-strip configuration items.

Exposes the device-0009 strip tunables the firmware ships as NVS-persisted
settable scalars — currently the LED count. These are configuration values
(not live controls), so they are grouped under the device's configuration
category.

The power-on indicator (boot colour + brightness) is NOT a number here: it is
a config-category light so it can use Home Assistant's native colour wheel and
brightness slider (see :mod:`.light`).
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

_FRIENDLY_NAMES = {"led_count": "LED count"}


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
