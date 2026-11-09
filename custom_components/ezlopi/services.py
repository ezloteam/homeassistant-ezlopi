"""Custom services for the ezloPi integration.

The device-0009 LED strip exposes a writable ``canvas`` framebuffer item
(``{"start": <pixel index>, "rgb": "<hex>"}``, 6 hex chars — RRGGBB — per pixel
in chain/row-major order). Writing it blits that pixel run and puts the strip
into the on-device "image" effect. The integration otherwise surfaces ``canvas``
only as a read-only sensor, so without these services the "image" effect is
unusable from Home Assistant (nothing can push pixels).

Two services bridge that gap:

* ``ezlopi.set_canvas`` — the primitive: write one ``{start, rgb}`` run to the
  strip's canvas. Call it repeatedly to draw an arbitrary framebuffer.
* ``ezlopi.draw_image`` — convenience: load an image file, resize it to the
  strip geometry (read from the canvas sensor) and blit it in chunks.

The firmware deliberately sends no value-updated broadcast on a canvas write
(it keeps the draw path fast), so writes are fire-and-forget — there is no state
echo to wait on. A canvas write also floors brightness to a visible level if it
was 0, so the image is never hidden; set the light ``effect`` back to e.g.
``solid`` to leave image mode afterwards.
"""
from __future__ import annotations

import ast
import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_CANVAS = "set_canvas"
SERVICE_DRAW_IMAGE = "draw_image"

ATTR_START = "start"
ATTR_RGB = "rgb"
ATTR_IMAGE = "image"

# The firmware item name of the strip framebuffer (classified as a plain sensor
# because its name matches no light/number rule — see device_types).
CANVAS_ITEM_NAME = "canvas"

# Pixels per canvas write. Each pixel is 6 hex chars, so keep every blit well
# inside a single ws frame on large strips; the bench 42px strip is one chunk.
_CHUNK_PIXELS = 256


def _valid_hex(value: str) -> str:
    """Validate an RGB hex payload: whole pixels (6 hex chars each).

    Spaces between pixels are ignored. ``bytes.fromhex`` (not ``int(…, 16)``)
    does the hex check so a ``0x`` prefix, a sign, or underscores are rejected
    rather than silently passed to the firmware.
    """
    cleaned = "".join(value.split()).lower()
    if not cleaned or len(cleaned) % 6 != 0:
        raise vol.Invalid(
            "rgb must be whole pixels: 6 hex chars (RRGGBB) per pixel"
        )
    try:
        bytes.fromhex(cleaned)
    except ValueError as err:
        raise vol.Invalid("rgb must be hexadecimal (0-9a-f)") from err
    return cleaned


SET_CANVAS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_START, default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Required(ATTR_RGB): vol.All(cv.string, _valid_hex),
    }
)

DRAW_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_IMAGE): cv.string,
    }
)


def _geometry(canvas_element: dict[str, Any]) -> tuple[int, int]:
    """Pixel (width, height) of the strip from the canvas sensor value.

    The firmware reports the value as a dict (or its ``repr`` string, e.g.
    ``"{'width':42,'height':1,'pixels':42}"``). A plain strip reports
    ``width == 0``; fall back to ``pixels`` as a single row. Returns ``(0, 0)``
    when the geometry can't be determined.
    """
    raw = canvas_element.get("value")
    data: dict[str, Any] | None = None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
    if not data:
        return 0, 0
    try:
        width = int(data.get("width") or 0)
        height = int(data.get("height") or 0)
        pixels = int(data.get("pixels") or 0)
    except (ValueError, TypeError):
        return 0, 0  # a non-numeric geometry field — treat as unknown
    if width > 0 and height > 0:
        return width, height
    if pixels > 0:
        return pixels, 1
    return 0, 0


def _loaded_coordinators(hass: HomeAssistant) -> list[Any]:
    """Every coordinator across the loaded ezloPi config entries."""
    coordinators: list[Any] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            coordinators.extend(runtime.coordinators)
    return coordinators


def _canvas_target(
    coordinators: list[Any], unique_id: str
) -> tuple[Any, str, tuple[int, int]] | None:
    """Resolve a targeted entity's unique id to its hub's canvas item.

    Returns ``(connection, canvas_item_id, (width, height))`` or ``None`` if no
    coordinator owns the entity. The canvas item on the SAME physical device as
    the target is preferred, so targeting either the strip's light entity or its
    canvas sensor works. Raises :class:`HomeAssistantError` when the owning hub
    has no canvas item, or has several and the target doesn't disambiguate.
    """
    for coordinator in coordinators:
        target: dict[str, Any] | None = None
        for item_id, element in coordinator.data.items():
            if f"{coordinator.serial}_{item_id}" == unique_id:
                target = element
                break
        if target is None:
            continue
        canvases = [
            e for e in coordinator.data.values() if e.get("name") == CANVAS_ITEM_NAME
        ]
        same_device = [
            e for e in canvases if e.get("deviceId") == target.get("deviceId")
        ]
        chosen = same_device or canvases
        if not chosen:
            raise HomeAssistantError(
                f"this hub has no '{CANVAS_ITEM_NAME}' item "
                "(needs a device-0009 LED strip)"
            )
        if len(chosen) > 1:
            raise HomeAssistantError(
                f"multiple '{CANVAS_ITEM_NAME}' items on this hub; "
                "target the specific canvas sensor entity"
            )
        canvas = chosen[0]
        return coordinator.connection, str(canvas["id"]), _geometry(canvas)
    return None


