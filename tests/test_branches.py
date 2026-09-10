"""Branch coverage for entity value parsing, helpers and detection."""
from typing import Any

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.ezlopi.binary_sensor import EzloBinarySensor
from custom_components.ezlopi.climate import EzloClimate
from custom_components.ezlopi.device_types import (
    detect_by_value_type,
    get_additional_entities,
    should_create_multiple_entities,
)
from custom_components.ezlopi.light import EzloLight
from custom_components.ezlopi.entity import EzloEntity
from custom_components.ezlopi.lock import EzloLock
from custom_components.ezlopi.sensor import EzloSensor

from .fixtures import make_coordinator, make_device, make_item


def _one(hass: HomeAssistant, item: dict, device: dict) -> Any:
    coord = make_coordinator(hass, [item], [device])
    return coord, coord.data[item["_id"]]


# ---- light ----

@pytest.mark.parametrize(
    ("value", "vtype", "is_on", "level"),
    [
        (75, "int", True, 75),
        (0, "int", False, 0),
        ("on", "token", True, 100),
        ("off", "token", False, 0),
        (True, "bool", True, 100),
        ("40", "token", True, 40),
        ("weird", "token", False, 0),
    ],
)
async def test_light_value_parsing(
    hass: HomeAssistant, value: Any, vtype: str, is_on: bool, level: int
) -> None:
    coord, el = _one(hass, make_item("i", "d", "dimmer", value, vtype),
                     make_device("d", "Dev", "dimmer.inwall"))
    light = EzloLight(coord, el, None)
    assert light.is_on is is_on
    assert light.extra_state_attributes["brightness_percent"] == level


async def test_light_turn_on_with_brightness(hass: HomeAssistant) -> None:
    coord, el = _one(hass, make_item("i", "d", "dimmer", 0, "int"),
                     make_device("d", "Dev", "dimmer.inwall"))
    light = EzloLight(coord, el, None)
    await light.async_turn_on(brightness=255)
    assert coord.connection.sent[-1] == ("i", 100)


# ---- dimmer = one light merging the brightness + on/off items ----

def test_device_name_does_not_classify_items() -> None:
    # On/off item on a device named "...Dimmer" must stay a switch, not a light.
    from custom_components.ezlopi.device_types import detect_device_platform
    info = {"name": "switch", "valueType": "bool", "hasSetter": True,
            "deviceName": "Frankever Dimmer"}
    assert detect_device_platform(info)["platform"] == "switch"


async def test_dimmer_merges_switch_into_light(hass: HomeAssistant) -> None:
    from custom_components.ezlopi.light import _setup_lights
    from custom_components.ezlopi.switch import _make_switch

    items = [
        make_item("dm", "d", "dimmer", 60, "int"),   # brightness item
        make_item("sw", "d", "switch", True, "bool"),  # on/off item
    ]
    coord = make_coordinator(hass, items, [make_device("d", "Frankever Dimmer", "")])

    # detection: dimmer -> light, on/off -> switch (device name ignored)
    assert coord.data["dm"]["type"] == "light"
    assert coord.data["sw"]["type"] == "switch"

    # the on/off item is owned by the light, so no standalone switch
    assert _make_switch(coord, coord.data["sw"]) is None

    # _setup_lights builds exactly one light for the device
    added: list[Any] = []
    _setup_lights(coord, lambda e: added.extend(e))
    assert len(added) == 1
    light = added[0]

    # on/off comes from the switch item, brightness from the dimmer item
    assert light.is_on is True
    assert light.brightness == int(60 * 255 / 100)
    await light.async_turn_off()
    assert ("sw", False) in coord.connection.sent
    await light.async_turn_on(brightness=255)
    assert ("dm", 100) in coord.connection.sent  # brightness -> dimmer item only
    # The on/off item must NOT be sent for a brightness command: an explicit
    # "on" snaps the hardware to 100%, overriding the requested brightness.
    assert ("sw", True) not in coord.connection.sent


