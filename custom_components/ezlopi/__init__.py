"""The ezloPi integration."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .connection import EzloHubConnection
from .const import DOMAIN
from .coordinator import EzloDataUpdateCoordinator
from .ezlopi_utils import EzloAuthError, EzloCloudAPI
from .firmware import EzloFirmwareCoordinator
from .mdns_connector import EzloPiMDSConnector

_LOGGER = logging.getLogger(__name__)

DATA_BROWSER = "browser"

# Overall cap on waiting for hubs to deliver initial data during setup. Setup
# never blocks longer than this regardless of how many hubs are slow/offline.
_SETUP_READY_TIMEOUT = 12

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


@dataclass
class EzloRuntimeData:
    """Per-config-entry runtime state, stored on ConfigEntry.runtime_data."""

    api: EzloCloudAPI
    firmware: EzloFirmwareCoordinator
    coordinators: list[EzloDataUpdateCoordinator] = field(default_factory=list)


EzloConfigEntry = ConfigEntry  # ConfigEntry[EzloRuntimeData] at type-check time


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the global mDNS browser used to locate hubs on the LAN."""
    browser = EzloPiMDSConnector()
    await browser.async_init(hass)
    await browser.async_get_service_info()
    hass.data[DOMAIN] = {DATA_BROWSER: browser}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EzloConfigEntry) -> bool:
    """Set up an ezloPi account: connect to each hub and start its coordinator."""
    session = async_get_clientsession(hass)
    browser: EzloPiMDSConnector = hass.data[DOMAIN][DATA_BROWSER]

    api = EzloCloudAPI(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=session,
    )

    # test-before-setup: surface auth vs connection failures distinctly.
    try:
        ok = await api.fetch_hub_list()
    except EzloAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(str(err)) from err
    if not ok or not api.get_hub_list():
        raise ConfigEntryNotReady("No ezloPi hubs found for this account")

    # Account-wide firmware release tracker (latest version from dl.mios.com).
    # Best-effort: a failed fetch leaves latest_version unknown, never blocks setup.
    firmware = EzloFirmwareCoordinator(hass, session)
    await firmware.async_refresh()

    runtime = EzloRuntimeData(api=api, firmware=firmware)
    entry.runtime_data = runtime

    # Start every hub connection, then wait for initial data CONCURRENTLY with a
    # short overall cap. Never block setup on a single hub: offline hubs, and
    # 5.7.x hubs that need the (slower) NMA fallback, would otherwise serialize
    # into a minutes-long setup that Home Assistant cancels ("stuck initializing").
    # Entities for hubs that aren't ready yet are added later via the coordinator
    # push, so a slow hub just appears a bit after startup.
    for hub in api.get_hub_list().values():
        connection = EzloHubConnection(
            hass, session, browser, hub.serial, hub.token,
            on_update=lambda: None,
            jwt_token=api.token,
            legacy_auth=api.legacy_auth,
            legacy_sig=api.legacy_sig,
            nma_url=hub.nma_url,
            name=hub.name,
        )
        coordinator = EzloDataUpdateCoordinator(hass, entry, connection)
        connection.start()
        runtime.coordinators.append(coordinator)

    async def _wait_ready(coordinator: EzloDataUpdateCoordinator) -> None:
        try:
            await coordinator.connection.async_wait_ready()
        except Exception:  # noqa: BLE001 - a hub not ready at startup is fine
            _LOGGER.warning(
                "ezloPi hub %s not ready at startup; it will appear once connected",
                coordinator.serial,
            )

    try:
        await asyncio.wait_for(
            asyncio.gather(*(_wait_ready(c) for c in runtime.coordinators)),
            timeout=_SETUP_READY_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError):
        _LOGGER.warning(
            "ezloPi: not all hubs delivered data within %ss; continuing setup",
            _SETUP_READY_TIMEOUT,
        )

    for coordinator in runtime.coordinators:
        await coordinator.async_config_entry_first_refresh()

    # One HA device per controller (keyed by serial) now carries every entity.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_stale_devices(hass, entry, runtime)
    _async_remove_migrated_strip_entities(hass, entry, runtime)
    return True


def _async_remove_migrated_strip_entities(
    hass: HomeAssistant, entry: EzloConfigEntry, runtime: EzloRuntimeData
) -> None:
    """Drop LED-strip entities that moved platform/domain across versions.

    Some strip items were exposed one way in an earlier release and another way
    now; on upgrade the old entity is orphaned. Each ``(domain, unique_id)`` is
    removed only in the domain it used to live in, so the item's current entity
    (which reuses the same item id in a different domain) is left untouched:

    * 1.4.0: the power-on indicator (``boot_bri`` + ``boot_rgbcolor``) was five
      ``number`` entities; 1.6.0 makes it a config-category light.
    * 1.5.0: the ``effect`` item fell through to a ``sensor``; 1.6.0 folds it
      into the strip light (effect selector) + ``speed``/``intensity`` numbers.
    """
    stale: set[tuple[str, str]] = set()
    for coordinator in runtime.coordinators:
        serial = coordinator.serial
        for element in coordinator.data.values():
            item_id = element.get("id")
            item_type = element.get("type")
            if item_type == "config_light":
                stale.add(("number", f"{serial}_{item_id}"))
            elif item_type == "config_light_color":
                for channel in ("red", "green", "blue", "cwhite"):
                    stale.add(("number", f"{serial}_{item_id}_{channel}"))
            elif item_type == "light_effect":
                stale.add(("sensor", f"{serial}_{item_id}"))
    if not stale:
        return

    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if (entity.domain, entity.unique_id) in stale:
            entity_registry.async_remove(entity.entity_id)


def _async_remove_stale_devices(
    hass: HomeAssistant, entry: EzloConfigEntry, runtime: EzloRuntimeData
) -> None:
    """Drop registry devices that aren't a current controller device.

    The integration now uses one device per controller (identifier = serial).
    This removes leftovers from older versions: the per-end-device devices
    (``serial_deviceId``) and the separate ``serial_controller`` firmware device,
    which have no entities anymore now that everything lives on ``serial``.
    """
    valid = {coordinator.serial for coordinator in runtime.coordinators}

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        idents = [i for (d, i) in device.identifiers if d == DOMAIN]
        if not idents or idents[0] in valid:
            continue
        # Any ezlo device whose identifier isn't a known controller serial is a
        # stale old-scheme device (per-end-device or _controller) — remove it.
        device_registry.async_remove_device(device.id)


async def async_unload_entry(hass: HomeAssistant, entry: EzloConfigEntry) -> bool:
    """Tear down the entry: stop hub connections and unload platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime: EzloRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None:
        for coordinator in runtime.coordinators:
            await coordinator.connection.async_stop()
    return unloaded
