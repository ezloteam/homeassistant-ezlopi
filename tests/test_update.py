"""Tests for the firmware manifest coordinator and the update entity."""
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant

from custom_components.ezlopi.const import DOMAIN
from custom_components.ezlopi.firmware import EzloFirmwareCoordinator
from custom_components.ezlopi.update import EzloFirmwareUpdate

from .fixtures import SERIAL, make_coordinator, make_device, make_item


class _Resp:
    def __init__(self, status: int, payload: Any = None, raise_client: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._raise = raise_client

    async def __aenter__(self) -> "_Resp":
        if self._raise:
            raise aiohttp.ClientError("boom")
        return self

    async def __aexit__(self, *args: Any) -> None:
        return

    async def json(self, **kwargs: Any) -> Any:
        return self._payload


class _Session:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def get(self, *args: Any, **kwargs: Any) -> _Resp:
        return self._resp


def _coord(hass: HomeAssistant, session: _Session) -> EzloFirmwareCoordinator:
    return EzloFirmwareCoordinator(hass, session)  # type: ignore[arg-type]


async def test_firmware_manifest_parses_version(hass: HomeAssistant) -> None:
    c = _coord(hass, _Session(_Resp(200, {"version": "5.7.15", "tag": "v5.7.15"})))
    assert await c._async_update_data() == "5.7.15"


async def test_firmware_manifest_http_error_keeps_last(hass: HomeAssistant) -> None:
    c = _coord(hass, _Session(_Resp(503)))
    c.data = "5.7.15"  # last known
    assert await c._async_update_data() == "5.7.15"


async def test_firmware_manifest_client_error_returns_last(hass: HomeAssistant) -> None:
    c = _coord(hass, _Session(_Resp(200, raise_client=True)))
    assert await c._async_update_data() is None  # never fetched, no prior value


async def test_firmware_manifest_missing_version_field(hass: HomeAssistant) -> None:
    c = _coord(hass, _Session(_Resp(200, {"tag": "v5.7.15"})))
    assert await c._async_update_data() is None


class _FakeFirmware:
    """Minimal stand-in for EzloFirmwareCoordinator in entity tests."""

    def __init__(self, data: str | None) -> None:
        self.data = data

    def async_add_listener(self, _cb: Any) -> Any:
        return lambda: None


def _light_coord(hass: HomeAssistant) -> Any:
    return make_coordinator(hass, [make_item("dm", "d", "dimmer", 0, "int")],
                            [make_device("d", "Dim", "")])


async def test_update_entity_reports_installed_and_latest(hass: HomeAssistant) -> None:
    coord = _light_coord(hass)
    entity = EzloFirmwareUpdate(coord, _FakeFirmware("5.7.15"))  # type: ignore[arg-type]
    assert entity.installed_version == "4.1.6"  # FakeConnection.firmware
    assert entity.latest_version == "5.7.15"
    assert entity.available is True
    assert entity.unique_id == f"{SERIAL}_firmware"
    assert (DOMAIN, f"{SERIAL}_controller") in entity.device_info["identifiers"]


async def test_update_entity_latest_falls_back_to_installed(hass: HomeAssistant) -> None:
    coord = _light_coord(hass)
    entity = EzloFirmwareUpdate(coord, _FakeFirmware(None))  # type: ignore[arg-type]
    # Manifest unavailable -> no spurious update offered.
    assert entity.latest_version == entity.installed_version == "4.1.6"


async def test_update_entity_unavailable_when_disconnected(hass: HomeAssistant) -> None:
    coord = _light_coord(hass)
    coord.connection.connected = False
    entity = EzloFirmwareUpdate(coord, _FakeFirmware("5.7.15"))  # type: ignore[arg-type]
    assert entity.available is False


async def test_update_entity_install_triggers_ota(hass: HomeAssistant) -> None:
    coord = _light_coord(hass)
    entity = EzloFirmwareUpdate(coord, _FakeFirmware("5.7.15"))  # type: ignore[arg-type]
    await entity.async_install(version=None, backup=False)
    assert ("firmware_update", None) in coord.connection.sent