async def test_standalone_switch_is_created(hass: HomeAssistant) -> None:
    from custom_components.ezlopi.switch import _make_switch
    coord, el = _one(hass, make_item("sw", "d", "relay", True, "bool"),
                     make_device("d", "Outlet", "switch.inwall"))
    switch = _make_switch(coord, el)
    assert switch is not None  # no light on the device
    assert switch.is_on is True
    await switch.async_turn_on()
    await switch.async_turn_off()
    assert coord.connection.sent[-1] == ("sw", False)


@pytest.mark.parametrize(
    ("value", "is_on"),
    [("on", True), ("off", False), (1, True), (0, False), (True, True)],
)
async def test_dimmer_onoff_value_types(
    hass: HomeAssistant, value: Any, is_on: bool
) -> None:
    coord = make_coordinator(hass, [make_item("dm", "d", "dimmer", 50, "int")],
                             [make_device("d", "Dim", "")])
    light = EzloLight(coord, coord.data["dm"], {"id": "sw"})
    coord.data["sw"] = {"value": value}
    assert light.is_on is is_on


async def test_dimmer_set_brightness_does_not_send_onoff(hass: HomeAssistant) -> None:
    """Setting brightness must drive only the dimmer item.

    On the hardware, an explicit on/off "on" snaps the level to 100%, so a
    brightness command that also sent the on/off item would jump back to full.
    """
    from homeassistant.components.light import ATTR_BRIGHTNESS

    coord = make_coordinator(hass, [make_item("dm", "d", "dimmer", 0, "int")],
                             [make_device("d", "Dim", "")])
    light = EzloLight(coord, coord.data["dm"], {"id": "sw"})
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 127})
    # Exactly one command, to the dimmer item, at the mapped level (127->49).
    assert coord.connection.sent == [("dm", 49)]
    assert not any(item_id == "sw" for item_id, _ in coord.connection.sent)


async def test_dimmer_brightness_clamped_to_minimum(hass: HomeAssistant) -> None:
    """A tiny brightness maps to at least level 1 so it doesn't turn the load off."""
    from homeassistant.components.light import ATTR_BRIGHTNESS

    coord = make_coordinator(hass, [make_item("dm", "d", "dimmer", 0, "int")],
                             [make_device("d", "Dim", "")])
    light = EzloLight(coord, coord.data["dm"], {"id": "sw"})
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 1})  # 1 -> int(0.39) -> clamp to 1
    assert coord.connection.sent == [("dm", 1)]


async def test_dimmer_plain_on_uses_onoff(hass: HomeAssistant) -> None:
    """Plain on (no brightness) uses the on/off item when present."""
    coord = make_coordinator(hass, [make_item("dm", "d", "dimmer", 0, "int")],
                             [make_device("d", "Dim", "")])
    light = EzloLight(coord, coord.data["dm"], {"id": "sw"})
    await light.async_turn_on()
    assert coord.connection.sent == [("sw", True)]


async def test_setup_lights_skips_items_without_device(hass: HomeAssistant) -> None:
    from custom_components.ezlopi.light import _setup_lights
    coord = make_coordinator(hass, [], [])
    coord.data = {"x": {"id": "x", "deviceId": None, "type": "light"}}
    added: list[Any] = []
    _setup_lights(coord, lambda e: added.extend(e))
    assert added == []  # item with no parent device is skipped


# ---- lock ----

@pytest.mark.parametrize(
    ("value", "vtype", "locked"),
    [(True, "bool", True), ("unlocked", "token", False), (1, "int", True),
     (None, "token", None)],
)
async def test_lock_value_parsing(
    hass: HomeAssistant, value: Any, vtype: str, locked: Any
) -> None:
    coord, el = _one(hass, make_item("i", "d", "bolt", value, vtype),
                     make_device("d", "Door", "doorlock"))
    assert EzloLock(coord, el).is_locked is locked


# ---- climate ----

