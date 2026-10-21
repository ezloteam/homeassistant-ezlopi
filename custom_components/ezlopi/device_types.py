"""Device type mapping and detection for ezloPi devices."""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Complete device type mapping registry
DEVICE_TYPE_MAP: dict[str, dict[str, Any]] = {
    # Motion sensors
    "sensor.motion": {
        "platform": "binary_sensor",
        "device_class": "motion",
        "description": "Motion detection sensor"
    },
    
    # Door/Window sensors
    "sensor.door": {
        "platform": "binary_sensor", 
        "device_class": "door",
        "description": "Door sensor"
    },
    "sensor.window": {
        "platform": "binary_sensor",
        "device_class": "window",
        "description": "Window sensor"
    },
    
    # Security devices
    "doorlock": {
        "platform": "lock",
        "device_class": "lock",
        "description": "Smart door lock",
        "additional_entities": [
            {"platform": "sensor", "item_type": "battery"},
            {"platform": "binary_sensor", "item_type": "tamper"}
        ]
    },
    
    # Lighting devices
    "dimmer.outlet": {
        "platform": "light",
        "device_class": "light",
        "description": "Dimmable outlet"
    },
    "dimmer.inwall": {
        "platform": "light",
        "device_class": "light",
        "description": "In-wall dimmer"
    },
    
    # Switch devices
    "switch.outlet": {
        "platform": "switch",
        "device_class": "outlet",
        "description": "Power outlet switch"
    },
    "switch.inwall": {
        "platform": "switch",
        "device_class": "switch",
        "description": "In-wall switch"
    },
    
    # Climate devices
    "thermostat": {
        "platform": "climate",
        "device_class": "thermostat",
        "description": "HVAC thermostat"
    },
    
    # Remote control
    "transmitter.ir": {
        "platform": "remote",
        "device_class": "remote",
        "description": "Infrared transmitter"
    },
    
    # Generic types
    "device": {
        "platform": "sensor",
        "device_class": None,
        "description": "Generic device"
    },
    "sensor": {
        "platform": "sensor",
        "device_class": None,
        "description": "Generic sensor"
    }
}

# Item name patterns for detection
ITEM_NAME_PATTERNS: dict[str, dict[str, Any]] = {
    # Binary sensor patterns
    "motion": {"platform": "binary_sensor", "device_class": "motion"},
    "door": {"platform": "binary_sensor", "device_class": "door"},
    "window": {"platform": "binary_sensor", "device_class": "window"},
    "tamper": {"platform": "binary_sensor", "device_class": "tamper"},
    "smoke": {"platform": "binary_sensor", "device_class": "smoke"},
    "presence": {"platform": "binary_sensor", "device_class": "presence"},
    "occupancy": {"platform": "binary_sensor", "device_class": "occupancy"},
    "water": {"platform": "binary_sensor", "device_class": "moisture"},
    "leak": {"platform": "binary_sensor", "device_class": "moisture"},
    
    # Light patterns
    "dimmer": {"platform": "light", "device_class": "light"},
    "brightness": {"platform": "light", "device_class": "light"},
    
    # Lock patterns
    "lock": {"platform": "lock", "device_class": "lock"},
    "deadbolt": {"platform": "lock", "device_class": "lock"},
    
    # Climate patterns
    "thermostat": {"platform": "climate", "device_class": "thermostat"},
    "hvac": {"platform": "climate", "device_class": "thermostat"},
    "air_conditioner": {"platform": "climate", "device_class": "thermostat"},
    "heater": {"platform": "climate", "device_class": "thermostat"},
    
    # Sensor patterns
    "temperature": {"platform": "sensor", "device_class": "temperature"},
    "humidity": {"platform": "sensor", "device_class": "humidity"},
    "battery": {"platform": "sensor", "device_class": "battery"},
    "power": {"platform": "sensor", "device_class": "power"},
    "energy": {"platform": "sensor", "device_class": "energy"},
    "voltage": {"platform": "sensor", "device_class": "voltage"},
    "current": {"platform": "sensor", "device_class": "current"},
}

# Exact item-name config items (device-0009 LED strip tunables). These are
# NVS-persisted settings the firmware exposes as settable items; they must be
# classified explicitly so they do not fall through to the value-type rules
# (e.g. a small led_count would otherwise look like a 0-100 "light" brightness,
# and boot_rgbcolor would be merged into the runtime light as its colour).
CONFIG_ITEM_NAMES: dict[str, dict[str, Any]] = {
    "led_count": {
        "platform": "number", "device_class": None, "entity_category": "config",
        "native_min": 1, "native_max": 1024, "native_step": 1, "mode": "box",
        "icon": "mdi:led-strip-variant",
    },
    "boot_bri": {
        "platform": "number", "device_class": None, "entity_category": "config",
        "native_min": 0, "native_max": 255, "native_step": 1, "mode": "slider",
        "icon": "mdi:brightness-6",
    },
    "boot_rgbcolor": {
        "platform": "number_color", "device_class": None, "entity_category": "config",
    },
}

