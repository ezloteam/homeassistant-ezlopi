"""Tests for the firmware manifest coordinator, variant selection, update entity."""
from typing import Any

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.ezlopi.const import DOMAIN
from custom_components.ezlopi.firmware import (
    EzloFirmwareCoordinator,
    FirmwareManifest,
    FirmwareVariant,
    select_variant,
    version_compare,
)
from custom_components.ezlopi.update import EzloFirmwareUpdate

from .fixtures import SERIAL, make_coordinator, make_device, make_item

# A manifest shaped like the real dl.mios.com/ezloPI/manifest.json.
_MANIFEST = FirmwareManifest(
    version="5.7.15",
    tag="v5.7.15",
    variants=(
        FirmwareVariant("dimmer", "esp32", "d.bin", "http://x/dimmer.bin", "m1"),
        FirmwareVariant("esp32", "esp32", "e.bin", "http://x/esp32.bin", "m2"),
        FirmwareVariant("switch", "esp32", "s.bin", "http://x/switch.bin", "m3"),
        FirmwareVariant("esp32s3_4mb", "esp32s3", "s3.bin", "http://x/s3.bin", "m4"),
    ),
)


# ---- version comparison ----

@pytest.mark.parametrize(("a", "b", "expected"), [
    ("4.1.6", "5.7.15", -1),
    ("5.7.15", "4.1.6", 1),
    ("5.6.12", "5.6.12", 0),
    ("5.7.7.119", "5.6.0", 1),   # 4-part vs 3-part
    ("5.6.0", "5.6.0", 0),
    ("5.10.0", "5.9.0", 1),      # numeric, not lexical
])
def test_version_compare(a: str, b: str, expected: int) -> None:
    assert version_compare(a, b) == expected


# ---- variant selection (mirrors the Ezlo app getDeviceSuffix) ----

def test_variant_esp32_old_frankever_dimmer() -> None:
    v = select_variant(_MANIFEST, "esp32", "4.1.6", "ezlopi_frankever_us_wb01d-01b")
    assert v is not None and v.suffix == "dimmer"


def test_variant_esp32_old_frankever_switch() -> None:
    v = select_variant(_MANIFEST, "esp32", "4.1.6", "ezlopi_frankever_ssms118-x")
    assert v is not None and v.suffix == "switch"


def test_variant_esp32_new_is_universal() -> None:
    v = select_variant(_MANIFEST, "esp32", "5.6.12", "ezlopi_generic")
    assert v is not None and v.suffix == "esp32"


def test_variant_dedicated_chip_by_target() -> None:
    v = select_variant(_MANIFEST, "esp32s3", "5.6.12", "ezlopi_generic")
    assert v is not None and v.target == "esp32s3"


def test_variant_old_esp32_without_model_refuses() -> None:
    # Model is required to disambiguate esp32 < 5.6.0; unknown -> no guess.
    assert select_variant(_MANIFEST, "esp32", "4.1.6", None) is None


# ---- manifest coordinator parsing ----

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


async def test_manifest_parses_full(hass: HomeAssistant) -> None:
    payload = {"version": "5.7.15", "tag": "v5.7.15", "firmware": [
        {"suffix": "dimmer", "target": "esp32", "url": "http://x/d.bin", "md5": "m1"},
        {"suffix": "esp32", "target": "esp32", "url": "http://x/e.bin", "md5": "m2"},
    ]}
    m = await _coord(hass, _Session(_Resp(200, payload)))._async_update_data()
    assert m is not None and m.version == "5.7.15"
    assert m.by_suffix("dimmer").url == "http://x/d.bin"  # type: ignore[union-attr]


async def test_manifest_http_error_keeps_last(hass: HomeAssistant) -> None:
    c = _coord(hass, _Session(_Resp(503)))
    c.data = _MANIFEST
    assert await c._async_update_data() is _MANIFEST


async def test_manifest_client_error_returns_last(hass: HomeAssistant) -> None:
    assert await _coord(hass, _Session(_Resp(200, raise_client=True)))._async_update_data() is None


async def test_manifest_missing_version(hass: HomeAssistant) -> None:
    assert await _coord(hass, _Session(_Resp(200, {"firmware": []})))._async_update_data() is None


# ---- update entity ----

class _FakeFirmware:
    def __init__(self, data: FirmwareManifest | None) -> None:
        self.data = data

    def async_add_listener(self, _cb: Any) -> Any:
        return lambda: None


def _light_coord(hass: HomeAssistant) -> Any:
    return make_coordinator(hass, [make_item("dm", "d", "dimmer", 0, "int")],
                            [make_device("d", "Dim", "")])


async def test_update_entity_installed_and_latest(hass: HomeAssistant) -> None:
    entity = EzloFirmwareUpdate(_light_coord(hass), _FakeFirmware(_MANIFEST))  # type: ignore[arg-type]
    assert entity.installed_version == "4.1.6"
    assert entity.latest_version == "5.7.15"
    assert entity.available is True
    assert entity.unique_id == f"{SERIAL}_firmware"
    assert (DOMAIN, f"{SERIAL}_controller") in entity.device_info["identifiers"]


async def test_update_entity_latest_falls_back_when_no_manifest(hass: HomeAssistant) -> None:
    entity = EzloFirmwareUpdate(_light_coord(hass), _FakeFirmware(None))  # type: ignore[arg-type]
    assert entity.latest_version == entity.installed_version == "4.1.6"


async def test_update_entity_unavailable_when_disconnected(hass: HomeAssistant) -> None:
    coord = _light_coord(hass)
    coord.connection.connected = False
    entity = EzloFirmwareUpdate(coord, _FakeFirmware(_MANIFEST))  # type: ignore[arg-type]
    assert entity.available is False


async def test_update_entity_install_sends_correct_image(hass: HomeAssistant) -> None:
    coord = _light_coord(hass)  # FakeConnection: chip esp32, 4.1.6, frankever dimmer
    entity = EzloFirmwareUpdate(coord, _FakeFirmware(_MANIFEST))  # type: ignore[arg-type]
    await entity.async_install(version=None, backup=False)
    assert ("firmware_update", ("5.7.15", "http://x/dimmer.bin")) in coord.connection.sent


async def test_update_entity_install_without_manifest_raises(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError
    entity = EzloFirmwareUpdate(_light_coord(hass), _FakeFirmware(None))  # type: ignore[arg-type]
    with pytest.raises(HomeAssistantError):
        await entity.async_install(version=None, backup=False)