async def test_climate_branches(hass: HomeAssistant) -> None:
    coord, el = _one(hass, make_item("i", "d", "setpoint", 18.0, "float"),
                     make_device("d", "T", "thermostat"))
    climate = EzloClimate(coord, el)
    climate.async_write_ha_state = lambda: None  # type: ignore[method-assign]

    climate._attr_hvac_mode = HVACMode.HEAT
    climate._attr_target_temperature = 22.0
    assert climate.hvac_action is HVACAction.HEATING
    climate._attr_hvac_mode = HVACMode.COOL
    assert climate.hvac_action is HVACAction.IDLE

    await climate.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
    assert coord.connection.sent[-1] == ("i", 23.0)
    await climate.async_turn_off()
    assert climate.hvac_mode is HVACMode.OFF
    await climate.async_turn_on()
    assert climate.hvac_mode is HVACMode.HEAT


async def test_climate_cooling_and_no_temperature(hass: HomeAssistant) -> None:
    coord, el = _one(hass, make_item("i", "d", "setpoint", 25.0, "float"),
                     make_device("d", "T", "thermostat"))
    climate = EzloClimate(coord, el)
    climate._attr_hvac_mode = HVACMode.COOL
    climate._attr_target_temperature = 20.0
    assert climate.hvac_action is HVACAction.COOLING
    climate._attr_hvac_mode = HVACMode.OFF
    assert climate.hvac_action is HVACAction.OFF
    # set_temperature with no temperature is a no-op
    await climate.async_set_temperature()
    assert coord.connection.sent == []


async def test_climate_current_temp_edge_cases(hass: HomeAssistant) -> None:
    coord, el = _one(hass, make_item("i", "d", "setpoint", 0.0, "float"),
                     make_device("d", "T", "thermostat"))
    climate = EzloClimate(coord, el)
    coord.data["i"]["value"] = {"mode": "heat"}  # dict without current_temp
    assert climate.current_temperature is None
    coord.data["i"]["value"] = "abc"  # non-numeric
    assert climate.current_temperature is None
    coord.data["i"]["value"] = 25.0
    climate._attr_hvac_mode = HVACMode.HEAT
    climate._attr_target_temperature = 20.0  # current >= target -> idle
    assert climate.hvac_action is HVACAction.IDLE


async def test_climate_fahrenheit_and_dict_value(hass: HomeAssistant) -> None:
    item = make_item("i", "d", "setpoint", {"current_temp": 68}, "float")
    coord, el = _one(hass, item, make_device("d", "T", "thermostat"))
    el["scale"] = "fahrenheit"  # element carries scale for the entity
    climate = EzloClimate(coord, el)
    assert climate.current_temperature == 68.0
    assert climate.min_temp == 50.0


# ---- sensor battery ----

async def test_sensor_battery(hass: HomeAssistant) -> None:
    coord, el = _one(hass, make_item("b", "d", "battery", 8, "int"),
                     make_device("d", "Dev", ""))
    sensor = EzloSensor(coord, el)
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.icon == "mdi:battery-10"
    assert sensor.extra_state_attributes["battery_status"] == "very_low"


async def test_sensor_non_battery_icon_none(hass: HomeAssistant) -> None:
    coord, el = _one(hass, make_item("t", "d", "temp", 20, "temperature"),
                     make_device("d", "Dev", "sensor"))
    assert EzloSensor(coord, el).icon is None


@pytest.mark.parametrize(
    ("value", "icon"),
    [
        (97, "mdi:battery"),
        (88, "mdi:battery-90"),
        (52, "mdi:battery-50"),
        (0, "mdi:battery-unknown"),
        ("bad", "mdi:battery-unknown"),
    ],
)
async def test_sensor_battery_icon_levels(
    hass: HomeAssistant, value: Any, icon: str
) -> None:
    coord, el = _one(hass, make_item("b", "d", "battery", value, "int"),
                     make_device("d", "Dev", ""))
    assert EzloSensor(coord, el).icon == icon


