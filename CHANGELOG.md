# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-06

First release. It merges `homekit-air-purifier` and `homekit-irrigation` into a
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
