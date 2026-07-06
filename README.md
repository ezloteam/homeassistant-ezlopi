# ezloPi for Home Assistant

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ezloteam&repository=homeassistant-ezlopi&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ezlopi)

A custom [Home Assistant](https://www.home-assistant.io/) integration for
[ezloPi](https://ezlopi.com/) controllers. It logs in to your Ezlo cloud
account, discovers your controllers, and then talks to each one **locally** over
its websocket API — exposing the connected devices (dimmers, switches, sensors,
locks, thermostats) as Home Assistant entities with live, push-based updates.

## High-level description

- **Cloud login, local control.** Your Ezlo username/password are used once
  against the Ezlo cloud to enumerate the controllers on your account and fetch
  each one's local access key. All device state and control then flows over a
  direct LAN websocket to the controller — no cloud round-trip per command.
- **Push updates.** The integration keeps a persistent websocket open to each
  hub and updates entities the moment the hub broadcasts a change, rather than
  polling.
- **Local discovery.** Controllers are found on the network via mDNS
  (`_ezlo._tcp.local.`), so Home Assistant can offer to set them up
  automatically.

## Supported devices

Tested against ezloPi ESP32-based controllers (e.g. the Frankever dimmer module,
`ezlopi_frankever_us_wb01d-01b`). Any device a controller exposes as an *item* is
mapped to a Home Assistant entity by its type/category.

## Supported functions

| Home Assistant platform | ezloPi items |
|---|---|
| `light` | Dimmers (brightness 0–100 → 0–255) |
| `switch` | On/off switches and outlets |
| `sensor` | Numeric/measurement items (temperature, humidity, battery, power, …) |
| `binary_sensor` | Boolean items (motion, door/window, leak, tamper, …) |
| `lock` | Locks (lock/unlock) |
| `climate` | Thermostats (setpoint + mode) |

There are no custom services/actions — entities use the standard Home Assistant
services for their domain (`light.turn_on`, `lock.lock`, etc.).

## Installation

### HACS (recommended)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**, add this
   repository as an *Integration*.
2. Install **ezloPi** and restart Home Assistant.

### Manual

Copy `custom_components/ezlopi` into your Home Assistant `config/custom_components/`
directory and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration →
ezloPi** (or accept the discovered-hub prompt). You'll be asked for:

### Installation / configuration parameters

| Field | Description |
|---|---|
| **Username** | Your Ezlo account username (email/id). |
| **Password** | Your Ezlo account password. Stored in the config entry and used to refresh the cloud token. |

One config entry represents one Ezlo account and covers every controller on it.
The credentials are validated against the Ezlo cloud before the entry is created.
If the credentials later stop working, Home Assistant raises a re-authentication
prompt; you can also change them via **Reconfigure**.

## How data is updated

The integration is **push** (`iot_class: local_push`). Each hub has a persistent
local websocket; the hub broadcasts `hub.item.updated` events which update the
relevant entity immediately. A lightweight application-level keepalive query runs
every ~15s to keep the socket open and refresh the full item list. There is no
periodic entity polling.

## Use cases

- Expose ezloPi-connected dimmers/switches to Home Assistant automations,
  dashboards, and voice assistants.
- Use ezloPi sensors (motion, contact, temperature, …) as triggers/conditions.
- Control ezloPi locks and thermostats from Home Assistant.

### Example automation

```yaml
automation:
  - alias: "Hallway dimmer to 30% at sunset"
    triggers:
      - trigger: sun
        event: sunset
    actions:
      - action: light.turn_on
        target:
          entity_id: light.frankever_dimmer_dimmer
        data:
          brightness_pct: 30
```

## Known limitations

- **Local network required.** Control happens over the LAN; the controller must
  be reachable from Home Assistant (same network / mDNS visible). Cloud-only
  relay is not used.
- **Account credentials are stored** in the config entry (standard for Home
  Assistant) and the cloud token is refreshed on reconnect.
- **Local API authentication** depends on controller firmware. The integration
  authenticates with the hub's `local_key`; older firmware may not enforce it.
- New devices added to a hub appear after the next item-list refresh; full
  dynamic add/remove of hubs at runtime is limited.

## Troubleshooting

- **Entities show *unavailable*** — the hub websocket is down. Confirm the
  controller is powered and on the same LAN as Home Assistant, and that mDNS
  (`_ezlo._tcp.local.`) is reachable (no VLAN/mDNS-reflector issues).
- **Setup fails with "cannot connect"** — Home Assistant can't reach the Ezlo
  cloud to log in. Check internet connectivity.
- **Setup fails with "invalid auth" / a re-auth prompt appears** — the
  username/password were rejected. Re-enter them.
- **Diagnostics** — download diagnostics from the integration's ⋮ menu to see
  each hub's connection status and item count (credentials/keys redacted).
- **Logs** — enable debug logging:
  ```yaml
  logger:
    logs:
      custom_components.ezlopi: debug
  ```

## Removal

Remove the integration from **Settings → Devices & Services → ezloPi → Delete**.
This closes the hub connections and removes the entities/devices. To fully
uninstall, delete `custom_components/ezlopi` and restart Home Assistant.