def _resolve(hass: HomeAssistant, entity_id: str) -> tuple[Any, str, tuple[int, int]]:
    """Map a targeted entity id to ``(connection, canvas_item_id, geometry)``."""
    registry = er.async_get(hass)
    reg_entry = registry.async_get(entity_id)
    if reg_entry is None or reg_entry.platform != DOMAIN:
        raise HomeAssistantError(f"{entity_id} is not an ezloPi entity")
    result = _canvas_target(_loaded_coordinators(hass), reg_entry.unique_id)
    if result is None:
        raise HomeAssistantError(
            f"could not locate the ezloPi hub for {entity_id} "
            "(its config entry may not be loaded)"
        )
    return result


def _resolve_targets(
    hass: HomeAssistant, entity_ids: list[str]
) -> list[tuple[Any, str, tuple[int, int]]]:
    """Resolve targeted entities to the distinct canvases they map to.

    Targeting both a strip's light and its canvas sensor resolves to the same
    canvas item; de-duplicating by ``(connection, canvas_id)`` keeps that a
    single write (and, for ``draw_image``, a single encode).
    """
    seen: set[tuple[int, str]] = set()
    targets: list[tuple[Any, str, tuple[int, int]]] = []
    for entity_id in entity_ids:
        connection, canvas_id, geometry = _resolve(hass, entity_id)
        key = (id(connection), canvas_id)
        if key in seen:
            continue
        seen.add(key)
        targets.append((connection, canvas_id, geometry))
    return targets


def _encode_image(path: str, width: int, height: int) -> str:
    """Resize an image to ``width``x``height`` and return row-major RRGGBB hex.

    Runs in the executor (blocking file IO + decode). Pillow ships with Home
    Assistant core; the import is guarded so a missing install gives a clear
    error rather than an opaque ImportError.
    """
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415 - resolved at call time
    except ImportError as err:  # pragma: no cover - Pillow is a HA core dep
        raise HomeAssistantError(
            "the Pillow library is required for ezlopi.draw_image but is not "
            "installed"
        ) from err
    try:
        with Image.open(path) as img:
            # exif_transpose honours the camera orientation tag; resize with
            # LANCZOS for a clean downscale to the (small) strip geometry.
            oriented = ImageOps.exif_transpose(img).convert("RGB")
            resized = oriented.resize((width, height), Image.Resampling.LANCZOS)
            # RGB bytes in row-major (left-to-right, top-down) order — exactly
            # the strip's chain order, so .hex() is the canvas payload directly.
            raw = resized.tobytes()
    except HomeAssistantError:
        raise
    except Exception as err:  # noqa: BLE001 - bad/oversized/undecodable image
        raise HomeAssistantError(f"could not read image {path}: {err}") from err
    return raw.hex()


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the ezloPi custom services (idempotent across config entries)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_CANVAS):
        return

    async def _write_chunk(
        connection: Any, canvas_id: str, start: int, rgb: str
    ) -> None:
        try:
            await connection.async_send_item_value(
                canvas_id, {"start": start, "rgb": rgb}
            )
        except HomeAssistantError:
            raise
        except Exception as err:  # noqa: BLE001 - surface as a service error
            raise HomeAssistantError(f"canvas write failed: {err}") from err

    async def _handle_set_canvas(call: ServiceCall) -> None:
        start: int = call.data[ATTR_START]
        rgb: str = call.data[ATTR_RGB]
        run_pixels = len(rgb) // 6
        for connection, canvas_id, (width, height) in _resolve_targets(
            hass, call.data[ATTR_ENTITY_ID]
        ):
            # When the strip geometry is known, reject a run that would overrun
            # it (the firmware may not clamp). Unknown geometry (count == 0) is
            # left to the firmware.
            count = width * height
            if count and start + run_pixels > count:
                raise HomeAssistantError(
                    f"pixels {start}..{start + run_pixels} exceed the strip's "
                    f"{count} pixels"
                )
            await _write_chunk(connection, canvas_id, start, rgb)

    async def _handle_draw_image(call: ServiceCall) -> None:
        image: str = call.data[ATTR_IMAGE]
        if not hass.config.is_allowed_path(image):
            raise HomeAssistantError(
                f"{image} is not in an allowed path "
                "(add its directory to allowlist_external_dirs)"
            )
        for connection, canvas_id, (width, height) in _resolve_targets(
            hass, call.data[ATTR_ENTITY_ID]
        ):
            if width <= 0 or height <= 0:
                raise HomeAssistantError(
                    "canvas geometry unknown; cannot size the image"
                )
            count = width * height
            hexdata = await hass.async_add_executor_job(
                _encode_image, image, width, height
            )
            for offset in range(0, count, _CHUNK_PIXELS):
                chunk = hexdata[offset * 6 : (offset + _CHUNK_PIXELS) * 6]
                await _write_chunk(connection, canvas_id, offset, chunk)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_CANVAS, _handle_set_canvas, schema=SET_CANVAS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DRAW_IMAGE, _handle_draw_image, schema=DRAW_IMAGE_SCHEMA
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the ezloPi custom services (called when the last entry unloads)."""
    hass.services.async_remove(DOMAIN, SERVICE_SET_CANVAS)
    hass.services.async_remove(DOMAIN, SERVICE_DRAW_IMAGE)
