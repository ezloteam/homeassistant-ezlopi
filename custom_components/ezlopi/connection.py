"""Asyncio-native local websocket connection to a single ezloPi hub.

Replaces the previous threaded ``websocket-client`` implementation. Runs entirely
on the Home Assistant event loop and uses the HA-shared aiohttp ``ClientSession``
(injected), so no per-connection session and no background OS threads.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .mdns_connector import EzloPiMDSConnector
from .utils import get_devices_info, get_login_params, set_item_value_request

_LOGGER = logging.getLogger(__name__)

_LOGIN_METHOD = "hub.offline.login.ui"
_RECONNECT_DELAY = 5
_READY_TIMEOUT = 20
# The hub closes idle sockets; keep traffic flowing well under that window.
_KEEPALIVE_INTERVAL = 15
# Raise a repair issue once a hub has been unreachable for this many tries.
_UNREACHABLE_THRESHOLD = 3
# After this many consecutive local misses/failures, fall back to the cloud
# NMA relay (if configured) instead of the LAN websocket.
_LOCAL_FALLBACK_THRESHOLD = 3


class EzloHubConnection:
    """A persistent, self-healing websocket connection to one ezloPi hub."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        browser: EzloPiMDSConnector,
        serial: str,
        token: str | None,
        on_update: Callable[[], None],
        jwt_token: str | None = None,
        legacy_auth: str | None = None,
        legacy_sig: str | None = None,
        nma_url: str | None = None,
    ) -> None:
        self._hass = hass
        self._session = session
        self._browser = browser
        self._serial = serial
        self._token = token
        self._on_update = on_update
        # Cloud/NMA fallback credentials. When the hub can't be reached on the
        # LAN we relay through the NMA broker at ``_nma_url`` using the cloud
        # JWT (bearer) plus the legacy MMS pair (loginUserMios handshake).
        self._jwt_token = jwt_token
        self._legacy_auth = legacy_auth
        self._legacy_sig = legacy_sig
        self._nma_url = nma_url
        # True while the active socket is the NMA relay, which frames requests
        # differently ({"api": "1.0", ...}) from the LAN protocol.
        self._nma_mode = False
        self._id_counter = 0
        # Correlate request ids to awaiting futures for request/response calls
        # (e.g. the OTA trigger), resolved from the read loop in _handle.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Set once a session receives real data (items/firmware). Used to detect
        # a socket that connects/logs in but never delivers (e.g. 5.7.x firmware
        # that rejects the local key) so we can fall back to NMA.
        self._session_got_data = False

        # Latest hub state, consumed by the coordinator via the utils helpers
        # (get_items/get_devices read these attributes).
        self.items: list[dict[str, Any]] = []
        self.devices: list[dict[str, Any]] = []
        self.device_metadata: dict[str, Any] = {}
        # Controller info from hub.info.get: firmware version, hardware chip
        # (e.g. "esp32", "esp32c3") and model — used to pick the OTA image.
        self.firmware: str | None = None
        self.chip: str | None = None
        self.model: str | None = None

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected = False
        self._ready = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def serial(self) -> str:
        return self._serial

    def start(self) -> None:
        """Launch the supervised connect/listen/reconnect loop."""
        self._run_task = self._hass.async_create_background_task(
            self._async_run(), f"ezlopi-ws-{self._serial}"
        )

    async def async_wait_ready(self) -> None:
        """Wait until the hub has delivered its first item (and ideally device) list.

        Resolves once both items and devices have arrived. If only items arrive
        within the timeout we still proceed (devices refine metadata via a later
        push); we only fail when nothing at all came back.
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_READY_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            if not self.items:
                raise

    async def async_stop(self) -> None:
        self._closed = True
        if self._ws is not None:
            await self._ws.close()
        if self._run_task is not None:
            self._run_task.cancel()

    async def async_set_item_value(self, item_id: str, value: Any) -> None:
        await self._send(set_item_value_request(item_id, value))

    async def _request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any] | None:
        """Send a method and wait for the hub's response (matched by id).

        Returns the response dict, or None if the hub sent no response within
        ``timeout`` (some commands, e.g. the OTA start, reboot before acking).
        """
        if self._ws is None or self._ws.closed:
            raise RuntimeError(f"hub {self._serial} not connected")
        req_id = self._next_id()
        if self._nma_mode:
            wire = {"api": "1.0", "method": method, "id": req_id, "params": params}
        else:
            wire = {"method": method, "id": req_id, "params": params}
        fut: asyncio.Future[dict[str, Any]] = self._hass.loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send_str(json.dumps(wire))
            return await asyncio.wait_for(fut, timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        finally:
            self._pending.pop(req_id, None)

    async def async_start_firmware_update(self, version: str, url: str) -> None:
        """Start the controller's OTA to ``version`` from firmware image ``url``.

        Sends the Ezlo app's ``hub.firmware.update.start`` command and inspects
        the reply. 5.7.x firmware starts the OTA but reboots without acking
        (EZPI-957), so *no* reply is treated as "started". An explicit error
        reply (e.g. rpc.method.notfound on firmware too old for the OTA RPC, or
        hub.firmware.update.busy/failed) is raised so the UI can surface it.
        """
        _LOGGER.info(
            "hub %s: starting OTA to %s from %s", self._serial, version, url
        )
        resp = await self._request(
            "hub.firmware.update.start",
            {"version": version, "urls": {"firmware": url}},
            timeout=15.0,
        )
        if resp is None:
            _LOGGER.info(
                "hub %s: OTA command sent, no ack (device rebooting to flash)",
                self._serial,
            )
            return
        if self._rpc_error(resp):
            err = resp.get("error") or {}
            _LOGGER.warning("hub %s: OTA rejected: %s", self._serial, err)
            raise RuntimeError(str(err.get("data") or err.get("message") or err))
        _LOGGER.info("hub %s: OTA start accepted", self._serial)

    def _resolve_url(self) -> str | None:
        return self._browser.get_connection_link_from_serial(self._serial)

    def _next_id(self) -> str:
        self._id_counter += 1
        return f"ha_{self._id_counter}"

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise RuntimeError(f"hub {self._serial} not connected")
        if self._nma_mode:
            # The NMA broker expects the hub method wrapped in its own envelope
            # with a unique id; responses/broadcasts are matched by content in
            # _handle, so the id only needs to be unique per request.
            wire: dict[str, Any] = {
                "api": "1.0",
                "method": payload.get("method"),
                "id": self._next_id(),
                "params": payload.get("params", {}),
            }
        else:
            wire = payload
        await self._ws.send_str(json.dumps(wire))

    @property
    def _issue_id(self) -> str:
        return f"hub_unreachable_{self._serial}"

    async def _async_run(self) -> None:
        failures = 0
        local_misses = 0
        while not self._closed:
            url = self._resolve_url()
            # Prefer the LAN socket; fall back to the cloud NMA relay only once
            # the hub has been missing/unreachable locally for a few tries (or
            # was never discovered on the LAN) and an NMA URL is available.
            use_nma = self._nma_url is not None and (
                url is None or local_misses >= _LOCAL_FALLBACK_THRESHOLD
            )
            try:
                if use_nma:
                    await self._async_connect_and_listen_nma(self._nma_url)  # type: ignore[arg-type]
                    # Re-probe the LAN after each NMA session so we return to
                    # local control as soon as the hub reappears on the network.
                    local_misses = 0
                    failures = 0
                elif url is not None:
                    await self._async_connect_and_listen(url)
                    local_misses = 0
                    failures = 0
                else:
                    # Not discovered locally and no NMA fallback available.
                    local_misses += 1
                    failures += 1
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep the supervisor alive
                _LOGGER.debug("hub %s connection error: %s", self._serial, err)
                failures += 1
                if use_nma:
                    # NMA attempt failed; retry the LAN before NMA again.
                    local_misses = 0
                else:
                    local_misses += 1
            self._connected = False
            self._on_update()  # surface unavailability to entities
            if failures == _UNREACHABLE_THRESHOLD:
                ir.async_create_issue(
                    self._hass,
                    DOMAIN,
                    self._issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="hub_unreachable",
                    translation_placeholders={"serial": self._serial},
                )
            if not self._closed:
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _async_connect_and_listen(self, url: str) -> None:
        """Connect over the LAN websocket, log in offline, then stream state."""
        _LOGGER.info("Connecting to hub %s at %s", self._serial, url)
        # No ws heartbeat: the hub does not answer ping frames, so aiohttp's
        # heartbeat would tear the socket down. We keep it alive with periodic
        # application-level queries instead (see _async_keepalive).
        self._nma_mode = False
        async with self._session.ws_connect(url) as ws:
            self._ws = ws
            self._connected = True
            try:
                logged_in = await asyncio.wait_for(
                    self._local_login(ws), timeout=_READY_TIMEOUT
                )
            except (TimeoutError, asyncio.TimeoutError):
                logged_in = False
            if not logged_in:
                # Login failed/timed out (e.g. 5.7.x firmware rejects the local
                # offline-login key with "Bad password"). Raise so the supervisor
                # counts a local miss and falls back to the NMA relay, which
                # authenticates via the cloud and still works for these hubs.
                raise RuntimeError(f"hub {self._serial} local login failed")
            await self._async_run_session(ws)

    async def _local_login(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        """Send the offline UI login and wait for the hub to confirm it.

        Returns True once the hub echoes the login method, False if the hub
        rejects it (an error reply, e.g. ``user.login.badpassword`` on firmware
        that no longer accepts the local key) or the socket closes.
        """
        login = get_login_params()
        login["params"]["user"] = self._serial
        login["params"]["token"] = self._token
        await self._send(login)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = self._parse(msg.data)
                if data is None:
                    continue
                if data.get("method") == _LOGIN_METHOD:
                    _LOGGER.info("Hub %s logged in", self._serial)
                    return True
                err = data.get("error")
                if isinstance(err, dict) and err.get("code"):
                    _LOGGER.warning(
                        "Hub %s local login rejected: %s",
                        self._serial, err.get("data") or err.get("message"),
                    )
                    return False
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return False
        return False

    async def _async_connect_and_listen_nma(self, nma_url: str) -> None:
        """Connect through the cloud NMA relay when the LAN is unreachable.

        The NMA host presents a certificate signed by a secp256k1 CA that the
        stack can't validate (matching hubcmd's behaviour), so TLS verification
        is disabled for this socket. Auth is the cloud JWT as a bearer header
        plus the loginUserMios/register handshake.
        """
        _LOGGER.info("Connecting to hub %s via NMA relay %s", self._serial, nma_url)
        self._nma_mode = True
        headers = (
            {"Authorization": f"Bearer {self._jwt_token}"} if self._jwt_token else {}
        )
        try:
            async with self._session.ws_connect(
                nma_url, ssl=False, headers=headers
            ) as ws:
                self._ws = ws
                self._connected = True
                try:
                    logged_in = await asyncio.wait_for(
                        self._nma_login(ws), timeout=_READY_TIMEOUT
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    _LOGGER.warning("Hub %s NMA login timed out", self._serial)
                    return
                if logged_in:
                    await self._async_run_session(ws)
        finally:
            self._nma_mode = False

    async def _nma_login(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        """Perform the NMA handshake: loginUserMios then register by serial."""
        if not self._legacy_auth or not self._legacy_sig:
            _LOGGER.warning(
                "Hub %s: no legacy MMS tokens available for NMA login",
                self._serial,
            )
            return False
        login_id = self._next_id()
        await ws.send_str(json.dumps({
            "api": "1.0",
            "method": "loginUserMios",
            "id": login_id,
            "params": {
                "MMSAuth": self._legacy_auth,
                "MMSAuthSig": self._legacy_sig,
            },
        }))
        resp = await self._await_response(ws, login_id)
        if resp is None or self._rpc_error(resp):
            _LOGGER.warning("Hub %s NMA loginUserMios failed: %s", self._serial, resp)
            return False
        register_id = self._next_id()
        await ws.send_str(json.dumps({
            "api": "1.0",
            "method": "register",
            "id": register_id,
            "params": {"serial": self._serial},
        }))
        resp = await self._await_response(ws, register_id)
        if resp is None or self._rpc_error(resp):
            _LOGGER.warning("Hub %s NMA register failed: %s", self._serial, resp)
            return False
        _LOGGER.info("Hub %s registered over NMA", self._serial)
        return True

    async def _await_response(
        self, ws: aiohttp.ClientWebSocketResponse, want_id: str
    ) -> dict[str, Any] | None:
        """Read frames until one matching ``want_id`` arrives (or the socket ends).

        Broadcasts that arrive mid-handshake are ignored; the caller bounds this
        with a timeout.
        """
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = self._parse(msg.data)
                if data is not None and data.get("id") == want_id:
                    return data
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return None
        return None

    @staticmethod
    def _rpc_error(data: dict[str, Any]) -> bool:
        err = data.get("error")
        return isinstance(err, dict) and bool(err.get("code"))

    async def _async_run_session(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Send the initial queries, keep the socket alive, and stream updates.

        Shared by the LAN and NMA transports once their respective logins have
        completed; both speak the same hub RPC surface from here on.

        Raises if the socket connects but never delivers data within the ready
        window (a "logged in" reply can be a lie — e.g. 5.7.x firmware rejects
        the local key yet still echoes the login method), so the supervisor
        falls back to the NMA relay instead of sitting on a dead link.
        """
        self._session_got_data = False
        await self._send_queries()
        keepalive = asyncio.ensure_future(self._async_keepalive())
        watchdog = asyncio.ensure_future(self._async_data_watchdog(ws))
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = self._parse(msg.data)
                    if data is None:
                        continue
                    self._handle(data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            keepalive.cancel()
            watchdog.cancel()
        if not self._session_got_data:
            raise RuntimeError(f"hub {self._serial} delivered no data after login")

    async def _async_data_watchdog(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Drop the socket if no data arrives within the ready window."""
        await asyncio.sleep(_READY_TIMEOUT)
        if not self._session_got_data:
            _LOGGER.warning(
                "Hub %s connected but sent no data; dropping to fall back", self._serial
            )
            await ws.close()

    async def _send_queries(self) -> None:
        for query in get_devices_info():
            await self._send(query)

    async def _async_keepalive(self) -> None:
        """Periodically re-query so the hub keeps the socket open and data fresh."""
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL)
            await self._send_queries()

    @staticmethod
    def _parse(raw: str) -> dict[str, Any] | None:
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _handle(self, data: dict[str, Any]) -> None:
        # Deliver responses to in-flight _request() calls (e.g. OTA start).
        req_id = data.get("id")
        if req_id is not None and req_id in self._pending:
            fut = self._pending.get(req_id)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return

        if data.get("method") == _LOGIN_METHOD:
            return

        # Item-change broadcasts are keyed by msg_subclass. Locally they carry
        # id "ui_broadcast"; relayed over NMA they may not, so match on the
        # subclass directly rather than the id.
        if data.get("msg_subclass") == "hub.item.updated":
            result = data.get("result", {})
            self._update_item(result.get("_id"), result.get("value"))
            return

        result = data.get("result") or {}
        changed = False
        if "firmware" in result:  # hub.info.get response
            self.firmware = result["firmware"]
            # "hardware" is the chip family (esp32/esp32c3/...); both feed OTA
            # image selection (see firmware.select_variant).
            self.chip = result.get("hardware")
            self.model = result.get("model")
            changed = True
        if "items" in result:
            self.items = result["items"]
            changed = True
        if "devices" in result:
            self.devices = result["devices"]
            self.device_metadata = {
                str(d["_id"]): {
                    "deviceType": d.get("deviceTypeId"),
                    "category": d.get("category"),
                    "subcategory": d.get("subcategory"),
                    "armed": d.get("armed", False),
                    "room_id": d.get("roomId"),
                    "battery_powered": d.get("batteryPowered", False),
                }
                for d in self.devices
                if d.get("_id")
            }
            changed = True
        if changed:
            # Real data arrived — this session is delivering.
            self._session_got_data = True
        if changed and self.items:
            # Ready once we have both items and their device metadata.
            if self.devices:
                self._ready.set()
                # Hub is reachable again — clear any unreachable repair issue.
                ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
            self._on_update()

    def _update_item(self, item_id: str | None, value: Any) -> None:
        if item_id is None:
            return
        for item in self.items:
            if item.get("_id") == item_id:
                item["value"] = value
                break
        self._on_update()

    def get_device_metadata(self, device_id: str) -> dict[str, Any]:
        meta: dict[str, Any] = self.device_metadata.get(device_id, {})
        return meta
