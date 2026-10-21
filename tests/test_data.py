"""Data-path tests: detection, item processing, coordinator and entity values."""
from typing import Any

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlopi import EzloRuntimeData
from custom_components.ezlopi.binary_sensor import EzloBinarySensor
from custom_components.ezlopi.climate import EzloClimate
from custom_components.ezlopi.const import DOMAIN
from custom_components.ezlopi.coordinator import EzloDataUpdateCoordinator
from custom_components.ezlopi.device_types import detect_device_platform
from custom_components.ezlopi.light import EzloLight
from custom_components.ezlopi.lock import EzloLock
from custom_components.ezlopi.sensor import EzloSensor
from custom_components.ezlopi.switch import EzloSwitch
from custom_components.ezlopi.utils import get_device_data, getDeviceName, getItemsData


class FakeConnection:
    """Stand-in for EzloHubConnection that exposes static items/devices."""

    def __init__(self, items: list[dict], devices: list[dict]) -> None:
        self.items = items
        self.devices = devices
        self.serial = "105203280"
        self.connected = True
        self.firmware = "4.1.6"
        self.hub_name = "Test ezloPi"
        self._on_update = lambda: None
        self.sent: list[tuple[str, Any]] = []

    async def async_set_item_value(self, item_id: str, value: Any) -> None:
        self.sent.append((item_id, value))


def _item(_id: str, device_id: str, name: str, value: Any, vtype: str) -> dict:
    return {
        "_id": _id, "deviceId": device_id, "name": name, "value": value,
        "valueType": vtype, "hasSetter": True, "show": True,
    }


def _device(_id: str, name: str, type_id: str) -> dict:
    return {"_id": _id, "name": name, "deviceTypeId": type_id,
            "category": None, "subcategory": None, "armed": False}


ITEMS = [
    _item("i_light", "d_light", "dimmer", 50, "int"),
    _item("i_switch", "d_switch", "relay", True, "bool"),
    _item("i_sensor", "d_sensor", "temp", 21.5, "float"),
    _item("i_binary", "d_binary", "motion", True, "bool"),
    _item("i_lock", "d_lock", "bolt", "locked", "token"),
    _item("i_climate", "d_climate", "setpoint", 20.0, "float"),
]
DEVICES = [
    _device("d_light", "Hall Dimmer", "dimmer.inwall"),
    _device("d_switch", "Plug", "switch.inwall"),
    _device("d_sensor", "Room Temp", "sensor"),
    _device("d_binary", "Hall Motion", "sensor.motion"),
    _device("d_lock", "Front Door", "doorlock"),
    _device("d_climate", "Thermostat", "thermostat"),
]


# ---- pure detection ----

@pytest.mark.parametrize(
    ("info", "platform"),
    [
        ({"deviceType": "dimmer.inwall"}, "light"),
        ({"deviceType": "doorlock"}, "lock"),
        ({"category": "security", "subcategory": "motion"}, "binary_sensor"),
        ({"category": "temperature"}, "sensor"),
        ({"name": "dimmer"}, "light"),
        ({"name": "mystery"}, "sensor"),  # fallback
    ],
)
def test_detect_device_platform(info: dict, platform: str) -> None:
    assert detect_device_platform(info)["platform"] == platform


def test_detect_tolerates_none_device_name() -> None:
    # deviceName present but None must not crash.
    assert detect_device_platform({"name": "x", "deviceName": None})["platform"]


# ---- utils ----

def test_get_items_data_skips_value_less_items() -> None:
    # Control-only items (e.g. a dimmer's dimmer_up/down/stop) have show:true but
    # no "value" key. They must be skipped, not crash the whole data build.
    items = [
        _item("dm", "d", "dimmer", 19, "int"),
        {"_id": "up", "deviceId": "d", "name": "dimmer_up", "valueType": "int",
         "hasSetter": True, "show": True},  # no "value" key
        _item("sw", "d", "switch", False, "bool"),
    ]
    elements = getItemsData(FakeConnection(items, [_device("d", "Strip", "dimmer.outlet")]))
    names = {e["name"] for e in elements}
    assert names == {"dimmer", "switch"}  # dimmer_up skipped, no KeyError


def test_get_items_data_and_helpers() -> None:
    conn = FakeConnection(ITEMS, DEVICES)
    elements = getItemsData(conn)
    by_id = {e["id"]: e for e in elements}
    assert by_id["i_light"]["type"] == "light"
    assert by_id["i_switch"]["type"] == "switch"
    assert by_id["i_lock"]["type"] == "lock"
    assert by_id["i_climate"]["type"] == "climate"
    # switch bool value is normalised to on/off
    assert by_id["i_switch"]["value"] == "on"
    assert getDeviceName("d_light", conn) == "Hall Dimmer"
    assert getDeviceName("missing", conn) is None


async def test_get_device_data() -> None:
    conn = FakeConnection(ITEMS, DEVICES)
    assert await get_device_data("i_sensor", conn) == 21.5


# ---- coordinator + entities ----

def _coordinator(hass: HomeAssistant) -> EzloDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="user")
    entry.add_to_hass(hass)
    conn = FakeConnection(ITEMS, DEVICES)
    coord = EzloDataUpdateCoordinator(hass, entry, conn)  # type: ignore[arg-type]
    coord.data = coord._build_data()
    return coord


async def test_coordinator_build_data(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    assert set(coord.data) == {e["_id"] for e in ITEMS}


async def test_light_entity(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    light = EzloLight(coord, coord.data["i_light"], None)
    assert light.is_on is True
    assert light.brightness == round(50 * 255 / 100)
    assert light.available is True
    # device_info groups every entity under one device per controller.
    assert (DOMAIN, "105203280") in light.device_info["identifiers"]
    await light.async_turn_off()
    assert coord.connection.sent[-1] == ("i_light", 0)


async def test_switch_entity(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    switch = EzloSwitch(coord, coord.data["i_switch"])
    assert switch.is_on is True
    await switch.async_turn_off()
    assert coord.connection.sent[-1] == ("i_switch", False)


async def test_sensor_entity(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    sensor = EzloSensor(coord, coord.data["i_sensor"])
    assert sensor.native_value == 21.5
    assert sensor.extra_state_attributes["sensorId"] == "i_sensor"


async def test_binary_sensor_entity(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    binary = EzloBinarySensor(coord, coord.data["i_binary"])
    assert binary.is_on is True


async def test_lock_entity(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    lock = EzloLock(coord, coord.data["i_lock"])
    assert lock.is_locked is True
    await lock.async_unlock()
    assert coord.connection.sent[-1] == ("i_lock", False)


async def test_climate_entity(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    climate = EzloClimate(coord, coord.data["i_climate"])
    assert climate.current_temperature == 20.0
    climate.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    await climate.async_set_hvac_mode(HVACMode.HEAT)
    assert climate.hvac_mode == HVACMode.HEAT
    assert coord.connection.sent[-1] == ("i_climate", "heat")


async def test_unavailable_when_disconnected(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    light = EzloLight(coord, coord.data["i_light"], None)
    coord.connection.connected = False
    assert light.available is False
