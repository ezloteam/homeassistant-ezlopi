import aiohttp
import asyncio
import logging
from typing import Any, Dict
from .const import (
    EZLOPI_API_URL_BASE,
    EZLOPI_CONTROLLER_LIST_URL,
    EZLOPI_LOGIN_URL,
)

_LOGGER = logging.getLogger(__name__)


class EzloAuthError(Exception):
    """Credentials were rejected by the Ezlo cloud."""


class EzloConnectionError(Exception):
    """The Ezlo cloud could not be reached (transient/network failure)."""


def _ensure_nma_path(url: str) -> str:
    """Ensure an NMA websocket URL ends with the required ``/nma`` path."""
    base = url.rstrip("/")
    if not base.endswith("/nma"):
        base += "/nma"
    return base


def compute_nma_url(
    nma_host: str | None, nma_controllers: list[dict[str, Any]] | None
) -> str | None:
    """Derive the client-facing NMA websocket URL for a controller.

    Prefer an explicit ``nma_controllers[].url`` if the cloud provides one,
    otherwise transform the raw ``nma_host`` into its client-facing
    ``-ui-cloud`` variant (the raw host's certificate uses a secp256k1 CA some
    clients can't validate; the ``-ui-cloud`` host has a normal cert). The
    account's hosts come in two shapes:
      * ``nma-serverN-cloud.ezlo.com``  -> ``nma-serverN-ui-cloud.ezlo.com``
      * ``nma-serverN-<oem>.ezlo.com``  -> ``nma-serverN-<oem>-ui-cloud.ezlo.com``
    """
    if nma_controllers:
        url = nma_controllers[0].get("url")
        if url:
            return _ensure_nma_path(str(url))
    if not nma_host:
        return None
    host = str(nma_host)
    if "-ui-cloud.ezlo.com" not in host:
        if "-cloud.ezlo.com" in host:
            # e.g. "nma-server8-cloud.ezlo.com:443"
            #   -> "nma-server8-ui-cloud.ezlo.com:443"
            host = host.replace("-cloud.ezlo.com", "-ui-cloud.ezlo.com")
        else:
            # e.g. "nma-server21-ezlo-security.ezlo.com:443"
            #   -> "nma-server21-ezlo-security-ui-cloud.ezlo.com:443"
            suffix = ".ezlo.com"
            idx = host.rfind(suffix)
            if idx >= 0:
                host = host[:idx] + "-ui-cloud.ezlo.com" + host[idx + len(suffix):]
    return _ensure_nma_path("wss://" + host)


class EzloPIHubInfo:
    def __init__(
        self,
        serial: str,
        token: str | None,
        name: str,
        nma_url: str | None = None,
    ) -> None:
        self.serial = serial
        self.token = token
        self.name = name
        # Client-facing NMA websocket URL for remote (cloud-relayed) access,
        # used as a fallback when the hub can't be reached on the LAN. None
        # when the account/controller doesn't expose an NMA host.
        self.nma_url = nma_url

