"""Tests for the LED-strip config items.

``led_count`` is a config Number. The power-on indicator (boot_bri +
boot_rgbcolor) is a SEPARATE, config-category light so it uses Home Assistant's
native colour wheel + brightness slider rather than raw channel sliders.
"""
from typing import Any

from homeassistant.components.light import ATTR_RGBW_COLOR, ColorMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.ezlopi import (
    EzloRuntimeData,
    _async_remove_migrated_boot_entities,
)
from custom_components.ezlopi.const import DOMAIN
from custom_components.ezlopi.device_types import detect_device_platform
from custom_components.ezlopi.light import EzloLight, _setup_lights
from custom_components.ezlopi.number import EzloNumber, _setup_numbers

from .fixtures import SERIAL, make_coordinator, make_device, make_item


def _rgb(_id: str, device_id: str, name: str, value: dict) -> dict:
    return {"_id": _id, "deviceId": device_id, "name": name, "value": value,
            "valueType": "rgb", "hasSetter": True, "show": True}


# ---- classification ----

def test_config_items_classified() -> None:
    assert detect_device_platform(
        {"name": "led_count", "valueType": "int", "hasSetter": True, "value": 30}
    )["platform"] == "number"
    # The boot indicator is a config-category light, not a number.
    assert detect_device_platform(
        {"name": "boot_bri", "valueType": "int", "hasSetter": True, "value": 48}
    )["platform"] == "config_light"
    assert detect_device_platform(
        {"name": "boot_rgbcolor", "valueType": "rgb", "hasSetter": True, "value": {}}
    )["platform"] == "config_light_color"


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
        make_item("cnt", "d", "led_count", 30, "int"),           # config scalar
        make_item("bb", "d", "boot_bri", 48, "int"),             # boot brightness
        _rgb("bc", "d", "boot_rgbcolor", {"red": 0, "green": 255, "blue": 0, "cwhite": 0}),
    ]
    return make_coordinator(hass, items, [make_device("d", "Strip", "")])


async def test_types_assigned(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    assert coord.data["cnt"]["type"] == "number"
    assert coord.data["bb"]["type"] == "config_light"
    assert coord.data["bc"]["type"] == "config_light_color"


async def test_two_lights_runtime_and_boot(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    lights: list[Any] = []
    _setup_lights(coord, lambda e: lights.extend(e))
    # One runtime light (dimmer + rgbcolor) + one boot light (boot_bri +
    # boot_rgbcolor) = two, each on its own item id (distinct unique ids).
    assert len(lights) == 2
    assert len({light.unique_id for light in lights}) == 2
    by_color = {light._color_id: light for light in lights}
    runtime = by_color["rgb"]
    boot = by_color["bc"]
    # Runtime light is the device's primary (no name, no category, 0-100 scale).
    assert runtime.entity_category is None
    assert runtime.name is None
    assert runtime._scale == 100
    # Boot light is a config-category RGBW light with an explicit sub-name.
    assert boot.entity_category is EntityCategory.CONFIG
    assert boot.name == "Boot color"
    assert boot._scale == 255
    assert boot.supported_color_modes == {ColorMode.RGBW}


async def test_boot_light_reads_brightness_and_colour(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    boot = EzloLight(
        coord, coord.data["bb"], coord.data["bc"], scale=255,
        entity_category=EntityCategory.CONFIG, name="Boot light",
    )
    # boot_bri is already 0-255, so it maps straight through (no 0-100 scaling).
    assert boot.is_on is True
    assert boot.brightness == 48
    assert boot.rgbw_color == (0, 255, 0, 0)


async def test_boot_light_set_brightness_and_colour(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    boot = EzloLight(coord, coord.data["bb"], coord.data["bc"], scale=255)
    # Brightness is written on the boot_bri scale (identity 0-255), not /100.
    await boot.async_turn_on(brightness=128)
    assert coord.connection.sent[-1] == ("bb", 128)
    # Colour writes the whole rgbcolor object to the boot_rgbcolor item.
    await boot.async_turn_on(**{ATTR_RGBW_COLOR: (10, 20, 30, 40)})
    assert coord.connection.sent[-1] == (
        "bc", {"red": 10, "green": 20, "blue": 30, "cwhite": 40}
    )
    # Off zeroes the boot brightness.
    await boot.async_turn_off()
    assert coord.connection.sent[-1] == ("bb", 0)


# ---- led_count number ----

async def test_number_platform_builds_only_led_count(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    created: list[Any] = []
    _setup_numbers(coord, lambda e: created.extend(e))
    assert len(created) == 1
    assert created[0].entity_category is EntityCategory.CONFIG


async def test_scalar_number_value_and_set(hass: HomeAssistant) -> None:
    coord = _strip_coord(hass)
    num = EzloNumber(coord, coord.data["cnt"])
    assert num.native_value == 30.0
    assert num.native_min_value == 1.0
    assert num.native_max_value == 1024.0
    await num.async_set_native_value(60)
    assert coord.connection.sent[-1] == ("cnt", 60)


# ---- 1.4.0 -> 1.5.0 migration: drop the old boot Number entities ----

async def test_migration_removes_old_boot_numbers(hass: HomeAssistant) -> None:
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    coord = _strip_coord(hass)  # boot_bri "bb" (config_light), boot_rgbcolor "bc"
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="migration")
    entry.add_to_hass(hass)
    reg = er.async_get(hass)

    # Pre-1.5.0 Number entities that must be removed.
    gone = [reg.async_get_or_create(
        "number", DOMAIN, f"{SERIAL}_bb", config_entry=entry).entity_id]
    for ch in ("red", "green", "blue", "cwhite"):
        gone.append(reg.async_get_or_create(
            "number", DOMAIN, f"{SERIAL}_bc_{ch}", config_entry=entry).entity_id)

    # The new boot light shares the boot_bri item id but is in the light domain
    # — it must survive. An unrelated config Number (led_count) must too.
    boot_light = reg.async_get_or_create(
        "light", DOMAIN, f"{SERIAL}_bb", config_entry=entry).entity_id
    led_count = reg.async_get_or_create(
        "number", DOMAIN, f"{SERIAL}_cnt", config_entry=entry).entity_id

    runtime = EzloRuntimeData(api=None, firmware=None, coordinators=[coord])  # type: ignore[arg-type]
    _async_remove_migrated_boot_entities(hass, entry, runtime)

    for entity_id in gone:
        assert reg.async_get(entity_id) is None, f"{entity_id} should be removed"
    assert reg.async_get(boot_light) is not None, "boot light must survive"
    assert reg.async_get(led_count) is not None, "led_count must survive"
