"""Shared fakes and sample hub data for ezloPi tests."""
from __future__ import annotations

from typing import Any

from custom_components.ezlopi.ezlopi_utils import EzloPIHubInfo

SERIAL = "105203280"


def make_item(_id: str, device_id: str, name: str, value: Any, vtype: str) -> dict:
    return {
        "_id": _id, "deviceId": device_id, "name": name, "value": value,
        "valueType": vtype, "hasSetter": True, "show": True,
    }


def make_device(_id: str, name: str, type_id: str) -> dict:
    return {"_id": _id, "name": name, "deviceTypeId": type_id,
            "category": None, "subcategory": None, "armed": False}


ITEMS = [
    make_item("i_light", "d_light", "dimmer", 50, "int"),
    make_item("i_switch", "d_switch", "relay", True, "bool"),
    make_item("i_sensor", "d_sensor", "temp", 21.5, "float"),
    make_item("i_binary", "d_binary", "motion", True, "bool"),
    make_item("i_lock", "d_lock", "bolt", "locked", "token"),
    make_item("i_climate", "d_climate", "setpoint", 20.0, "float"),
]
DEVICES = [
    make_device("d_light", "Hall Dimmer", "dimmer.inwall"),
    make_device("d_switch", "Plug", "switch.inwall"),
    make_device("d_sensor", "Room Temp", "sensor"),
    make_device("d_binary", "Hall Motion", "sensor.motion"),
    make_device("d_lock", "Front Door", "doorlock"),
    make_device("d_climate", "Thermostat", "thermostat"),
]


class FakeConnection:
    """Stand-in for EzloHubConnection exposing static items/devices."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.serial = SERIAL
        self.connected = True
        self.items = list(ITEMS)
        self.devices = list(DEVICES)
        self.firmware = "4.1.6"
        self._on_update = kwargs.get("on_update", lambda: None)
        self.sent: list[tuple[str, Any]] = []

    def start(self) -> None:
        return

    async def async_wait_ready(self) -> None:
        return

    async def async_stop(self) -> None:
        return

    async def async_set_item_value(self, item_id: str, value: Any) -> None:
        self.sent.append((item_id, value))

    async def async_start_firmware_update(self) -> None:
        self.sent.append(("firmware_update", None))


def make_coordinator(hass: Any, items: list[dict], devices: list[dict]) -> Any:
    """Build a coordinator backed by a FakeConnection with the given data."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ezlopi.const import DOMAIN
    from custom_components.ezlopi.coordinator import EzloDataUpdateCoordinator

    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="user")
    entry.add_to_hass(hass)
    conn = FakeConnection()
    conn.items = items
    conn.devices = devices
    coord = EzloDataUpdateCoordinator(hass, entry, conn)
    coord.data = coord._build_data()
    return coord


class FakeBrowser:
    """Stand-in for the mDNS browser."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        return

    async def async_init(self, hass: Any) -> None:
        return

    async def async_get_service_info(self) -> None:
        return

    def get_connection_link_from_serial(self, serial: str | None = None) -> str:
        return "ws://10.0.0.5:17001"


class FakeCloudAPI:
    """Stand-in for EzloCloudAPI returning one hub."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.token = "jwt"
        self.legacy_auth = "A"
        self.legacy_sig = "S"
        self._hubs = {
            SERIAL: EzloPIHubInfo(
                SERIAL, "local-key", "Hub",
                nma_url="wss://nma-ui-cloud.ezlo.com/nma",
            )
        }

    async def fetch_hub_list(self) -> bool:
        return True

    def get_hub_list(self) -> dict[str, EzloPIHubInfo]:
        return self._hubs
