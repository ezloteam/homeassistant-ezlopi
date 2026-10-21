"""Full setup/unload + diagnostics tests with the hub mocked at a low level."""
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezlopi.const import DOMAIN
from custom_components.ezlopi.diagnostics import async_get_config_entry_diagnostics
from custom_components.ezlopi.ezlopi_utils import EzloAuthError, EzloConnectionError

from .fixtures import SERIAL, FakeBrowser, FakeCloudAPI, FakeConnection

pytestmark = pytest.mark.real_setup

_DATA = {CONF_USERNAME: "user", CONF_PASSWORD: "pass"}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data=_DATA, unique_id="user")


def _patches():
    return (
        patch("custom_components.ezlopi.EzloPiMDSConnector", FakeBrowser),
        patch("custom_components.ezlopi.EzloHubConnection", FakeConnection),
        patch("custom_components.ezlopi.EzloCloudAPI", FakeCloudAPI),
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    with _patches()[0], _patches()[1], _patches()[2]:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_creates_entities_and_devices(hass: HomeAssistant) -> None:
    from homeassistant.helpers import entity_registry as er

    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED

    # One HA device per controller (keyed by serial) carries every entity.
    dev_reg = dr.async_get(hass)
    ctrl = dev_reg.async_get_device_by_identifier((DOMAIN, SERIAL), entry.entry_id)
    assert ctrl is not None
    assert ctrl.sw_version == "4.1.6"  # from hub.info.get
    # No per-end-device devices.
    entry_devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    identifiers = {ident for d in entry_devices for ident in d.identifiers}
    assert (DOMAIN, f"{SERIAL}_d_light") not in identifiers

    # The dimmer light exists, is on, and lives on the single controller device.
    ent_reg = er.async_get(hass)
    light_id = ent_reg.async_get_entity_id("light", DOMAIN, f"{SERIAL}_i_light")
    assert light_id is not None
    assert hass.states.get(light_id).state == "on"
    assert ent_reg.async_get(light_id).device_id == ctrl.id
    # The firmware update entity is on the SAME device.
    fw_id = ent_reg.async_get_entity_id("update", DOMAIN, f"{SERIAL}_firmware")
    assert fw_id is not None
    assert ent_reg.async_get(fw_id).device_id == ctrl.id


async def test_stale_devices_pruned(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    # Pre-seed leftover old-scheme devices (per-end-device + _controller).
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{SERIAL}_d_old")}, name="old end-device",
    )
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{SERIAL}_controller")}, name="old controller",
    )
    await _setup(hass, entry)
    # Old-scheme devices removed; the one controller device (serial) remains.
    eid = entry.entry_id
    assert dev_reg.async_get_device_by_identifier((DOMAIN, f"{SERIAL}_d_old"), eid) is None
    assert dev_reg.async_get_device_by_identifier((DOMAIN, f"{SERIAL}_controller"), eid) is None
    assert dev_reg.async_get_device_by_identifier((DOMAIN, SERIAL), eid) is not None


async def test_unload(hass: HomeAssistant) -> None:
    entry = _entry()
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics(hass: HomeAssistant) -> None:
    entry = _entry()
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    assert diag["hubs"][0]["item_count"] == 6
    assert diag["hubs"][0]["connected"] is True


async def test_setup_auth_failure_raises(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    class AuthFail(FakeCloudAPI):
        async def fetch_hub_list(self) -> bool:
            raise EzloAuthError("bad")

    with (
        patch("custom_components.ezlopi.EzloPiMDSConnector", FakeBrowser),
        patch("custom_components.ezlopi.EzloCloudAPI", AuthFail),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_connection_failure_retries(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    class NoHubs(FakeCloudAPI):
        def get_hub_list(self) -> dict:
            return {}

    with (
        patch("custom_components.ezlopi.EzloPiMDSConnector", FakeBrowser),
        patch("custom_components.ezlopi.EzloCloudAPI", NoHubs),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_cloud_connection_error_retries(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    class ConnFail(FakeCloudAPI):
        async def fetch_hub_list(self) -> bool:
            raise EzloConnectionError("down")

    with (
        patch("custom_components.ezlopi.EzloPiMDSConnector", FakeBrowser),
        patch("custom_components.ezlopi.EzloCloudAPI", ConnFail),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
