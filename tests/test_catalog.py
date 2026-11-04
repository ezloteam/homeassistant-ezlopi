"""Tests for the server-side firmware-catalog decision client."""
from types import SimpleNamespace
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant

from custom_components.ezlopi.catalog import (
    EzloFirmwareDecisionCoordinator,
    FirmwareDecision,
    parse_decision,
)


# ---- parse_decision ----

def test_parse_decision_false() -> None:
    d = parse_decision({"update_available": False, "reason": "unknown_product"})
    assert d == FirmwareDecision(False, None, None, None, "unknown_product")


def test_parse_decision_true_full() -> None:
    d = parse_decision({
        "update_available": True, "target_version": "5.7.22", "reason": "channel",
        "file": {"url": "http://x/f.bin", "md5": "m", "sig": "s", "size": 10},
    })
    assert d is not None and d.update_available
    assert d.target_version == "5.7.22"
    assert d.file_url == "http://x/f.bin" and d.file_md5 == "m"


def test_parse_decision_version_from_file() -> None:
    # target_version may be carried on the file object instead of top-level.
    d = parse_decision({
        "update_available": True,
        "file": {"url": "http://x/f.bin", "version": "5.7.22"},
    })
    assert d is not None and d.update_available and d.target_version == "5.7.22"


def test_parse_decision_available_but_no_url_is_no_offer() -> None:
    d = parse_decision({"update_available": True, "target_version": "5.7.22", "file": {}})
    assert d is not None and d.update_available is False


def test_parse_decision_non_dict() -> None:
    assert parse_decision([]) is None
    assert parse_decision(None) is None


# ---- coordinator ----

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
        self.calls: list[dict[str, Any]] = []

    def get(self, *args: Any, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return self._resp


def _conn(firmware: str | None = "5.7.19", model: str | None = "ezlopi_generic",
          chip: str | None = "esp32") -> Any:
    return SimpleNamespace(firmware=firmware, model=model, chip=chip, serial="105203280")


def _coord(hass: HomeAssistant, session: _Session, conn: Any, active: bool = True) -> Any:
    return EzloFirmwareDecisionCoordinator(
        hass, session, conn, lambda: active  # type: ignore[arg-type]
    )


async def test_decision_fetch_success_sends_product_params(hass: HomeAssistant) -> None:
    payload = {"update_available": True, "target_version": "5.7.22",
               "file": {"url": "http://x/f.bin", "md5": "m"}}
    session = _Session(_Resp(200, payload))
    c = _coord(hass, session, _conn())
    d = await c._async_update_data()
    assert d is not None and d.update_available and d.target_version == "5.7.22"
    # model / hardware / version all forwarded to the catalog.
    params = session.calls[0]["params"]
    assert params == {"version": "5.7.19", "model": "ezlopi_generic", "hardware": "esp32"}


async def test_decision_inactive_keeps_last_without_fetch(hass: HomeAssistant) -> None:
    session = _Session(_Resp(200, {"update_available": True}))
    c = _coord(hass, session, _conn(), active=False)
    prev = FirmwareDecision(False, None, None, None, "prev")
    c.data = prev
    assert await c._async_update_data() is prev
    assert session.calls == []  # flag OFF -> no network at all


async def test_decision_no_version_keeps_last(hass: HomeAssistant) -> None:
    session = _Session(_Resp(200, {"update_available": True}))
    c = _coord(hass, session, _conn(firmware=None))
    c.data = None
    assert await c._async_update_data() is None
    assert session.calls == []  # never query without a version


async def test_decision_http_error_keeps_last(hass: HomeAssistant) -> None:
    c = _coord(hass, _Session(_Resp(503)), _conn())
    prev = FirmwareDecision(False, None, None, None, "prev")
    c.data = prev
    assert await c._async_update_data() is prev


async def test_decision_client_error_keeps_last(hass: HomeAssistant) -> None:
    c = _coord(hass, _Session(_Resp(200, raise_client=True)), _conn())
    c.data = None
    assert await c._async_update_data() is None
