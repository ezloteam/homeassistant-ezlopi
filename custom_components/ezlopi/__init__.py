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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .connection import EzloHubConnection
from .const import DOMAIN
from .coordinator import EzloDataUpdateCoordinator
from .ezlopi_utils import EzloAuthError, EzloCloudAPI
from .mdns_connector import EzloPiMDSConnector

_LOGGER = logging.getLogger(__name__)

DATA_BROWSER = "browser"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class EzloRuntimeData:
    """Per-config-entry runtime state, stored on ConfigEntry.runtime_data."""

    api: EzloCloudAPI
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

    runtime = EzloRuntimeData(api=api)
    entry.runtime_data = runtime

    for hub in api.get_hub_list().values():
        connection = EzloHubConnection(
            hass, session, browser, hub.serial, hub.token,
            on_update=lambda: None,
            jwt_token=api.token,
            legacy_auth=api.legacy_auth,
            legacy_sig=api.legacy_sig,
            nma_url=hub.nma_url,
        )
        coordinator = EzloDataUpdateCoordinator(hass, entry, connection)
        connection.start()
        try:
            await connection.async_wait_ready()
        except (TimeoutError, asyncio.TimeoutError):
            # Hub is on the account but not reachable on the LAN right now. Its
            # connection keeps retrying in the background and its entities are
            # added when it connects, so don't fail the whole entry for it.
            _LOGGER.warning(
                "ezloPi hub %s is not reachable on the local network yet", hub.serial
            )
        await coordinator.async_config_entry_first_refresh()
        runtime.coordinators.append(coordinator)

    # Devices are the physical end-devices (created from each entity's
    # device_info), not the controllers — so no hub device is registered here.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_stale_devices(hass, entry, runtime)
    return True


def _async_remove_stale_devices(
    hass: HomeAssistant, entry: EzloConfigEntry, runtime: EzloRuntimeData
) -> None:
    """Drop registry devices that no longer correspond to a current ezlo device.

    Covers leftover hub/controller devices from older versions and physical
    devices that have disappeared from a hub.
    """
    valid: set[str] = set()
    connected_serials: set[str] = set()
    for coordinator in runtime.coordinators:
        if not coordinator.data:
            continue  # offline hub — leave its devices alone, they may return
        connected_serials.add(coordinator.serial)
        for element in coordinator.data.values():
            device_id = element.get("deviceId") or element["id"]
            valid.add(f"{coordinator.serial}_{device_id}")

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        idents = [i for (d, i) in device.identifiers if d == DOMAIN]
        if not idents or idents[0] in valid:
            continue
        ident = idents[0]
        # Remove legacy controller devices (bare serial, no "_") and physical
        # devices a connected hub no longer reports.
        is_legacy_hub = "_" not in ident
        if is_legacy_hub or ident.split("_")[0] in connected_serials:
            device_registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_unload_entry(hass: HomeAssistant, entry: EzloConfigEntry) -> bool:
    """Tear down the entry: stop hub connections and unload platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime: EzloRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None:
        for coordinator in runtime.coordinators:
            await coordinator.connection.async_stop()
    return unloaded
