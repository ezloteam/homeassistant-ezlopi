"""Remote feature-flag client for the ezloPi integration.

Mirrors the kill switch the Ezlo mobile apps use (iOS !124 / Android !1725): a
small document of booleans served by ``cloud/feature-flags`` behind api-cloud,
with a bundled point-in-time snapshot (``flags_snapshot.json``) as the offline
compiled default.

Precedence (agreed by the iOS, Android and Home Assistant clients)::

    live response > cached last response > bundled snapshot > compiled default

The fetch never blocks entity setup, and any error keeps the last good document,
so a flag never flips just because the network is down. The last good live
document is persisted (HA ``Store``) so the choice survives a restart taken
while offline — important for a kill switch.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import EZLOPI_FEATURE_FLAGS_BASE

_LOGGER = logging.getLogger(__name__)

# This integration's dedicated id in the feature-flags APP registry and its
# platform value. Both are fixed per-client constants, NOT an OEM app key.
# (Registration of `ezlopi_homeassistant` / platform `homeassistant` is owned by
# cloud/feature-flags; until it lands, live /flags answers 400 unknown_app and
# the bundled snapshot is used — the intended graceful degradation.)
FLAGS_APP = "ezlopi_homeassistant"
FLAGS_PLATFORM = "homeassistant"

# The integration only ever talks to the live api-cloud base, so the running
# environment is "live"; a cached/bundled document for any other env/app/platform
# is discarded (its flags don't apply to this configuration).
FLAGS_ENV = "live"

# Kill switch for the server-side firmware-catalog decision path. ON (default)
# => use the catalog GET /decision; OFF => fall back to the dl.mios.com manifest
# + client-side variant picker.
FLAG_FIRMWARE_CATALOG = "firmware_catalog"

# Last-resort values when no document carries a flag.
_COMPILED_DEFAULTS: dict[str, bool] = {FLAG_FIRMWARE_CATALOG: True}

_FETCH_TIMEOUT = 10
# Clamp the refresh cadence to the service's agreed bounds (README §4): honour
# ttl_seconds, but never poll faster than 60s nor let it stretch past a day.
_MIN_REFRESH = 60
_MAX_REFRESH = 86400

STORAGE_VERSION = 1


def flags_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Per-entry Store holding the last good live flags document."""
    return Store(hass, STORAGE_VERSION, f"ezlopi_flags_{entry_id}")


def _parse_retry_after(value: str | None) -> int | None:
    """Seconds to wait from a 429 Retry-After (delta-seconds form only)."""
    if not value:
        return None
    try:
        return max(int(value), 1)
    except ValueError:
        return None


