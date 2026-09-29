"""Tests for the async hub websocket connection."""
import asyncio
import json
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.ezlopi import connection as conn_mod
from custom_components.ezlopi.connection import EzloHubConnection

from .fixtures import DEVICES, ITEMS


class _Msg:
    def __init__(self, type_: Any, data: str = "") -> None:
        self.type = type_
        self.data = data


class _WS:
    def __init__(self, messages: list[_Msg]) -> None:
        self._messages = messages
        self.sent: list[str] = []
        self.closed = False

    async def __aenter__(self) -> "_WS":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return

    def __aiter__(self) -> "_WS":
        return self

    async def __anext__(self) -> _Msg:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, ws: _WS) -> None:
        self._ws = ws

    def ws_connect(self, url: str, **kwargs: Any) -> _WS:
        return self._ws


class _Browser:
    def __init__(self, url: str | None) -> None:
        self._url = url

    def get_connection_link_from_serial(self, serial: str | None = None) -> str | None:
        return self._url


def _login_then_data() -> _WS:
    return _WS([
        _Msg(aiohttp.WSMsgType.TEXT, json.dumps({"method": "hub.offline.login.ui"})),
        _Msg(aiohttp.WSMsgType.TEXT,
             json.dumps({"id": "q", "result": {"items": ITEMS, "devices": DEVICES}})),
        _Msg(aiohttp.WSMsgType.CLOSED),
    ])


def _make(hass: HomeAssistant, ws: _WS, url: str | None = "ws://x:1") -> EzloHubConnection:
    return EzloHubConnection(
        hass, _Session(ws), _Browser(url), "105203280", "tok", on_update=lambda: None
    )


async def test_connect_login_and_receive(hass: HomeAssistant) -> None:
    ws = _login_then_data()
    c = _make(hass, ws)

    async def _noop_keepalive() -> None:
        return

    with patch.object(c, "_async_keepalive", _noop_keepalive):
        await c._async_connect_and_listen("ws://x:1")
    # login + the two list queries were sent
    assert any("hub.offline.login.ui" in s for s in ws.sent)
    assert len(ws.sent) >= 3
    assert c.items == ITEMS
    assert c.devices == DEVICES
    assert c._ready.is_set()


async def test_item_update_broadcast(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))
    c.items = [dict(i) for i in ITEMS]
    updates: list[int] = []
    c._on_update = lambda: updates.append(1)
    c._handle({"id": "ui_broadcast", "msg_subclass": "hub.item.updated",
               "result": {"_id": "i_light", "value": 99}})
    assert next(i for i in c.items if i["_id"] == "i_light")["value"] == 99
    assert updates  # listener notified


async def test_handle_ignores_bad_json_and_unknown_item(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))
    assert c._parse("not json") is None
    # broadcast for an unknown item id is a no-op
    c._handle({"id": "ui_broadcast", "msg_subclass": "hub.item.updated",
               "result": {"_id": "missing", "value": 1}})


async def test_async_set_item_value_sends(hass: HomeAssistant) -> None:
    ws = _WS([])
    c = _make(hass, ws)
    c._ws = ws  # type: ignore[assignment]
    await c.async_set_item_value("i_light", 10)
    assert json.loads(ws.sent[0])["params"]["_id"] == "i_light"


async def test_send_without_connection_raises(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))
    with pytest.raises(RuntimeError):
        await c.async_set_item_value("x", 1)


async def test_wait_ready_timeout_with_items(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))
    c.items = list(ITEMS)  # items present -> tolerate timeout
    with patch.object(conn_mod, "_READY_TIMEOUT", 0):
        await c.async_wait_ready()


async def test_wait_ready_timeout_without_items(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))
    with patch.object(conn_mod, "_READY_TIMEOUT", 0), pytest.raises(asyncio.TimeoutError):
        await c.async_wait_ready()


async def test_run_raises_repair_issue_when_unreachable(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]), url=None)  # browser never resolves a URL
    sleeps = {"n": 0}

    async def _sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] >= 4:
            c._closed = True

    with (
        patch.object(conn_mod.asyncio, "sleep", _sleep),
        patch.object(conn_mod.ir, "async_create_issue") as create,
    ):
        await c._async_run()
    create.assert_called_once()


async def test_run_success_then_close(hass: HomeAssistant) -> None:
    c = _make(hass, _login_then_data())

    async def _noop() -> None:
        return

    async def _sleep(_seconds: float) -> None:
        c._closed = True

    with (
        patch.object(c, "_async_keepalive", _noop),
        patch.object(conn_mod.asyncio, "sleep", _sleep),
    ):
        await c._async_run()
    assert c.items == ITEMS


async def test_run_handles_connect_error(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))

    async def _boom(url: str) -> None:
        raise RuntimeError("connect failed")

    async def _sleep(_seconds: float) -> None:
        c._closed = True

    with (
        patch.object(c, "_async_connect_and_listen", _boom),
        patch.object(conn_mod.asyncio, "sleep", _sleep),
    ):
        await c._async_run()  # error is swallowed, loop exits cleanly
    assert c._closed is True


