"""Tests for the ezloPi custom services (canvas framebuffer writes)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol
from PIL import Image

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.ezlopi.const import DOMAIN
from custom_components.ezlopi.coordinator import EzloDataUpdateCoordinator
from custom_components.ezlopi import services
from custom_components.ezlopi.services import (
    _canvas_target,
    _encode_image,
    _geometry,
    _valid_hex,
    async_register_services,
)

from .fixtures import SERIAL, FakeConnection, make_device, make_item


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #
def test_valid_hex_normalises_and_strips_spaces() -> None:
    assert _valid_hex(" FF00ff 00FF00 ") == "ff00ff00ff00"


@pytest.mark.parametrize(
    "bad",
    [
        "", "abc", "ff00", "gggggg", "ff00f",
        "0x1234",  # a 0x prefix must not pass as hex
        "12_f00",  # underscores must not pass
        "-fffff",  # a sign must not pass
    ],
)
def test_valid_hex_rejects_bad_payloads(bad: str) -> None:
    with pytest.raises(vol.Invalid):
        _valid_hex(bad)


def test_geometry_from_dict() -> None:
    assert _geometry({"value": {"width": 42, "height": 2, "pixels": 84}}) == (42, 2)


def test_geometry_from_repr_string() -> None:
    assert _geometry({"value": "{'width':42,'height':1,'pixels':42}"}) == (42, 1)


def test_geometry_pixels_only_is_single_row() -> None:
    # A plain strip reports width 0; fall back to pixels as one row.
    assert _geometry({"value": {"width": 0, "height": 0, "pixels": 30}}) == (30, 1)


@pytest.mark.parametrize("value", [None, "not a dict", "{broken", 123, {}])
def test_geometry_unknown_returns_zero(value: Any) -> None:
    assert _geometry({"value": value}) == (0, 0)


def test_encode_image_row_major_rgb(tmp_path: Any) -> None:
    img = Image.new("RGB", (2, 1))
    img.putpixel((0, 0), (255, 0, 0))
    img.putpixel((1, 0), (0, 0, 255))
    path = str(tmp_path / "two.png")
    img.save(path)
    assert _encode_image(path, 2, 1) == "ff00000000ff"


def test_encode_image_resizes_to_geometry(tmp_path: Any) -> None:
    path = str(tmp_path / "white.png")
    Image.new("RGB", (1, 1), (255, 255, 255)).save(path)
    assert _encode_image(path, 3, 1) == "ffffff" * 3


# --------------------------------------------------------------------------- #
# Canvas target resolution                                                     #
# --------------------------------------------------------------------------- #
def _coord(serial: str, items: list[dict], connection: Any = None) -> Any:
    data = {it["id"]: it for it in items}
    return SimpleNamespace(
        serial=serial, data=data, connection=connection or object()
    )


def _elem(_id: str, name: str, device_id: str, value: Any = None) -> dict:
    return {"id": _id, "name": name, "deviceId": device_id, "value": value}


def test_canvas_target_resolves_via_sibling_light() -> None:
    conn = object()
    coord = _coord(
        SERIAL,
        [
            _elem("rgb1", "rgbcolor", "d_strip"),
            _elem("cv1", "canvas", "d_strip", "{'width':42,'height':1,'pixels':42}"),
        ],
        conn,
    )
    # Target the light entity; the canvas on the same device is chosen.
    result = _canvas_target([coord], f"{SERIAL}_rgb1")
    assert result == (conn, "cv1", (42, 1))


def test_canvas_target_resolves_when_targeting_canvas_itself() -> None:
    coord = _coord(SERIAL, [_elem("cv1", "canvas", "d_strip", {"pixels": 10})])
    _, item_id, geom = _canvas_target([coord], f"{SERIAL}_cv1")
    assert item_id == "cv1"
    assert geom == (10, 1)


def test_canvas_target_prefers_same_device_canvas() -> None:
    coord = _coord(
        SERIAL,
        [
            _elem("rgb1", "rgbcolor", "d_strip_a"),
            _elem("cvA", "canvas", "d_strip_a", {"pixels": 5}),
            _elem("cvB", "canvas", "d_strip_b", {"pixels": 9}),
        ],
    )
    _, item_id, _geom = _canvas_target([coord], f"{SERIAL}_rgb1")
    assert item_id == "cvA"


def test_canvas_target_ambiguous_raises() -> None:
    # Target item's device has no canvas; two canvases elsewhere are ambiguous.
    coord = _coord(
        SERIAL,
        [
            _elem("x1", "relay", "d_other"),
            _elem("cvA", "canvas", "d_strip_a", {"pixels": 5}),
            _elem("cvB", "canvas", "d_strip_b", {"pixels": 9}),
        ],
    )
    with pytest.raises(HomeAssistantError, match="multiple"):
        _canvas_target([coord], f"{SERIAL}_x1")


def test_canvas_target_no_canvas_raises() -> None:
    coord = _coord(SERIAL, [_elem("x1", "relay", "d_other")])
    with pytest.raises(HomeAssistantError, match="no 'canvas'"):
        _canvas_target([coord], f"{SERIAL}_x1")


def test_canvas_target_unknown_entity_returns_none() -> None:
    coord = _coord(SERIAL, [_elem("cv1", "canvas", "d_strip", {"pixels": 1})])
    assert _canvas_target([coord], "999_nope") is None


# --------------------------------------------------------------------------- #
# End-to-end service calls                                                     #
# --------------------------------------------------------------------------- #
_STRIP_ITEMS = [
    make_item("rgb1", "d_strip", "rgbcolor", {"red": 1, "green": 2, "blue": 3}, "rgb"),
    make_item(
        "cv1", "d_strip", "canvas", "{'width':4,'height':1,'pixels':4}", "string"
    ),
]
_STRIP_DEVICES = [make_device("d_strip", "RGBCCT LED strip", "device-0009")]


def _setup_entry_with_strip(hass: Any) -> tuple[Any, FakeConnection]:
    """Build a loaded config entry whose coordinator has a canvas item."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="user")
    entry.add_to_hass(hass)
    conn = FakeConnection()
    conn.items = list(_STRIP_ITEMS)
    conn.devices = list(_STRIP_DEVICES)
    coord = EzloDataUpdateCoordinator(hass, entry, conn)
    coord.data = coord._build_data()
    entry.runtime_data = SimpleNamespace(coordinators=[coord])

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{SERIAL}_cv1",
        config_entry=entry, suggested_object_id="strip_canvas",
    )
    return entry, conn


