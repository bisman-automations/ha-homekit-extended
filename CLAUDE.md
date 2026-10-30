# HomeKit Extended: notes for Claude and other coding agents

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
2. **Version**: the version is in both `custom_components/homekit_extended/manifest.json`
   and `VERSION` in `const.py`; bump both in the release commit.
3. **README.md**: update when behavior or options change.
4. **UI text** comes from `scripts/gen_strings.py` (see Layout).

## Testing

Home Assistant 2026.3 and newer need Python 3.14; the oldest supported
version (2026.8, from `hacs.json`) runs on 3.14 too (`requirements_test_min.txt`).

Run the shared checks at the end of this file. The tests also need core
HomeKit's requirements for the installed Home Assistant, which change between
releases:

```
pip install $(python scripts/core_requirements.py)
```

CI installs the same set with the `core-requirements` input
(`homekit camera ffmpeg homekit_controller`).

For Home Assistant dev or beta, run `python scripts/run_upcoming_tests.py -q`
instead of `pytest`. The test harness is built for released versions; the
script imports Home Assistant first and bridges the harness's known gaps with
newer versions. When a dev or beta job fails before any test runs (an import
error inside `pytest_homeassistant_custom_component`), add the shim there. The
Test workflow's **next** job runs this script against the newest pre-release
harness.

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
- **Oldest supported Home Assistant is 2026.8.** Feature-detect newer APIs and
  keep old behavior where they're missing (see `LINKED_DEVICES_SUPPORTED`, 2026.9, and `all_devices`: `registry.devices`
  is a mapping on 2026.8 and an iterable of entries from 2026.9).
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

<!-- BEGIN bisman-automations shared rules -->
<!-- This block is the same in every bisman-automations repo. Edit it in
     bisman-automations/.github (templates/CLAUDE.shared.md) and copy it to
     every repo; the Validate workflow's "standards" job checks it matches. -->

## Shared rules (every bisman-automations repo)

### Releases

- The version lives in `custom_components/<domain>/manifest.json`. Anything
  else that repeats it is called out in the repo-specific notes below and is
  bumped in the same commit.
- Versions follow [Semantic Versioning](https://semver.org): patch for fixes,
  translations, docs and CI; minor for new features, options or entities;
  major when something users rely on is removed or renamed (entities,
  services, events, options) or setup has to be redone.
- Check what is released before choosing a version:
  `git ls-remote --tags origin | awk -F/ '{print $3}' | sort -V | tail -1`
  (`sort -V`: plain sorting puts `v1.10.0` before `v1.9.0`). Never add
  changes to a released version. If the manifest version has no tag yet,
  add to its CHANGELOG section instead of bumping again.
- Every push to `main` ships as a release. In that same commit, named
  `Release X.Y.Z`, bump the manifest version and write the notes directly
  under a new `## [X.Y.Z] - YYYY-MM-DD` heading in `CHANGELOG.md` (Keep a
  Changelog: Added, Changed, Deprecated, Removed, Fixed, Security), with
  that version's compare link at the bottom. There is no `## [Unreleased]`
  section and no `[Unreleased]:` compare link. New work is not collected
  under a holding heading and moved later; the release commit is where the
  heading and the entries are written. The standards check fails if a
  `## [Unreleased]` heading is present.
- Ask Donavan before every push. Push fast-forward only: never force-push,
  rebase or amend anything already pushed, or rewrite history in any other
  way. Rewriting history orphans the release tags (it broke drafting in
  ha-button-actions).
- Pushing to `main` runs **Draft release**, which creates or refreshes a
  draft `vX.Y.Z` with that version's CHANGELOG section as the notes, the
  commits since the last release and `<domain>.zip`. **Donavan publishes
  the draft himself**, and publishing creates the tag. Never create or push
  tags, publish releases, or edit published releases.
- Publishing runs **Release check**: the tag must match the manifest version
  and the CHANGELOG, and be on `main`. If it fails, fix it with a new patch
  release, not by moving the tag.

### Commits

- Author commits as `Donavan Becker <donavan.becker@icloud.com>` (set
  `git config user.name` / `user.email` in the clone). HACS's
  default-repository check needs the submitter to have a fair share of the
  commits. Credit an AI assistant with a `Co-Authored-By:` trailer if you
  like, never as the author.
- Subject lines are plain, imperative and say what changed for the user
  ("Show Pico remotes as unavailable when Lutron isn't running"). No
  `fix:`/`feat:` prefixes. Release commits are "Release X.Y.Z".
- Commit and push only when asked.

### CI (shared workflows)

- `.github/workflows/` holds four thin callers of the reusable workflows in
  [bisman-automations/.github](https://github.com/bisman-automations/.github):
  `test.yml` (lint, frontend, pytest on the oldest supported and the newest
  Home Assistant, plus the upcoming one as information only), `validate.yml`
  (hassfest, HACS, standards), `draft-release.yml` and `release-check.yml`.
  Don't add jobs to the callers. Use the shared workflow's inputs, or change
  the shared workflow so every repo gets the improvement.
- `requirements_test.txt` leaves `pytest-homeassistant-custom-component`
  unpinned (newest Home Assistant) and pins ruff (and mypy where used).
  `requirements_test_min.txt` pins the harness release for the minimum in
  `hacs.json`: the newest harness release of that month. CI fails if they
  drift apart.
- Raising the minimum Home Assistant means changing, together: `hacs.json`,
  `requirements_test_min.txt`, `python-min` in `test.yml` (3.13 up to
  2026.2, 3.14 from 2026.3), the README's Home Assistant badge, and a
  CHANGELOG entry.
- Home Assistant 2026.3 and newer need Python 3.14 (3.14.2+).
- The published hassfest rejects `categories` in `manifest.json` and Pillow
  in `requirements`. Leave both out.
- Dependabot (`.github/dependabot.yml`) proposes action and pinned-tool
  updates weekly. It ignores the test harness on purpose.
- Test and Validate also run every Thursday, the day after Home Assistant's
  betas come out. The **next** test job (newest pre-release harness) is
  information only: when it fails, fix the cause before that release ships.

### Before saying work is done

Run from the repo root, in a Python 3.14 venv with `requirements_test.txt`
installed:

```
ruff check .
ruff format --check .
pytest
```

Also run `node --test tests/*.mjs` when the repo has Node tests, mypy when
the repo-specific notes list it, and the tests in a second venv with
`requirements_test_min.txt` when a change could behave differently on the
oldest supported Home Assistant. The standards check (also in CI) is
`python <path to bisman-automations/.github>/scripts/check_repo.py .`.

### Code and docs

- Support every Home Assistant from the `hacs.json` minimum to the newest
  release. Check for deprecations in the newest release and the beta.
- Keep entity IDs, unique IDs, service names and event names stable. When
  one has to change, migrate it and say so under Changed in the CHANGELOG.
- `strings.json` and `translations/en.json` hold the same keys. Home
  Assistant reads only `translations/`, so change both in the same commit.
- Never put personal details in the repo (README, CHANGELOG, code, tests,
  screenshots): no names, addresses, account or device identifiers, or real
  entity IDs. Use placeholders such as `light.living_room`.
- `README.md` is user documentation with the standard header (badges, HACS
  and "add integration" My buttons) and sections Installation, Setup, ...,
  Troubleshooting, Development, License. Use plain language and show names,
  not entity IDs, wherever the UI does.
<!-- END bisman-automations shared rules -->