async def test_handle_items_only_does_not_ready(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))
    c._handle({"result": {"items": ITEMS}})  # items but no devices yet
    assert c.items == ITEMS
    assert not c._ready.is_set()


async def test_handle_captures_firmware(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))
    c._handle({"result": {"firmware": "4.1.6", "serial": 105203280}})
    assert c.firmware == "4.1.6"


async def test_keepalive_sends_queries(hass: HomeAssistant) -> None:
    ws = _WS([])
    c = _make(hass, ws)
    c._ws = ws  # type: ignore[assignment]
    calls = {"n": 0}

    async def _sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    with patch.object(conn_mod.asyncio, "sleep", _sleep), pytest.raises(asyncio.CancelledError):
        await c._async_keepalive()
    assert ws.sent  # at least one keepalive query was sent


async def test_misc_accessors_and_start(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))
    assert c.serial == "105203280"
    c.device_metadata = {"x": {"k": 1}}
    assert c.get_device_metadata("x") == {"k": 1}
    assert c.get_device_metadata("missing") == {}
    c._update_item(None, 5)  # no item id -> no-op
    assert c._resolve_url() == "ws://x:1"

    async def _noop() -> None:
        return

    with patch.object(c, "_async_run", _noop):
        c.start()
    await c.async_stop()


def _make_nma(
    hass: HomeAssistant, ws: _WS, url: str | None = None
) -> EzloHubConnection:
    return EzloHubConnection(
        hass, _Session(ws), _Browser(url), "105203280", "tok",
        on_update=lambda: None,
        jwt_token="jwt", legacy_auth="A", legacy_sig="S",
        nma_url="wss://nma-ui-cloud.ezlo.com/nma",
    )


async def test_nma_login_and_session(hass: HomeAssistant) -> None:
    # loginUserMios (ha_1) and register (ha_2) succeed, then item/device data.
    ws = _WS([
        _Msg(aiohttp.WSMsgType.TEXT, json.dumps({"id": "ha_1", "result": {}})),
        _Msg(aiohttp.WSMsgType.TEXT, json.dumps({"id": "ha_2", "result": {}})),
        _Msg(aiohttp.WSMsgType.TEXT,
             json.dumps({"id": "ha_3", "result": {"items": ITEMS, "devices": DEVICES}})),
        _Msg(aiohttp.WSMsgType.CLOSED),
    ])
    c = _make_nma(hass, ws)

    async def _noop() -> None:
        return

    with patch.object(c, "_async_keepalive", _noop):
        await c._async_connect_and_listen_nma("wss://nma-ui-cloud.ezlo.com/nma")

    # Handshake framed with api:1.0 and the MMS credentials.
    login = json.loads(ws.sent[0])
    assert login["method"] == "loginUserMios"
    assert login["api"] == "1.0"
    assert login["params"] == {"MMSAuth": "A", "MMSAuthSig": "S"}
    assert json.loads(ws.sent[1])["method"] == "register"
    assert json.loads(ws.sent[1])["params"] == {"serial": "105203280"}
    # Queries were sent NMA-framed and data was applied.
    assert json.loads(ws.sent[2])["api"] == "1.0"
    assert c.items == ITEMS
    assert c.devices == DEVICES
    assert c._ready.is_set()
    # nma_mode is cleared once the session ends.
    assert c._nma_mode is False


async def test_nma_login_missing_tokens(hass: HomeAssistant) -> None:
    c = _make_nma(hass, _WS([]))
    c._legacy_auth = None
    c._ws = _WS([])  # type: ignore[assignment]
    c._nma_mode = True
    assert await c._nma_login(c._ws) is False  # type: ignore[arg-type]


async def test_nma_login_rpc_error(hass: HomeAssistant) -> None:
    ws = _WS([
        _Msg(aiohttp.WSMsgType.TEXT,
             json.dumps({"id": "ha_1", "error": {"code": 401, "data": "denied"}})),
    ])
    c = _make_nma(hass, ws)
    c._ws = ws  # type: ignore[assignment]
    c._nma_mode = True
    assert await c._nma_login(ws) is False  # type: ignore[arg-type]


async def test_nma_broadcast_without_ui_id(hass: HomeAssistant) -> None:
    # Over NMA the item-update broadcast may lack the "ui_broadcast" id; it must
    # still be matched by msg_subclass.
    c = _make_nma(hass, _WS([]))
    c.items = [dict(i) for i in ITEMS]
    c._handle({"msg_subclass": "hub.item.updated",
               "result": {"_id": "i_light", "value": 42}})
    assert next(i for i in c.items if i["_id"] == "i_light")["value"] == 42


async def test_send_nma_framing(hass: HomeAssistant) -> None:
    ws = _WS([])
    c = _make_nma(hass, ws)
    c._ws = ws  # type: ignore[assignment]
    c._nma_mode = True
    await c.async_set_item_value("i_light", 10)
    wire = json.loads(ws.sent[0])
    assert wire["api"] == "1.0"
    assert wire["method"] == "hub.item.value.set"
    assert wire["params"]["_id"] == "i_light"


