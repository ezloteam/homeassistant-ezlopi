from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.zeroconf import async_get_instance
from homeassistant.core import HomeAssistant
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

_LOGGER = logging.getLogger(__name__)

class EzloPiMDSConnector(ServiceListener):
    def __init__(self, service: str = '_ezlo', proto: str = '_tcp') -> None:
        self.service = service
        self.proto = proto
        self.type = f'{self.service}.{self.proto}.local.'
        self.zc: Zeroconf | None = None  # Zeroconf instance assigned later
        self.service_dict: dict[str, dict[str, Any]] = {}
        self.service_size = 0

    async def async_init(self, hass: HomeAssistant) -> None:
        self.zc = await async_get_instance(hass)


    # A service appearing and a service changing both carry the current info we
    # need (a hub re-announces via update_service, not just add_service), so
    # both must (re)store it — otherwise a hub seen only via update is missed.
    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._store_service(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._store_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.service_dict.pop(name, None)
        _LOGGER.debug("ezlo mDNS service %s removed", name)

    def _store_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is None:
            return
        info_dict: dict[str, Any] = dict()
        info_dict['type'] = info.type
        info_dict['name'] = info.name
        info_dict['address'] = info.parsed_addresses()
        info_dict['port'] = info.port
        info_dict['server'] = info.server
        info_dict['properties'] = {
            key.decode('utf-8'): value.decode('utf-8')
            for key, value in info.properties.items()
            if value is not None
        }
        info_dict['interface_index'] = info.interface_index
        self.service_dict[name] = info_dict
        _LOGGER.debug("ezlo mDNS service %s stored", name)

    async def async_get_service_info(self) -> None:
        await asyncio.sleep(2)
        _LOGGER.info("Autodiscover devices...")
        assert self.zc is not None
        self.browser = ServiceBrowser(zc=self.zc, type_=self.type, listener=self)

        try:
            for i in range(1, 3):
                _LOGGER.info(f"Progress: {i * 10}%")
                await asyncio.sleep(1)
        finally:
            _LOGGER.info("Service discovery complete.")

    def get_serial(self, data: dict[str, Any]) -> str:
        properties = data.get('properties', {})
        serial = properties.get('Serial')

        if not serial:
            raise KeyError("Key 'Serial' is missing in 'properties'.")

        return str(serial)

    def get_connection_link_from_serial(self, serial: str | None = None) -> str | None:
        if serial is None:
            raise ValueError('Expected serial but got None')

        for key, value in self.service_dict.items():
            # A hub mid-reboot can briefly advertise an incomplete record (no
            # Serial TXT yet, or no resolved address). Skip such an entry rather
            # than let it abort resolution for every hub — this method is called
            # from the connection supervisor, and a raise here would kill it.
            try:
                if self.get_serial(value) == serial:
                    return f'ws://{value["address"][0]}:{value["port"]}'
            except (KeyError, IndexError):
                continue

        _LOGGER.debug("Hub %s not found on the local network (mDNS)", serial)
        return None

    def get_connection_links(self) -> dict[str, str]:
        connection_links: dict[str, str] = {}
        for key, value in self.service_dict.items():
            try:
                serial = self.get_serial(value)
                connection_links[serial] = f'ws://{value["address"][0]}:{value["port"]}'
            except (KeyError, IndexError):
                continue
        return connection_links