"""Tests for the Ezlo cloud API client (login + controller listing)."""
from typing import Any

import aiohttp
import pytest

from custom_components.ezlopi.ezlopi_utils import (
    EzloAuthError,
    EzloCloudAPI,
    EzloConnectionError,
    compute_nma_url,
)


def test_compute_nma_url_cloud_host() -> None:
    # The live "-cloud.ezlo.com" host maps to its "-ui-cloud" client variant.
    assert compute_nma_url("nma-server8-cloud.ezlo.com:443", None) == (
        "wss://nma-server8-ui-cloud.ezlo.com:443/nma"
    )


def test_compute_nma_url_oem_host() -> None:
    # Hosts without "-cloud" get "-ui-cloud" inserted before ".ezlo.com".
    assert compute_nma_url("nma-server21-ezlo-security.ezlo.com", None) == (
        "wss://nma-server21-ezlo-security-ui-cloud.ezlo.com/nma"
    )


def test_compute_nma_url_prefers_explicit_controller_url() -> None:
    assert compute_nma_url(
        "nma-server8-cloud.ezlo.com", [{"url": "wss://relay.example.com"}]
    ) == "wss://relay.example.com/nma"


def test_compute_nma_url_none_without_host() -> None:
    assert compute_nma_url(None, None) is None


class _Resp:
    def __init__(self, status: int, payload: dict | None = None,
                 raise_client: bool = False) -> None:
        self.status = status
        self._payload = payload or {}
        self._raise = raise_client

    async def __aenter__(self) -> "_Resp":
        if self._raise:
            raise aiohttp.ClientError("boom")
        return self

    async def __aexit__(self, *args: Any) -> None:
        return

    async def json(self) -> dict:
        return self._payload


class _Session:
    """Returns queued responses for successive post()/get() calls."""

    def __init__(self, *responses: _Resp) -> None:
        self._responses = list(responses)

    def _next(self) -> _Resp:
        return self._responses.pop(0)

    def post(self, *args: Any, **kwargs: Any) -> _Resp:
        return self._next()

    def get(self, *args: Any, **kwargs: Any) -> _Resp:
        return self._next()


def _api(*responses: _Resp) -> EzloCloudAPI:
    return EzloCloudAPI("user", "pass", _Session(*responses))  # type: ignore[arg-type]


async def test_fetch_hub_list_success() -> None:
    api = _api(
        _Resp(200, {"token": "jwt", "legacy_token": {"auth": "A", "sig": "S"}}),
        _Resp(200, {"controllers": [
            {"serial": "111", "uuid": "u", "name": "Hub", "local_key": "lk"},
            {"serial": "", "uuid": "x", "name": "skip"},  # blank serial ignored
        ]}),
        # v1 enrichment: controller has no nma_host in v4, so a v1 lookup runs.
        _Resp(200, {"data": {"controllers": [
            {"serial": "111", "nma_host": "nma-server21-ezlo-security.ezlo.com:443"},
        ]}}),
    )
    assert await api.fetch_hub_list() is True
    hubs = api.get_hub_list()
    assert list(hubs) == ["111"]
    assert hubs["111"].token == "lk"
    # legacy MMS pair captured for the NMA handshake
    assert (api.legacy_auth, api.legacy_sig) == ("A", "S")
    # v1 nma_host transformed to the -ui-cloud wss endpoint
    assert hubs["111"].nma_url == (
        "wss://nma-server21-ezlo-security-ui-cloud.ezlo.com:443/nma"
    )


async def test_fetch_hub_list_nma_host_from_v4() -> None:
    """When v4 already includes nma_host, no v1 enrichment call is made."""
    api = _api(
        _Resp(200, {"token": "jwt"}),
        _Resp(200, {"controllers": [
            {"serial": "111", "name": "Hub", "local_key": "lk",
             "nma_host": "nma-server9-ezlo.ezlo.com"},
        ]}),
        # no third response queued -> IndexError if enrichment wrongly runs
    )
    assert await api.fetch_hub_list() is True
    assert api.get_hub_list()["111"].nma_url == (
        "wss://nma-server9-ezlo-ui-cloud.ezlo.com/nma"
    )


async def test_login_invalid_credentials() -> None:
    with pytest.raises(EzloAuthError):
        await _api(_Resp(200, {})).fetch_hub_list()


async def test_login_rejected_status() -> None:
    with pytest.raises(EzloAuthError):
        await _api(_Resp(401)).fetch_hub_list()


async def test_login_server_error() -> None:
    with pytest.raises(EzloConnectionError):
        await _api(_Resp(500)).fetch_hub_list()


async def test_login_client_error() -> None:
    with pytest.raises(EzloConnectionError):
        await _api(_Resp(200, raise_client=True)).fetch_hub_list()


async def test_controller_list_no_controllers() -> None:
    api = _api(_Resp(200, {"token": "jwt"}), _Resp(200, {"controllers": []}))
    assert await api.fetch_hub_list() is False


async def test_controller_list_http_error() -> None:
    api = _api(_Resp(200, {"token": "jwt"}), _Resp(503))
    assert await api.fetch_hub_list() is False


async def test_controller_list_client_error() -> None:
    api = _api(_Resp(200, {"token": "jwt"}), _Resp(200, raise_client=True))
    with pytest.raises(EzloConnectionError):
        await api.fetch_hub_list()
