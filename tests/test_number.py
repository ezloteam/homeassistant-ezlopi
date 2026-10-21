"""Tests for the LED-strip config number platform + its classification."""
from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.ezlopi.device_types import detect_device_platform
from custom_components.ezlopi.light import _setup_lights
from custom_components.ezlopi.number import (
    EzloColorChannelNumber,
    EzloNumber,
    _setup_numbers,
)

from .fixtures import make_coordinator, make_device, make_item


def _rgb(_id: str, device_id: str, name: str, value: dict) -> dict:
    return {"_id": _id, "deviceId": device_id, "name": name, "value": value,
            "valueType": "rgb", "hasSetter": True, "show": True}


# ---- classification ----

def test_config_items_classified_as_number() -> None:
    assert detect_device_platform(
        {"name": "led_count", "valueType": "int", "hasSetter": True, "value": 30}
    )["platform"] == "number"
    assert detect_device_platform(
        {"name": "boot_bri", "valueType": "int", "hasSetter": True, "value": 48}
    )["platform"] == "number"
    assert detect_device_platform(
        {"name": "boot_rgbcolor", "valueType": "rgb", "hasSetter": True, "value": {}}
    )["platform"] == "number_color"


def test_led_count_not_misread_as_light() -> None:
    # 30 is within 0-100 and int+setter -> the generic rule would call it a
    # light; the exact-name override must win.
    info = {"name": "led_count", "valueType": "int", "hasSetter": True, "value": 30}
    assert detect_device_platform(info)["platform"] == "number"


# ---- coordinator wiring ----

def _strip_coord(hass: HomeAssistant) -> Any:
    items = [
        make_item("dm", "d", "dimmer", 50, "int"),               # runtime brightness
        _rgb("rgb", "d", "rgbcolor", {"red": 1, "green": 2, "blue": 3, "cwhite": 4}),
        make_item("cnt", "d", "led_count", 30, "int"),           # config
        make_item("bb", "d", "boot_bri", 48, "int"),             # config
        _rgb("bc", "d", "boot_rgbcolor", {"red": 0, "green": 255, "blue": 0, "cwhite": 0}),
    ]
    return make_coordinator(hass, items, [make_device("d", "Strip", "")])


async def test_types_assigned_and_no_light_collision(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    assert coord.data["cnt"]["type"] == "number"
    assert coord.data["bb"]["type"] == "number"
    assert coord.data["bc"]["type"] == "number_color"
    # The runtime light still builds from dimmer + rgbcolor only — the config
    # items must not be consumed by it.
    lights: list[Any] = []
    _setup_lights(coord, lambda e: lights.extend(e))
    assert len(lights) == 1
    assert lights[0]._color_id == "rgb"  # runtime colour, not boot_rgbcolor


async def test_number_platform_builds_entities(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    created: list[Any] = []
    _setup_numbers(coord, lambda e: created.extend(e))
    # led_count + boot_bri (2 scalars) + 4 boot-colour channels = 6 entities.
    assert len(created) == 6
    assert sum(isinstance(e, EzloColorChannelNumber) for e in created) == 4
    assert all(e.entity_category is EntityCategory.CONFIG for e in created)
    # unique ids are distinct (the 4 channels share one item id).
    assert len({e.unique_id for e in created}) == 6


async def test_scalar_number_value_and_set(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    num = EzloNumber(coord, coord.data["cnt"])
    assert num.native_value == 30.0
    assert num.native_min_value == 1.0
    assert num.native_max_value == 1024.0
    await num.async_set_native_value(60)
    assert coord.connection.sent[-1] == ("cnt", 60)


async def test_boot_color_channel_read_and_write(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    blue = EzloColorChannelNumber(coord, coord.data["bc"], "blue", "blue")
    green = EzloColorChannelNumber(coord, coord.data["bc"], "green", "green")
    assert blue.native_value == 0.0
    assert green.native_value == 255.0
    assert blue.native_min_value == 0.0 and blue.native_max_value == 255.0
    # Setting one channel writes the WHOLE rgbcolor object (read-modify-write).
    await blue.async_set_native_value(128)
    item_id, value = coord.connection.sent[-1]
    assert item_id == "bc"
    assert value == {"red": 0, "green": 255, "blue": 128, "cwhite": 0}
