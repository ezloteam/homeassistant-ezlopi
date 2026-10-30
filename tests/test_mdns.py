"""Tests for the mDNS connector."""
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.ezlopi.mdns_connector import EzloPiMDSConnector


class _Info:
    type = "_ezlo._tcp.local."
    name = "EzloPi._ezlo._tcp.local."
    server = "ezlopi_3280.local."
    port = 17001
    interface_index = 14
    properties = {b"Serial": b"105203280", b"empty": None}

    def parsed_addresses(self) -> list[str]:
        return ["10.0.0.5"]


class _Zc:
    def get_service_info(self, type_: str, name: str) -> _Info | None:
        return _Info()


def _connector_with_service() -> EzloPiMDSConnector:
    c = EzloPiMDSConnector()
    c.add_service(_Zc(), _Info.type, _Info.name)  # type: ignore[arg-type]
    return c


def test_add_service_and_link_lookup() -> None:
    c = _connector_with_service()
    assert c.get_connection_link_from_serial("105203280") == "ws://10.0.0.5:17001"
    assert c.get_connection_links() == {"105203280": "ws://10.0.0.5:17001"}


def test_link_lookup_unknown_serial_returns_none() -> None:
    assert _connector_with_service().get_connection_link_from_serial("nope") is None


def test_link_lookup_requires_serial() -> None:
    with pytest.raises(ValueError):
        EzloPiMDSConnector().get_connection_link_from_serial(None)


def test_get_serial_missing_raises() -> None:
    with pytest.raises(KeyError):
        EzloPiMDSConnector().get_serial({"properties": {}})


def test_resolver_skips_serial_less_service() -> None:
    # A hub mid-reboot can advertise an incomplete record (no Serial TXT). It
    # must not abort resolution for every other hub (that would kill the
    # connection supervisor, which calls this on its loop).
    c = EzloPiMDSConnector()
    c.service_dict = {
        "bad._ezlo._tcp.local.": {"properties": {}, "address": ["10.0.0.9"], "port": 17001},
        "good._ezlo._tcp.local.": {
            "properties": {"Serial": "105203837"}, "address": ["10.0.8.60"], "port": 17001},
    }
    assert c.get_connection_link_from_serial("105203837") == "ws://10.0.8.60:17001"
    assert c.get_connection_links() == {"105203837": "ws://10.0.8.60:17001"}


def test_resolver_skips_addressless_service() -> None:
    # A record with no resolved address yet must be skipped, not raise IndexError.
    c = EzloPiMDSConnector()
    c.service_dict = {
        "noaddr": {"properties": {"Serial": "105203837"}, "address": [], "port": 17001},
    }
    assert c.get_connection_link_from_serial("105203837") is None
    assert c.get_connection_links() == {}


def test_add_service_handles_missing_info() -> None:
    class _NoInfo:
        def get_service_info(self, *a: Any) -> None:
            return None

    c = EzloPiMDSConnector()
    c.add_service(_NoInfo(), "t", "n")  # type: ignore[arg-type]
    assert c.service_dict == {}


def test_update_service_stores_like_add() -> None:
    # A hub that only re-announces (update_service) must still be recorded.
    c = EzloPiMDSConnector()
    c.update_service(_Zc(), _Info.type, _Info.name)  # type: ignore[arg-type]
    assert c.get_connection_link_from_serial("105203280") == "ws://10.0.0.5:17001"


def test_remove_service_removes() -> None:
    c = _connector_with_service()
    assert c.service_dict  # present
    c.remove_service(_Zc(), _Info.type, _Info.name)  # type: ignore[arg-type]
    assert c.service_dict == {}


async def test_async_init_and_discovery(hass: Any) -> None:
    c = EzloPiMDSConnector()
    with (
        patch("custom_components.ezlopi.mdns_connector.async_get_instance",
              return_value=_Zc()),
        patch("custom_components.ezlopi.mdns_connector.ServiceBrowser"),
        patch("custom_components.ezlopi.mdns_connector.asyncio.sleep"),
    ):
        await c.async_init(hass)
        await c.async_get_service_info()
    assert c.zc is not None
