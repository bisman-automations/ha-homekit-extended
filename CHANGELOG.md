# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-10-06

### Added
- **Advertise without ID suffix** connection option. An accessory is
  discovered in Apple Home and Home Assistant as "Irrigation System" instead of
  "Irrigation System 8D111D". If another device on the network already uses that
  name, the suffix is kept. The option is off by default, and turning it on
  doesn't require pairing again.
- **Multi-button event entities are split.** Some integrations put a whole
  remote on one `event` entity, with event types like `button_1_single`,
  `button_2_hold` or `single_left`. Each physical button now becomes its own
  HomeKit button, worked out from the event types the entity reports.
  Single-button entities, including Hue's `short_release` and `long_press`,
  work as before.

### Upgrading
- A Buttons accessory that uses a multi-button entity gets new buttons, so
  remove it from Apple Home and add it again: **Configure → Pairing → Reset
  pairing**.

### Fixed
- The minimum Home Assistant version is now 2025.3, which 1.1.0 already needed.
  HACS offered 1.1.0 to 2025.1 and 2025.2, where it failed to load.

## [1.1.0] - 2026-10-06

### Added
- Six new accessory types:
  - Ceiling fan with light
  - Buttons and remotes from `event` entities (single, double and long press)
  - Multi-sensor
  - Air quality monitor
  - Power strip
  - Shower or faucet
- **Set up from a device.** Pick the device an accessory represents and its
  entities are filled in, and the accessory is named after it. The port and
  pairing code move into a collapsed **Connection** section and are chosen
  automatically.
- **Pairing QR code.** A notification shows a QR code to scan in Apple Home
  until the accessory is paired.
- **Manage accessories from Configure.** The menu has Entities, Run times, Port
  and pairing code, and Pairing, where you can show the QR code or reset
  pairing.
- **Device page.** Each accessory has a device in Home Assistant with a
  **Paired** sensor, plus downloadable diagnostics.
- **Irrigation, all native HomeKit behavior:**
  - Each zone has its own run time, and run times changed in Apple Home are
    saved.
  - Turning the system on runs every enabled zone in turn.
  - Zones turned off in Apple Home are skipped.
  - One zone at a time, on by default.
  - Optional pump or master valve, which turns off 5 seconds after the last
    zone.
  - A zone shows a fault when its valve is unavailable.
- Changing run times or enabled zones takes effect right away, without
  restarting the accessory.

### Changed
- Turning the Irrigation System off now closes only the zones that are running.
- Accessories now report "HomeKit Extended" as the manufacturer.

### Fixed
- When an accessory's port was already in use, Home Assistant marked the entry
  as failed instead of retrying it. The cleanup after a failed start crashed and
  hid the real error. The entry now shows "Retrying setup".
- The setup flow now also rejects ports used by the old `homekit_irrigation`
  and `homekit_air_purifier` integrations, or by anything else on the host.

### Upgrading
- Existing accessories keep their pairing. Because their services gained new
  characteristics, Apple Home may show them as "No Response" until you remove
  and re-add them. You can do this from **Configure → Pairing → Reset pairing**.

## [1.0.0] - 2026-10-06

First stable release. It merges `homekit-air-purifier` and `homekit-irrigation` into a
single `homekit_extended` integration.

### Added
- The setup flow now starts with a menu where you pick the accessory type: an
  air purifier or an irrigation system.
- Both accessory types now share one HAP driver lifecycle, and the HomeKit
  address is advertised on the network interfaces configured in Home Assistant.
- Irrigation zones now close automatically when their HomeKit run time ends.
- The pairing code is randomly generated, and the setup flow rejects codes the
  HomeKit spec forbids, including the old default `123-45-678`.
- The setup flow suggests a free port and catches port conflicts with other
  HomeKit Extended accessories and with HomeKit Bridge entries.
- Removing an entry now deletes its stored pairing state.
- Added a test suite, plus hassfest, HACS, ruff and pytest checks in CI.
- Added the HomeKit Extended icon, shown in Home Assistant 2026.3 and later, and an
  "Open in HACS" button in the README.

### Changed
- Irrigation remaining time is now calculated when HomeKit reads it, instead of
  being pushed every second.
- Run times are now capped at HomeKit's maximum of 3600 seconds.
- Valves are now picked with an entity selector, and zone names follow the
  valve's friendly name in Home Assistant.
- Accessory names now come from the integration entry's title.

### Fixed
- Irrigation setup no longer breaks when no valve entities exist yet.
- Clearing an optional air purifier sensor in the options now removes it.

[1.2.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.2.0
[1.1.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.1.0
[1.0.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.0.0
