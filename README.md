# HomeKit Extended

A Home Assistant custom integration that publishes HomeKit accessory types which
Home Assistant's built-in **HomeKit Bridge** can't build on its own. These are
single HomeKit accessories assembled from several Home Assistant entities.

| Accessory | Built from | What the built-in bridge does instead |
| --- | --- | --- |
| **Irrigation System** | Several `valve.*` entities, one per zone | Exposes each valve as its own separate accessory |
| **Air Purifier** | A `fan.*` entity plus optional sensors | Has an air purifier type, but without a linked Fan service, swing mode or AQI-style input |

Each accessory runs next to the HomeKit Bridge integration, not inside it. Every
accessory you add gets its own port and its own pairing code, and you add it in
Apple Home separately.

## Installation

### HACS

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bisman-automations&repository=homekit-extended&category=integration)

Click the button above, then install **HomeKit Extended** and restart Home
Assistant. Or add it by hand:

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/bisman-automations/homekit-extended` with the category
   **Integration**.
3. Install **HomeKit Extended**, then restart Home Assistant.

### Manual

Copy `custom_components/homekit_extended` into your Home Assistant
`config/custom_components/` folder, then restart.

## Adding an accessory

1. Go to **Settings → Devices & services → Add integration → HomeKit Extended**.
2. Choose **Air purifier** or **Irrigation system**.
3. Fill in the form. The suggested port is already free, and the suggested
   pairing code is random.
4. In Apple Home, choose **Add Accessory → More options**, select the accessory,
   and enter the pairing code.

To add more accessories, repeat these steps. Each one appears as its own
integration entry.

## Irrigation System

Publishes one **Irrigation System** accessory, with one **Valve** zone for each
selected valve. Zone names come from each valve's friendly name in Home
Assistant.

- **Starting a zone in Apple Home** opens the valve. The valve closes
  automatically when the zone's run time ends.
- **Run time** starts at the configured default and can be changed per zone in
  Apple Home, up to 3600 seconds, which is HomeKit's limit. A run time of 0 means
  the zone runs until you stop it.
- **Turning the whole system off** in Apple Home closes every zone.
- **Opening or closing a valve outside HomeKit**, for example from an automation
  or the valve itself, is mirrored to Apple Home. Only runs started from Apple
  Home get the automatic close.

## Air Purifier

Publishes one **Air Purifier** accessory backed by a fan entity, with linked
services:

| HomeKit service | Source | Notes |
| --- | --- | --- |
| Air Purifier | Fan | Power, speed (`fan.set_percentage`), swing (`fan.oscillate`), and auto mode through a preset named `auto` |
| Fan | Fan | Linked so Apple Home shows fan controls on the purifier |
| Air Quality | Air quality sensor or PM2.5 sensor | Accepts a HomeKit 1–5 value or an AQI value; if only PM2.5 is set, quality is estimated from it |
| Humidity | Humidity sensor | Optional |
| Temperature | Temperature sensor | Optional; converted to °C |
| Filter Maintenance | Filter life sensor (%) | Optional; asks for a replacement at 10% or less |

## Changing an accessory

Use **Configure** on the integration entry to change the port, pairing code or
entities. To rename the accessory, rename the integration entry.

Apple Home caches an accessory's structure. If you add or remove sensors or
zones, or change the port or pairing code, you may need to remove the accessory
from Apple Home and add it again.

## Tips

- **Avoid duplicates.** Leave these entities out of the built-in HomeKit
  Bridge's filter, or Apple Home will show them twice.
- **Watch for port conflicts.** The setup flow rejects ports already used by
  another HomeKit Extended accessory or a HomeKit Bridge entry. If a port is
  busy for another reason, the entry retries until the port is free.
- **Networking follows Home Assistant.** Accessories are advertised on the
  interfaces chosen under **Settings → System → Network**.

## Moving from homekit-air-purifier or homekit-irrigation

This integration replaces both standalone integrations.

1. Delete the old integration's entries, then remove it in HACS.
2. Remove the old accessories from Apple Home.
3. Add them again here. Pairing can't be carried over, because the pairing
   state is stored per integration.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt ruff
pytest
ruff check custom_components tests && ruff format --check custom_components tests
```

The tests run against a real Home Assistant core through
`pytest-homeassistant-custom-component`. One test also starts the real HAP
server on a local socket.

## License

MIT. See [LICENSE](LICENSE).