# Category to platform mapping
CATEGORY_MAP: dict[str, dict[str, Any]] = {
    "security": {
        "default_platform": "binary_sensor",
        "subcategories": {
            "door": {"platform": "binary_sensor", "device_class": "door"},
            "window": {"platform": "binary_sensor", "device_class": "window"},
            "motion": {"platform": "binary_sensor", "device_class": "motion"},
            "door_lock": {"platform": "lock", "device_class": "lock"}
        }
    },
    "light": {
        "default_platform": "light",
        "subcategories": {
            "dimmer": {"platform": "light", "device_class": "light"},
            "switch": {"platform": "switch", "device_class": "switch"}
        }
    },
    "temperature": {
        "default_platform": "sensor",
        "device_class": "temperature"
    },
    "humidity": {
        "default_platform": "sensor",
        "device_class": "humidity"
    },
    "switch": {
        "default_platform": "switch",
        "subcategories": {
            "outlet": {"platform": "switch", "device_class": "outlet"},
            "electricity": {"platform": "switch", "device_class": "switch"}
        }
    },
    "climate": {
        "default_platform": "climate",
        "device_class": "thermostat"
    }
}


def detect_device_platform(device_info: dict[str, Any]) -> dict[str, Any]:
    """
    Detect the appropriate platform and device class for a device.
    
    Args:
        device_info: Dictionary containing device information from ezloPi
        
    Returns:
        Dictionary with platform and device_class keys
    """
    # 0. Exact config item names (LED-strip tunables) win over everything, so
    # they are never misread as a light/sensor by the generic rules below.
    name_exact = device_info.get("name") or ""
    if name_exact in CONFIG_ITEM_NAMES:
        result = CONFIG_ITEM_NAMES[name_exact].copy()
        _LOGGER.debug(f"Config item detected by name '{name_exact}': {result}")
        return result

    # 1. Check explicit device type first
    device_type = device_info.get("deviceType") or device_info.get("device_type")
    if device_type and device_type in DEVICE_TYPE_MAP:
        result = DEVICE_TYPE_MAP[device_type].copy()
        _LOGGER.debug(f"Device detected by type '{device_type}': {result}")
        return result
    
    # 2. Check category and subcategory
    category = device_info.get("category")
    subcategory = device_info.get("subcategory")
    
    if category and category in CATEGORY_MAP:
        cat_info = CATEGORY_MAP[category]
        
        # Check subcategory first
        if subcategory and "subcategories" in cat_info:
            if subcategory in cat_info["subcategories"]:
                sub: dict[str, Any] = cat_info["subcategories"][subcategory]
                result = sub.copy()
                _LOGGER.debug(f"Device detected by category/subcategory '{category}/{subcategory}': {result}")
                return result
        
        # Use default for category
        result = {
            "platform": cat_info.get("default_platform", "sensor"),
            "device_class": cat_info.get("device_class")
        }
        _LOGGER.debug(f"Device detected by category '{category}': {result}")
        return result
    
    # 3. Check item name patterns. Match only against the item name — NOT the
    # device name: a device called "Frankever Dimmer" must not turn its on/off
    # item into a light just because the device name contains "dimmer".
    item_name = (device_info.get("name") or "").lower()

    for pattern, mapping in ITEM_NAME_PATTERNS.items():
        if pattern in item_name:
            result = mapping.copy()
            _LOGGER.debug(f"Device detected by name pattern '{pattern}': {result}")
            return result
    
    # 4. Fall back to value type detection
    return detect_by_value_type(device_info)


def detect_by_value_type(device_info: dict[str, Any]) -> dict[str, Any]:
    """
    Detect platform based on value type (fallback method).
    
    Args:
        device_info: Dictionary containing device information
        
    Returns:
        Dictionary with platform and device_class keys
    """
    value_type = device_info.get("valueType", "")
    has_setter = device_info.get("hasSetter", False)
    
    # Check for specific value types
    if value_type == "bool":
        # Boolean with setter is likely a switch
        if has_setter:
            return {"platform": "switch", "device_class": "switch"}
        else:
            # Boolean without setter is likely a binary sensor
            return {"platform": "binary_sensor", "device_class": None}
    
    elif value_type == "temperature":
        return {"platform": "sensor", "device_class": "temperature"}
    
    elif value_type == "humidity":
        return {"platform": "sensor", "device_class": "humidity"}
    
    elif value_type == "battery":
        return {"platform": "sensor", "device_class": "battery"}

    elif value_type == "rgb":
        # RGB(W) color is a facet of the light, merged into the light entity
        # (see light._setup_lights) — not its own standalone entity.
        return {"platform": "light_color", "device_class": None}

    elif value_type in ["int", "float", "string", "token", "dictionary"]:
        # Check if it's a dimmer based on scale/range
        if has_setter and value_type in ["int", "float"]:
            scale = device_info.get("scale")
            if scale == "percent" or (0 <= device_info.get("value", 0) <= 100):
                return {"platform": "light", "device_class": "light"}
        
        return {"platform": "sensor", "device_class": None}
    
    # Default fallback
    _LOGGER.warning(f"Unknown value type '{value_type}', defaulting to sensor")
    return {"platform": "sensor", "device_class": None}


def get_additional_entities(device_type: str) -> list[Any]:
    """
    Get additional entities that should be created for a device type.
    
    Args:
        device_type: The device type string
        
    Returns:
        List of additional entity configurations
    """
    if device_type in DEVICE_TYPE_MAP:
        entities: list[Any] = DEVICE_TYPE_MAP[device_type].get("additional_entities", [])
        return entities
    return []


def should_create_multiple_entities(device_info: dict[str, Any]) -> bool:
    """
    Check if a device should create multiple entities.
    
    Args:
        device_info: Dictionary containing device information
        
    Returns:
        True if device needs multiple entities
    """
    device_type = device_info.get("deviceType") or device_info.get("device_type")
    if device_type:
        additional = get_additional_entities(device_type)
        return len(additional) > 0
    return False