class EzloCloudAPI:
    def __init__(self, username: str, password: str, session: aiohttp.ClientSession) -> None:
        """Initialize with credentials and a shared (HA-injected) aiohttp session."""
        self.username = username
        self.password = password
        self._session = session
        self.token = None
        # Legacy MMS token pair from the v4 login response, forwarded to the
        # NMA broker's loginUserMios handshake. None until a successful login.
        self.legacy_auth: str | None = None
        self.legacy_sig: str | None = None
        self.hub_list: Dict[str, EzloPIHubInfo] = {}
        self._lock = asyncio.Lock()

    async def __login(self) -> bool:
        """
        Asynchronously login to the Ezlo Cloud API to retrieve a token.

        Uses the v4 REST login endpoint, which takes the credentials directly
        and returns the JWT at the top level ({"token": ...}). The minted JWT
        is accepted by the legacy /v1/request gateway for subsequent calls
        (e.g. controller_list).
        """
        login_url = EZLOPI_LOGIN_URL
        headers = {"Content-type": "application/json"}
        login_data = {
            "user_id": self.username,
            "user_password": self.password
        }

        try:
            async with self._session.post(login_url, headers=headers, json=login_data) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("token"):
                        self.token = result["token"]
                        # Capture the legacy MMS pair (used for NMA login) when
                        # present; absence just disables the NMA fallback.
                        legacy = result.get("legacy_token") or {}
                        self.legacy_auth = legacy.get("auth")
                        self.legacy_sig = legacy.get("sig")
                        _LOGGER.info("Login successful!")
                        return True
                    _LOGGER.error("Login failed: token not received")
                    raise EzloAuthError("No token in login response")
                if response.status in (400, 401, 403):
                    _LOGGER.error(f"Login rejected: HTTP {response.status}")
                    raise EzloAuthError(f"Credentials rejected (HTTP {response.status})")
                _LOGGER.error(f"Login failed: HTTP {response.status}")
                raise EzloConnectionError(f"Login failed (HTTP {response.status})")
        except aiohttp.ClientError as err:
            _LOGGER.error('login connection err:[' + str(err) + ']')
            raise EzloConnectionError(str(err)) from err
        

    async def fetch_hub_list(self) -> bool:
        """
        Asynchronously fetch the list of hubs associated with the account using the token.
        """
        await self.__login()
        if not self.token:
            _LOGGER.error("Token is missing. Please login first.")
            return False

        # v4 REST: GET with the JWT in x-access-token; controllers are returned
        # at the top level (no {"data": ...} envelope). `local_key=1` asks the
        # service to include the hub's local access key.
        controller_list_url = EZLOPI_CONTROLLER_LIST_URL + "?local_key=1"
        headers_with_token = {
            "Content-type": "application/json",
            "x-access-token": self.token
        }

        try:
            async with self._lock:
                async with self._session.get(controller_list_url, headers=headers_with_token) as response:
                    if response.status == 200:
                        result = await response.json()
                        if result.get("controllers"):
                            self.hub_list = {}
                            for hub in result["controllers"]:
                                serial = hub['serial']
                                if(serial != None and serial != ''):
                                    # Use the hub's local_key as the local
                                    # hub.offline.login.ui token — this is what
                                    # the firmware enforces once updated.
                                    token = hub.get('local_key')
                                    nma_url = compute_nma_url(
                                        hub.get('nma_host'),
                                        hub.get('nma_controllers'),
                                    )
                                    self.hub_list[serial] = EzloPIHubInfo(
                                        serial=serial, token=token,
                                        name=hub['name'], nma_url=nma_url,
                                    )
                            _LOGGER.info("Hub list fetched successfully:{}".format(self.hub_list))
                            # The v4 controller listing doesn't always include
                            # the NMA host; backfill from the v1 gateway so the
                            # remote fallback is available.
                            if any(h.nma_url is None for h in self.hub_list.values()):
                                await self._enrich_nma_urls()
                            return True
                        _LOGGER.error("Hub list not found in the response")
                    else:
                        _LOGGER.error(f"Failed to fetch hub list: HTTP {response.status}")
        except aiohttp.ClientError as err:
            _LOGGER.error('fetch hub list connection err:[' + str(err) + ']')
            raise EzloConnectionError(str(err)) from err
        return False

    async def _enrich_nma_urls(self) -> None:
        """Backfill each hub's NMA URL from the legacy v1 controller_list.

        The v1 ``/v1/request`` gateway returns ``nma_host`` per controller (the
        proven source hubcmd uses); the v4 REST listing may omit it. Best-effort
        — any failure just leaves the NMA fallback unavailable.
        """
        if not self.token:
            return
        headers = {
            "Content-type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        body = {"call": "controller_list", "params": {"local_key": 1}}
        try:
            async with self._session.post(
                EZLOPI_API_URL_BASE, headers=headers, json=body
            ) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "NMA host lookup failed: HTTP %s", response.status
                    )
                    return
                result = await response.json()
        except aiohttp.ClientError as err:
            _LOGGER.debug("NMA host lookup connection error: %s", err)
            return

        controllers = (result.get("data") or {}).get("controllers") or []
        by_serial = {
            c.get("serial"): c for c in controllers if c.get("serial")
        }
        for serial, hub in self.hub_list.items():
            if hub.nma_url is not None:
                continue
            controller = by_serial.get(serial)
            if not controller:
                continue
            hub.nma_url = compute_nma_url(
                controller.get("nma_host"), controller.get("nma_controllers")
            )

    def get_hub_list(self) -> Dict[str, EzloPIHubInfo]:
        """
        Return the list of hubs.
        """
        return self.hub_list
