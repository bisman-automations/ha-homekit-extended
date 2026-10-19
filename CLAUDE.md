# HomeKit Extended: working on this repo

A Home Assistant custom integration (HACS) that publishes HomeKit accessories
Home Assistant's own HomeKit Bridge can't build: irrigation systems, ceiling
fans with lights, buttons, multi-sensors, air quality monitors, air purifiers,
power strips and showers/faucets. Each config entry is one accessory,
published either standalone (its own port and pairing code) or inside one of
core HomeKit's bridges.

Repo: `bisman-automations/ha-homekit-extended` (renamed from
`homekit-extended`; if git warns that the repository moved, run
`git remote set-url origin https://github.com/bisman-automations/ha-homekit-extended.git`).

## Every change

1. **Tests and lint pass** (see Testing). Add or update tests for what changed.
2. **CHANGELOG.md**: add a section at the top in Keep a Changelog style
   (`## [x.y.z] - YYYY-MM-DD`, then `### Added`/`### Changed`/`### Fixed`), and
   the compare/release link at the bottom. Write for users, in plain language.
3. **Version**: bump it in both `custom_components/homekit_extended/manifest.json`
   and `VERSION` in `const.py`. Semantic versioning: new feature → minor, fix →
   patch, breaking → major.
4. **Never edit a released version.** Check what's released first
   (`gh api repos/bisman-automations/ha-homekit-extended/releases --jq '.[0].tag_name'`)
   and start a new version if the one in the manifest is already out.
5. **README.md**: update when behavior or options change.
6. **Commit** with a conventional prefix (`feat:`, `fix:`, `docs:`, `test:`,
   `ci:`, `feat!:` for breaking) and push to `main`. Then watch CI
   (`gh run list -R bisman-automations/ha-homekit-extended -L 1`) until every job passes.

## Releases

Don't create tags or releases. On every push to `main`,
`.github/workflows/draft-release.yml` creates or refreshes a draft release for
the version in `manifest.json`; the maintainer publishes it. Tell him the
version is ready to publish.

## Testing

Home Assistant 2026.9 and newer need Python 3.14; the oldest supported
version (2025.3, from `hacs.json`) runs on 3.13.

```
pip install -r requirements_test.txt
pip install $(python scripts/core_requirements.py)   # core HomeKit's requirements for this HA version
pytest
ruff check custom_components tests scripts
ruff format --check custom_components tests scripts
```

For Home Assistant dev or beta, run `python scripts/run_upcoming_tests.py -q`
instead of `pytest`. The test harness is built for released versions; the
script imports Home Assistant first and bridges the harness's known gaps with
newer versions. When a dev or beta job fails before any test runs (an import
error inside `pytest_homeassistant_custom_component`), add the shim there.

CI (`.github/workflows/validate.yml`) runs hassfest, HACS validation, and the
tests on the current release, on 2025.3, and on Home Assistant beta and dev.
Run the suite on the oldest and newest versions yourself when touching
anything version-sensitive.

`tests/test_end_to_end.py` starts real HAP servers (core HomeKit's and ours)
and pairs with them through aiohomekit. It is the only test that exercises the
hook into core HomeKit with the network running, so keep it passing; pairing
is retried because aiohomekit and HAP-python occasionally fail at random.

## Layout

- `custom_components/homekit_extended/`
  - `__init__.py`: setup; picks standalone or bridged, raises repair issues.
  - `driver.py`: standalone HAP server (`HomeKitAccessoryServer`), stored
    instance and accessory IDs, advertising without the ID suffix.
  - `bridge.py`: accessories inside a core HomeKit Bridge
    (`BridgedAccessoryServer`), and the hook into core HomeKit.
  - `config_flow.py`: add flow (menu per type, then entities) and options.
  - `accessories/`: one module per type; `base.py` holds shared code,
    `valves.py` the irrigation system and faucet, `zone_links.py` the
    controller entity discovery (run time, countdown, run all, stop, resume).
  - `pairing.py`, `binary_sensor.py` (Paired sensor and the device),
    `diagnostics.py`.
- `scripts/gen_strings.py`: **source of all UI text.** Edit it and run
  `python scripts/gen_strings.py`; it writes `strings.json` and
  `translations/en.json`. Never edit those two files by hand.
- `scripts/core_requirements.py`: prints core HomeKit's requirements for the
  installed Home Assistant (used by CI).

## Rules that keep users' setups working

- **Stay native to HomeKit.** Use HomeKit's own services and characteristics
  for each type. Don't add switches or other extra services just to control
  something that isn't one (for example irrigation options). Settings that
  have no HomeKit control belong in the options flow.
- **Instance IDs must never shift.** Apple Home stores rooms, names and
  automations against them. Every repeated service type needs a stable
  `unique_id` (the entity ID); never add services ahead of existing ones;
  standalone accessories deliberately leave out HAP's protocol information
  service (`add_protocol_version_service`). `test_first_run_keeps_pyhap_iids`
  guards accessories paired before 2.0.0.
- **Accessory IDs are stored** (core bridge storage, or `ExtendedAidStorage`
  for zone accessories), keyed by entry and entity. Don't change the keys.
- **Stored entry data is a contract.** Released options keep working; migrate
  old values when reading them (see `zone_layout()` for 2.1.0's
  `separate_zones`).
- **Options that only change values apply in place** (`IN_PLACE_KEYS` in
  `accessories/base.py`, via `apply_in_place`); anything else reloads the
  entry and rebuilds the accessory.
- **Oldest supported Home Assistant is 2025.3.** Feature-detect newer APIs and
  keep old behavior where they're missing (see `LINKED_DEVICES_SUPPORTED`).
- **The core HomeKit hook uses private API**:
  `HomeKit._async_create_bridge_accessory`, `HomeKitEntryData.homekit`,
  `HomeKit.bridge`, `aid_storage`, `status`, plus `HomeIIDManager` and the
  IID/AID storage classes. When Home Assistant changes them, bridge mode must
  fail with a repair issue, never break the user's bridge.
- **Rain Bird Extended** (`bisman-automations/ha-rainbird-extended`) is found
  by entity, not by name: a duration `number` and a timestamp `sensor` on each
  zone's device, and on the controller device the `run_all_zones`,
  `stop_irrigation` and `resume` buttons and the `irrigating` binary sensor
  (translation keys). Keep those in step with that repo.

## Docs

- Plain language, written for Home Assistant users.
- Never put the maintainer's own details (MAC addresses, serial numbers,
  account numbers, addresses) in docs, tests or examples. Use placeholders.