async def test_set_canvas_sends_raw_fire_and_forget(hass: Any) -> None:
    _entry, conn = _setup_entry_with_strip(hass)
    async_register_services(hass)

    await hass.services.async_call(
        DOMAIN,
        "set_canvas",
        {"entity_id": "sensor.strip_canvas", "start": 2, "rgb": "ff0000 00ff00"},
        blocking=True,
    )
    # Raw send (no optimistic update), spaces stripped, start preserved.
    assert conn.raw_sent == [("cv1", {"start": 2, "rgb": "ff000000ff00"})]
    assert conn.sent == []  # never routed through the optimistic/coalescing path


async def test_set_canvas_rejects_run_overrunning_the_strip(hass: Any) -> None:
    _entry, conn = _setup_entry_with_strip(hass)  # 4-pixel strip
    async_register_services(hass)
    with pytest.raises(HomeAssistantError, match="exceed"):
        await hass.services.async_call(
            DOMAIN,
            "set_canvas",
            # start 2 + 4 pixels = 6 > 4
            {"entity_id": "sensor.strip_canvas", "start": 2,
             "rgb": "ff0000 ff0000 ff0000 ff0000"},
            blocking=True,
        )
    assert conn.raw_sent == []


async def test_set_canvas_dedupes_light_and_canvas_targets(hass: Any) -> None:
    entry, conn = _setup_entry_with_strip(hass)
    # Register the strip's light entity too; it shares the canvas's device.
    er.async_get(hass).async_get_or_create(
        "light", DOMAIN, f"{SERIAL}_rgb1",
        config_entry=entry, suggested_object_id="rgbcct_led_strip",
    )
    async_register_services(hass)
    await hass.services.async_call(
        DOMAIN,
        "set_canvas",
        {
            "entity_id": ["light.rgbcct_led_strip", "sensor.strip_canvas"],
            "rgb": "ff0000",
        },
        blocking=True,
    )
    # Both targets resolve to the same canvas item -> a single write.
    assert conn.raw_sent == [("cv1", {"start": 0, "rgb": "ff0000"})]