# ---- binary sensor ----

@pytest.mark.parametrize(
    ("value", "vtype", "on"),
    [("open", "token", True), (1, "int", True), (None, "token", False)],
)
async def test_binary_value_parsing(
    hass: HomeAssistant, value: Any, vtype: str, on: bool
) -> None:
    coord, el = _one(hass, make_item("i", "d", "motion", value, vtype),
                     make_device("d", "M", "sensor.motion"))
    assert EzloBinarySensor(coord, el).is_on is on


# ---- entity helpers ----

async def test_command_failure_raises_translated(hass: HomeAssistant) -> None:
    coord, el = _one(hass, make_item("i", "d", "dimmer", 0, "int"),
                     make_device("d", "Dev", "dimmer.inwall"))

    async def _boom(*args: Any) -> None:
        raise RuntimeError("nope")

    coord.connection.async_set_item_value = _boom  # type: ignore[assignment]
    with pytest.raises(HomeAssistantError):
        await EzloLight(coord, el, None).async_turn_off()


async def test_device_info_falls_back_to_item(hass: HomeAssistant) -> None:
    # An item with no parent device falls back to a device keyed by the item id.
    item = {"_id": "i", "deviceId": None, "name": "dimmer", "value": 1,
            "valueType": "int", "hasSetter": True, "show": True}
    coord = make_coordinator(hass, [item], [])
    info = EzloLight(coord, coord.data["i"], None).device_info
    from custom_components.ezlopi.const import DOMAIN
    assert info["identifiers"] == {(DOMAIN, f"{coord.serial}_i")}
    assert info["name"] == "dimmer"


# ---- device_types ----

@pytest.mark.parametrize(
    ("info", "platform"),
    [
        ({"valueType": "bool"}, "binary_sensor"),
        ({"valueType": "int", "hasSetter": True}, "light"),
        ({"valueType": "float"}, "sensor"),
        ({"valueType": "temperature"}, "sensor"),
    ],
)
def test_detect_by_value_type(info: dict, platform: str) -> None:
    assert detect_by_value_type(info)["platform"] == platform


@pytest.mark.parametrize(
    ("level", "status"),
    [(90, "good"), (60, "fair"), (30, "low"), (15, "critical"),
     (5, "very_low"), (None, "unknown"), ("x", "unknown")],
)
def test_battery_status_thresholds(level: Any, status: str) -> None:
    assert EzloEntity._get_battery_status(level) == status


async def test_legacy_accessors(hass: HomeAssistant) -> None:
    coord, el = _one(hass, make_item("i", "d", "dimmer", 1, "int"),
                     make_device("d", "Dev", "dimmer.inwall"))
    light = EzloLight(coord, el, None)
    assert light.get_device_name() == "dimmer"
    assert light.get_device_id() == "i"


def test_additional_entities_and_multiple() -> None:
    assert get_additional_entities("doorlock")  # has additional entities
    assert get_additional_entities("unknown") == []
    assert should_create_multiple_entities({"deviceType": "doorlock"}) is True
    assert should_create_multiple_entities({"deviceType": "sensor"}) is False


async def test_dynamic_platform_adds_new_items(hass: HomeAssistant) -> None:
    from custom_components.ezlopi.entity import async_setup_ezlo_platform

    coord = make_coordinator(
        hass,
        [make_item("s1", "d", "temp", 10, "float")],
        [make_device("d", "Dev", "sensor")],
    )
    added: list[Any] = []
    async_setup_ezlo_platform(coord, "sensor", EzloSensor, lambda e: added.extend(e))
    assert len(added) == 1

    # A new sensor item appears -> the listener adds it; existing one is not re-added.
    coord.connection.items.append(make_item("s2", "d2", "hum", 20, "float"))
    coord.connection.devices.append(make_device("d2", "Dev2", "sensor"))
    coord.async_set_updated_data(coord._build_data())
    assert len(added) == 2