class EzloFeatureFlags:
    """Holds the current flags document and refreshes it on the service's ttl."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        *,
        app_version: str,
        device_id: str,
        token_provider: Callable[[], str | None],
        bundled: dict[str, Any] | None,
        cached: dict[str, Any] | None = None,
        store: Store[dict[str, Any]] | None = None,
    ) -> None:
        self._hass = hass
        self._session = session
        self._app_version = app_version
        self._device_id = device_id
        self._token_provider = token_provider
        self._bundled = bundled
        self._cached = cached  # last good live doc, persisted across restarts
        self._live: dict[str, Any] | None = None
        self._store = store
        self._etag: str | None = None
        self._unsub: Callable[[], None] | None = None
        self._warned_unknown_app = False
        self._warned_anonymous = False

    @callback
    def start(self) -> None:
        """Begin fetching without blocking setup (bundled/cached cover the gap)."""
        self._hass.async_create_background_task(
            self._async_refresh_and_reschedule(), "ezlopi-feature-flags"
        )

    def is_enabled(self, key: str) -> bool:
        """Current value of a flag, by the agreed precedence.

        A document whose scope (env/app/platform) doesn't match this running
        configuration is skipped — its flags don't apply here.
        """
        for doc in (self._live, self._cached, self._bundled):
            if not self._scope_matches(doc):
                continue
            value = self._flag_from(doc, key)
            if value is not None:
                return value
        return _COMPILED_DEFAULTS.get(key, False)

    @staticmethod
    def _scope_matches(doc: dict[str, Any] | None) -> bool:
        if not isinstance(doc, dict):
            return False
        scope = doc.get("scope")
        if not isinstance(scope, dict):
            return False
        return (
            scope.get("env") == FLAGS_ENV
            and scope.get("app") == FLAGS_APP
            and scope.get("platform") == FLAGS_PLATFORM
        )

    @staticmethod
    def _flag_from(doc: dict[str, Any] | None, key: str) -> bool | None:
        if not isinstance(doc, dict):
            return None
        entry = (doc.get("flags") or {}).get(key)
        if not isinstance(entry, dict) or "enabled" not in entry:
            return None
        return bool(entry["enabled"])

    async def _async_refresh_and_reschedule(self, _now: Any = None) -> None:
        ttl = await self.async_refresh()
        delay = min(max(ttl or _MIN_REFRESH, _MIN_REFRESH), _MAX_REFRESH)
        self._unsub = async_call_later(
            self._hass, delay, self._async_refresh_and_reschedule
        )

    async def async_refresh(self) -> int | None:
        """Fetch /flags once. Returns ttl_seconds on success, else None.

        Any failure (network, non-2xx/304, or 400 unknown_app while the app id
        is not yet registered) keeps the last good document; it never raises.
        """
        params = {
            "app": FLAGS_APP,
            "platform": FLAGS_PLATFORM,
            "app_version": self._app_version,
        }
        headers: dict[str, str] = {"X-Device-Id": self._device_id}
        token = self._token_provider()
        if token:
            # Must be the Bearer header; an invalid/stale token degrades to an
            # anonymous document (never 401), so sending it is always safe.
            headers["Authorization"] = f"Bearer {token}"
        if self._etag:
            headers["If-None-Match"] = self._etag
        try:
            async with self._session.get(
                f"{EZLOPI_FEATURE_FLAGS_BASE}/flags",
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT),
            ) as response:
                if response.status == 304:
                    return self._ttl(self._live or self._cached)
                if response.status == 429:
                    # Back off for exactly as long as the service asks.
                    return _parse_retry_after(response.headers.get("Retry-After"))
                if response.status == 400:
                    if not self._warned_unknown_app:
                        _LOGGER.info(
                            "feature-flags: %s not registered yet; using the "
                            "bundled snapshot", FLAGS_APP,
                        )
                        self._warned_unknown_app = True
                    return None
                if response.status != 200:
                    _LOGGER.debug(
                        "feature-flags HTTP %s; keeping last known", response.status
                    )
                    return None
                doc = await response.json(content_type=None)
                etag = response.headers.get("ETag")
        except Exception as err:  # noqa: BLE001 - optional data, never fatal
            _LOGGER.debug("feature-flags fetch failed: %s", err)
            return None
        if not isinstance(doc, dict) or "flags" not in doc:
            _LOGGER.debug("feature-flags: malformed document; keeping last known")
            return None
        self._live = doc
        self._cached = doc
        self._etag = etag
        # Identity sanity-check: if we sent a token but the service evaluated us
        # as anonymous, jwt-router-3 didn't accept it — surface it (per-account
        # targeting won't apply) rather than silently degrade.
        if token and not self._warned_anonymous:
            subject = (doc.get("scope") or {}).get("subject") or {}
            if subject.get("kind") == "anonymous":
                _LOGGER.warning(
                    "feature-flags: a user token was sent but the response is "
                    "anonymous; the login/jwt-router did not accept the token"
                )
                self._warned_anonymous = True
        if self._store is not None:
            await self._store.async_save(doc)
        return self._ttl(doc)

    @staticmethod
    def _ttl(doc: dict[str, Any] | None) -> int | None:
        if not isinstance(doc, dict):
            return None
        ttl = doc.get("ttl_seconds")
        return int(ttl) if isinstance(ttl, (int, float)) else None

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