async def test_set_canvas_rejects_bad_hex(hass: Any) -> None:
    _setup_entry_with_strip(hass)
    async_register_services(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "set_canvas",
            {"entity_id": "sensor.strip_canvas", "rgb": "xyz"},
            blocking=True,
        )


async def test_draw_image_encodes_and_blits(hass: Any, tmp_path: Any) -> None:
    _entry, conn = _setup_entry_with_strip(hass)
    async_register_services(hass)
    path = str(tmp_path / "pattern.png")
    Image.new("RGB", (4, 1), (0, 0, 0)).save(path)
    hass.config.allowlist_external_dirs = {str(tmp_path)}

    await hass.services.async_call(
        DOMAIN,
        "draw_image",
        {"entity_id": "sensor.strip_canvas", "image": path},
        blocking=True,
    )
    assert len(conn.raw_sent) == 1
    item_id, payload = conn.raw_sent[0]
    assert item_id == "cv1"
    assert payload["start"] == 0
    assert payload["rgb"] == "000000" * 4  # 4 pixels, row-major


async def test_draw_image_chunks_large_strips(hass: Any, tmp_path: Any) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="big")
    entry.add_to_hass(hass)
    conn = FakeConnection()
    conn.items = [
        make_item(
            "cvBig", "d_big", "canvas",
            "{'width':300,'height':1,'pixels':300}", "string",
        )
    ]
    conn.devices = [make_device("d_big", "Big strip", "device-0009")]
    coord = EzloDataUpdateCoordinator(hass, entry, conn)
    coord.data = coord._build_data()
    entry.runtime_data = SimpleNamespace(coordinators=[coord])
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{SERIAL}_cvBig",
        config_entry=entry, suggested_object_id="big_canvas",
    )
    async_register_services(hass)

    path = str(tmp_path / "big.png")
    Image.new("RGB", (300, 1), (1, 2, 3)).save(path)
    hass.config.allowlist_external_dirs = {str(tmp_path)}

    await hass.services.async_call(
        DOMAIN, "draw_image",
        {"entity_id": "sensor.big_canvas", "image": path}, blocking=True,
    )
    # 300 px, 256 per chunk -> two writes at start 0 and 256.
    assert [p["start"] for _i, p in conn.raw_sent] == [0, 256]
    assert len(conn.raw_sent[0][1]["rgb"]) == 256 * 6
    assert len(conn.raw_sent[1][1]["rgb"]) == 44 * 6


async def test_draw_image_rejects_disallowed_path(hass: Any, tmp_path: Any) -> None:
    _setup_entry_with_strip(hass)
    async_register_services(hass)
    path = str(tmp_path / "blocked.png")
    Image.new("RGB", (4, 1)).save(path)
    hass.config.allowlist_external_dirs = set()  # not allowed
    with pytest.raises(HomeAssistantError, match="allowed path"):
        await hass.services.async_call(
            DOMAIN, "draw_image",
            {"entity_id": "sensor.strip_canvas", "image": path}, blocking=True,
        )


async def test_services_registered_and_removed(hass: Any) -> None:
    _setup_entry_with_strip(hass)
    async_register_services(hass)
    assert hass.services.has_service(DOMAIN, "set_canvas")
    assert hass.services.has_service(DOMAIN, "draw_image")
    services.async_unregister_services(hass)
    assert not hass.services.has_service(DOMAIN, "set_canvas")
    assert not hass.services.has_service(DOMAIN, "draw_image")
