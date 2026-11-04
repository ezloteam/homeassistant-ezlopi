"""Tests for the remote feature-flag client."""
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant

from custom_components.ezlopi.flags import FLAG_FIRMWARE_CATALOG, EzloFeatureFlags


def _doc(enabled: bool, ttl: int = 0, *, env: str = "live",
         app: str = "ezlopi_homeassistant", platform: str = "homeassistant",
         subject: str = "anonymous") -> dict[str, Any]:
    return {
        "schema": 1, "flags": {FLAG_FIRMWARE_CATALOG: {"enabled": enabled}},
        "ttl_seconds": ttl,
        "scope": {
            "env": env, "app": app, "platform": platform,
            "subject": {"kind": subject},
        },
    }


class _Resp:
    def __init__(self, status: int, payload: Any = None,
                 headers: dict[str, str] | None = None, raise_client: bool = False) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}
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


class _Store:
    def __init__(self) -> None:
        self.saved: Any = None

    async def async_save(self, data: Any) -> None:
        self.saved = data


def _flags(hass: HomeAssistant, session: Any, *, bundled: Any = None,
           cached: Any = None, token: str | None = "jwt", store: Any = None) -> EzloFeatureFlags:
    return EzloFeatureFlags(
        hass, session, app_version="1.7.0", device_id="dev-id",
        token_provider=lambda: token, bundled=bundled, cached=cached, store=store,
    )


# ---- is_enabled precedence ----

def test_compiled_default_when_no_document(hass: HomeAssistant) -> None:
    f = _flags(hass, _Session(_Resp(200)))
    # firmware_catalog defaults ON in the compiled defaults.
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is True
    # An unknown flag with no document and no compiled default is OFF.
    assert f.is_enabled("something_else") is False


def test_bundled_used_when_no_live(hass: HomeAssistant) -> None:
    f = _flags(hass, _Session(_Resp(200)), bundled=_doc(False))
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is False


def test_cached_beats_bundled(hass: HomeAssistant) -> None:
    # Persisted last-known (cached) wins over the bundled snapshot.
    f = _flags(hass, _Session(_Resp(200)), bundled=_doc(True), cached=_doc(False))
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is False


def test_scope_mismatch_document_discarded(hass: HomeAssistant) -> None:
    # A document for a different env/app/platform doesn't apply to this config
    # and is skipped, falling through to the compiled default (ON).
    f = _flags(hass, _Session(_Resp(200)), bundled=_doc(False, env="qa"))
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is True
    f2 = _flags(hass, _Session(_Resp(200)), bundled=_doc(False, platform="ios"))
    assert f2.is_enabled(FLAG_FIRMWARE_CATALOG) is True


# ---- async_refresh ----

async def test_refresh_200_sets_live_returns_ttl_saves(hass: HomeAssistant) -> None:
    store = _Store()
    session = _Session(_Resp(200, _doc(False, ttl=900), headers={"ETag": "v1"}))
    f = _flags(hass, session, bundled=_doc(True), store=store)
    ttl = await f.async_refresh()
    assert ttl == 900
    # Live document overrides the bundled snapshot...
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is False
    # ...and is persisted for the next (possibly offline) start.
    assert store.saved == _doc(False, ttl=900)
    # Bearer token and stable device id are sent.
    headers = session.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer jwt"
    assert headers["X-Device-Id"] == "dev-id"


async def test_refresh_304_keeps_live(hass: HomeAssistant) -> None:
    f = _flags(hass, _Session(_Resp(304)), bundled=_doc(True))
    f._live = _doc(False, ttl=600)
    f._etag = "v1"
    ttl = await f.async_refresh()
    assert ttl == 600
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is False


async def test_refresh_400_unknown_app_keeps_bundled(hass: HomeAssistant) -> None:
    # Until ezlopi_homeassistant is registered, /flags is 400; keep the snapshot.
    f = _flags(hass, _Session(_Resp(400, {"error_code": "unknown_app"})), bundled=_doc(True))
    assert await f.async_refresh() is None
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is True


async def test_refresh_error_keeps_cached(hass: HomeAssistant) -> None:
    f = _flags(hass, _Session(_Resp(200, raise_client=True)),
               bundled=_doc(True), cached=_doc(False))
    assert await f.async_refresh() is None
    # Network failed -> fall back to the persisted last-known (cached), not bundled.
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is False


async def test_refresh_malformed_document_keeps_last(hass: HomeAssistant) -> None:
    f = _flags(hass, _Session(_Resp(200, {"no": "flags"})), bundled=_doc(True))
    assert await f.async_refresh() is None
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is True


async def test_refresh_without_token_omits_authorization(hass: HomeAssistant) -> None:
    session = _Session(_Resp(200, _doc(True)))
    f = _flags(hass, session, token=None)
    await f.async_refresh()
    assert "Authorization" not in session.calls[0]["headers"]


async def test_refresh_429_returns_retry_after(hass: HomeAssistant) -> None:
    # Honour the service's Retry-After as the next-poll delay.
    f = _flags(hass, _Session(_Resp(429, headers={"Retry-After": "120"})), bundled=_doc(True))
    assert await f.async_refresh() == 120
    # Bundled still drives while backing off.
    assert f.is_enabled(FLAG_FIRMWARE_CATALOG) is True
