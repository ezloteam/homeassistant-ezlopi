"""Firmware release tracking + variant selection for ezloPi controllers.

Polls the public ezloPi firmware manifest (the same source the hubs use for
OTA) and reproduces the variant-selection logic from the official Ezlo mobile
app (``FirmwareUpdateService.getDeviceSuffix`` / ``FirmwareManifestManager``),
so the Home Assistant update entity flashes exactly the image the vendor would.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, EZLOPI_FIRMWARE_MANIFEST_URL

_LOGGER = logging.getLogger(__name__)

_REFRESH_INTERVAL = timedelta(hours=12)
_FETCH_TIMEOUT = 15
# Chips that ship a single per-target image (selected by hardware alone).
_DEDICATED_TARGETS = ("esp32c3", "esp32s2", "esp32s3")
# The firmware version at/after which esp32 uses one universal image.
_UNIVERSAL_ESP32_FROM = "5.6.0"


@dataclass(frozen=True)
class FirmwareVariant:
    """One firmware image entry from the manifest."""

    suffix: str
    target: str
    filename: str
    url: str
    md5: str


@dataclass(frozen=True)
class FirmwareManifest:
    """Parsed ezloPi firmware manifest."""

    version: str
    tag: str
    variants: tuple[FirmwareVariant, ...]

    def by_suffix(self, suffix: str) -> FirmwareVariant | None:
        return next((v for v in self.variants if v.suffix == suffix), None)

    def by_target(self, target: str) -> FirmwareVariant | None:
        return next((v for v in self.variants if v.target.lower() == target), None)


def _version_key(version: str) -> list[int]:
    """Numeric-segment key for dotted versions (mirrors NSString .numeric compare)."""
    key: list[int] = []
    for seg in str(version).split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        key.append(int(digits) if digits else 0)
    return key


def version_compare(a: str, b: str) -> int:
    """Return -1/0/1 for a<b / a==b / a>b using numeric segment comparison."""
    ka, kb = _version_key(a), _version_key(b)
    n = max(len(ka), len(kb))
    ka += [0] * (n - len(ka))
    kb += [0] * (n - len(kb))
    return (ka > kb) - (ka < kb)


def select_variant(
    manifest: FirmwareManifest,
    chip: str | None,
    firmware_version: str | None,
    model: str | None,
) -> FirmwareVariant | None:
    """Pick the firmware image for a device, matching the Ezlo app's logic.

    esp32c3/s2/s3 -> that target's image. esp32 -> for firmware < 5.6.0 the
    image depends on the model (frankever dimmer/switch, else generic esp32);
    from 5.6.0 on, a single universal esp32 image. Returns None when the correct
    variant can't be determined (e.g. model required but unknown), so the caller
    refuses to flash rather than guess.
    """
    chip_l = (chip or "esp32").lower()
    model_l = (model or "").lower()
    version = firmware_version or "0.0.0"

    if chip_l in _DEDICATED_TARGETS:
        return manifest.by_target(chip_l)

    if chip_l != "esp32":
        # Unknown chip: fall back to the generic esp32 image, as the app does.
        return manifest.by_suffix("esp32")

    if version_compare(version, _UNIVERSAL_ESP32_FROM) < 0:
        if not model_l:
            return None  # model required to disambiguate < 5.6.0, but unknown
        if "ezlopi_frankever_us_wb01d" in model_l:
            suffix = "dimmer"
        elif "ezlopi_frankever_ssms118" in model_l:
            suffix = "switch"
        else:
            suffix = "esp32"
    else:
        suffix = "esp32"
    return manifest.by_suffix(suffix)


def _parse_manifest(payload: object) -> FirmwareManifest | None:
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    if not version:
        return None
    variants: list[FirmwareVariant] = []
    for entry in payload.get("firmware") or []:
        if not isinstance(entry, dict):
            continue
        suffix, target, url = entry.get("suffix"), entry.get("target"), entry.get("url")
        if not (suffix and target and url):
            continue
        variants.append(FirmwareVariant(
            suffix=str(suffix),
            target=str(target),
            filename=str(entry.get("filename") or ""),
            url=str(url),
            md5=str(entry.get("md5") or ""),
        ))
    return FirmwareManifest(
        version=str(version),
        tag=str(payload.get("tag") or ""),
        variants=tuple(variants),
    )


class EzloFirmwareCoordinator(DataUpdateCoordinator[FirmwareManifest | None]):
    """Fetches and parses the ezloPi firmware manifest.

    Fetch/parse failures are non-fatal: the last known manifest is retained
    (``None`` until the first success), so a dl.mios.com outage never breaks the
    integration or the update entities.
    """

    def __init__(self, hass: HomeAssistant, session: aiohttp.ClientSession) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} firmware",
            update_interval=_REFRESH_INTERVAL,
        )
        self._session = session

    async def _async_update_data(self) -> FirmwareManifest | None:
        # Cache-bust like the vendor app so the check sees the current manifest.
        url = f"{EZLOPI_FIRMWARE_MANIFEST_URL}?t={int(time.time())}"
        try:
            async with self._session.get(
                url,
                headers={"Cache-Control": "no-cache"},
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT),
            ) as response:
                if response.status != 200:
                    _LOGGER.debug("firmware manifest HTTP %s; keeping last known", response.status)
                    return self.data
                payload = await response.json(content_type=None)
        except Exception as err:  # noqa: BLE001 - optional data, never fatal
            _LOGGER.debug("firmware manifest fetch failed: %s", err)
            return self.data

        manifest = _parse_manifest(payload)
        if manifest is None:
            _LOGGER.debug("firmware manifest could not be parsed")
            return self.data
        return manifest
