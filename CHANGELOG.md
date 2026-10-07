# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-10-06

### Added
- **Add accessories to a HomeKit Bridge.** Instead of its own port and pairing
  code, an accessory can now join one of the bridges from Home Assistant's
  HomeKit Bridge integration, so it appears in a bridge you've already paired
  with no extra pairing. Choose **Publish as** when adding an accessory, or
  move an existing one under **Configure → Bridge, port and pairing code**.
  - The accessory's ID comes from the bridge's own storage, so Apple Home keeps
    its room, scenes and automations across restarts.
  - It is added before the bridge is announced at startup, rejoins when the
    bridge reloads, and is added or removed live when you change it, without
    restarting the bridge.
  - Repairs explain when the bridge also publishes the same entities (they
    would show up twice), when the bridge is deleted or switched to accessory
    mode, and when it already has HomeKit's maximum of 150 accessories. Setup
    also warns about duplicates before you finish.
  - The **Paired** sensor shows whether the bridge is paired, and diagnostics
    show which bridge the accessory is in.
- **Stable instance IDs.** Every service and characteristic keeps a stored ID,
  using the same storage as the core HomeKit Bridge, so adding or removing
  zones, outlets, buttons or sensors no longer shifts the IDs Apple Home
  already knows. Accessories paired with an earlier version keep their current
  IDs.
- **No Response when unavailable.** Apple Home shows an accessory as not
  responding when every entity it mirrors is unavailable, the same as core
  HomeKit accessories.
- CI now also runs the tests against Home Assistant's beta and development
  versions, so changes to core HomeKit show up before they're released. The
  tests pass on 2026.10 beta.

### Changed
- HomeKit Extended now depends on Home Assistant's HomeKit Bridge integration
  and reuses its code for instance IDs. Nothing needs to be set up in it unless
  you want to add accessories to a bridge.

## [1.5.0] - 2026-10-06

### Added
- **Accessory information follows the device.** Manufacturer, model, serial
  number and firmware left empty now use the details Home Assistant already has
  for the device the accessory represents, instead of "HomeKit Extended".
  - The device is the one picked during setup or, for existing accessories, the
    device of their entities.
  - Zone devices are followed up to their controller, so a Rain Bird irrigation
    system shows the controller's model (for example ARC8) and firmware.
  - When the device has no serial number, its MAC address is used.
  - The accessory information screen lists what each empty field will use.

### Changed
- Setup no longer copies the device's details into the accessory information
  fields. They stay empty and follow the device, so a firmware update on the
  device shows up in Apple Home without editing anything. Values typed in still
  take priority.

## [1.4.1] - 2026-10-06

### Fixed
- The run-times screen no longer says every zone runs "up to 3600 seconds; 0
  runs until stopped". It now lists which zones are set on the controller,
  with the controller's real limits (for Rain Bird, 1 minute to 24 hours in
  1-minute steps), and which zones are stored by HomeKit Extended.

## [1.4.0] - 2026-10-06

### Added
- **Controller run times and countdowns for irrigation and showers.** When a
  valve's device also has a run-time `number` and a time-remaining timestamp
  `sensor`, the zone uses them. This is the same pattern as core HomeKit's
  linked valve duration and end time, and
  [Rain Bird Extended](https://github.com/bisman-automations/ha-rainbird-extended)
  creates these for every Rain Bird zone.
  - A zone's run time in Apple Home shows and changes the controller's
    run-time entity, within its limits. A Rain Bird zone, for example, runs from
    1 minute to 24 hours in 1-minute steps.
  - The countdown comes from the controller's time-remaining sensor, and the
    controller ends each run itself, so this integration doesn't keep a timer
    that could disagree with it.
  - "Run all zones" moves on when the controller finishes each zone.
  - The run-times step and **Configure → Run times** write to the
    controller's entities for those zones.
  - Controller timers are on by default. Turn them off with **Use the
    controller's run times and countdown** under Entities.
- **Picking a controller finds its zones.** Picking a device during setup now
  also finds the entities on devices connected through it. For example, picking
  a Rain Bird controller selects all of its zone valves.

## [1.3.0] - 2026-10-06

### Added
- **Accessory information** can now be changed: the manufacturer, model,
  serial number and firmware that Apple Home shows under an accessory's details.
  - If you pick a device during setup, these are filled in from that device;
    firmware such as `v3.2.1-beta` becomes `3.2.1`.
  - Change them later under **Configure → Accessory information**. Changes
    apply right away, without pairing again.
  - Empty fields use the defaults: "HomeKit Extended", the accessory type, a
    unique ID and the integration's version.
  - HomeKit rules are checked: firmware must look like 1, 1.2 or 1.2.3; the
    serial number needs at least 2 characters; each field is limited to 64
    characters.
  - "HomeKit Certified" can't be changed. Apple shows Yes only for certified
    hardware.

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

[2.0.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v2.0.0
[1.5.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.5.0
[1.4.1]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.4.1
[1.4.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.4.0
[1.3.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.3.0
[1.2.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.2.0
[1.1.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.1.0
[1.0.0]: https://github.com/bisman-automations/ha-homekit-extended/releases/tag/v1.0.0