async def test_run_uses_nma_when_no_local_url(hass: HomeAssistant) -> None:
    c = _make_nma(hass, _WS([]), url=None)  # never discovered on the LAN
    used = {"nma": 0}

    async def _fake_nma(nma_url: str) -> None:
        used["nma"] += 1

    async def _sleep(_seconds: float) -> None:
        c._closed = True

    with (
        patch.object(c, "_async_connect_and_listen_nma", _fake_nma),
        patch.object(conn_mod.asyncio, "sleep", _sleep),
    ):
        await c._async_run()
    assert used["nma"] == 1


async def test_run_falls_back_to_nma_after_local_failures(hass: HomeAssistant) -> None:
    c = _make_nma(hass, _WS([]), url="ws://x:1")  # local resolves but fails
    calls = {"local": 0, "nma": 0}

    async def _local_boom(url: str) -> None:
        calls["local"] += 1
        raise RuntimeError("unreachable")

    async def _fake_nma(nma_url: str) -> None:
        calls["nma"] += 1
        c._closed = True  # stop after first NMA attempt

    async def _sleep(_seconds: float) -> None:
        return

    with (
        patch.object(c, "_async_connect_and_listen", _local_boom),
        patch.object(c, "_async_connect_and_listen_nma", _fake_nma),
        patch.object(conn_mod.asyncio, "sleep", _sleep),
    ):
        await c._async_run()
    # Local tried up to the threshold, then NMA was used.
    assert calls["local"] == conn_mod._LOCAL_FALLBACK_THRESHOLD
    assert calls["nma"] == 1


async def test_ota_request_success_via_handle(hass: HomeAssistant) -> None:
    ws = _WS([])
    c = _make(hass, ws)
    c._ws = ws  # type: ignore[assignment]
    task = asyncio.ensure_future(
        c.async_start_firmware_update("5.7.15", "http://x/dimmer.bin")
    )
    await asyncio.sleep(0)  # let the request send
    sent = json.loads(ws.sent[-1])
    assert sent["method"] == "hub.firmware.update.start"
    assert sent["params"] == {"version": "5.7.15", "urls": {"firmware": "http://x/dimmer.bin"}}
    c._handle({"id": sent["id"], "result": {}})  # accepted
    await task  # no error


async def test_request_timeout_returns_none(hass: HomeAssistant) -> None:
    ws = _WS([])
    c = _make(hass, ws)
    c._ws = ws  # type: ignore[assignment]
    # No response fed -> times out -> None (the OTA "no ack" case).
    assert await c._request("hub.firmware.update.start", {}, timeout=0.01) is None


async def test_ota_no_ack_is_treated_as_started(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    c._request = _none  # type: ignore[assignment]
    await c.async_start_firmware_update("5.7.15", "http://x")  # no raise


async def test_ota_error_response_raises(hass: HomeAssistant) -> None:
    c = _make(hass, _WS([]))

    async def _err(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"error": {"code": -32000, "data": "hub.firmware.update.busy"}}

    c._request = _err  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await c.async_start_firmware_update("5.7.15", "http://x")


async def test_local_login_rejected_returns_false(hass: HomeAssistant) -> None:
    # 5.7.x firmware rejects the local key with a "Bad password" error reply.
    ws = _WS([_Msg(aiohttp.WSMsgType.TEXT, json.dumps(
        {"error": {"code": -32500, "data": "user.login.badpassword"}, "id": "_ID_"}))])
    c = _make(hass, ws)
    c._ws = ws  # type: ignore[assignment]
    assert await c._local_login(ws) is False


async def test_connect_raises_when_local_login_rejected(hass: HomeAssistant) -> None:
    # A rejected login must raise so the supervisor falls back to NMA.
    ws = _WS([
        _Msg(aiohttp.WSMsgType.TEXT, json.dumps(
            {"error": {"code": -32500, "data": "user.login.badpassword"}})),
        _Msg(aiohttp.WSMsgType.CLOSED),
    ])
    c = _make(hass, ws)
    with pytest.raises(RuntimeError):
        await c._async_connect_and_listen("ws://x:1")


async def test_session_no_data_raises(hass: HomeAssistant) -> None:
    # Hub "logs in" but only returns errors (no items/firmware) — the watchdog
    # must drop the session and raise so the supervisor falls back to NMA.
    ws = _WS([_Msg(aiohttp.WSMsgType.TEXT, json.dumps(
        {"method": "hub.info.get", "error": {"code": -32600, "data": "rpc.params.notfound"}, "id": "_ID_"}))])
    c = _make(hass, ws)
    c._ws = ws  # type: ignore[assignment]

    async def _noop() -> None:
        return

    with (
        patch.object(conn_mod, "_READY_TIMEOUT", 0),
        patch.object(c, "_async_keepalive", _noop),
        pytest.raises(RuntimeError),
    ):
        await c._async_run_session(ws)


async def test_async_stop(hass: HomeAssistant) -> None:
    ws = _WS([])
    c = _make(hass, ws)
    c._ws = ws  # type: ignore[assignment]
    await c.async_stop()
    assert c._closed is True
    assert ws.closed is True
