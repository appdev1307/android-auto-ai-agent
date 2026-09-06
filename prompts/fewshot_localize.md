# Few-shot examples (illustrative format only)

These show the tool→evidence→ranked-output pattern. The paths below are real AOSP paths
used to demonstrate FORMAT — in a real run, every path you output must come verbatim from
a tool result, never abbreviated and never invented.

## Example 1 — localization only
Bug: Speed not shown on cluster after ignition ON (Android 15).
Tools used:
  lookup_vss_signal("Vehicle.Speed")
  find_aidl_interface("IVehicle")
  hybrid_search("CarPropertyService subscribe timeout")
Ranked candidate files:
1. vendor/oem/vss/mapping/speed.yaml [vss]
2. hardware/interfaces/automotive/vehicle/aidl/impl/default_config/config/DefaultProperties.json [vhal]
3. packages/services/Car/service/src/com/android/car/CarPropertyService.java [carservice]
Root cause: VSS→VHAL mapping is present (speed.yaml resolves to PERF_VEHICLE_SPEED), but the
subscription is not re-established after resume — the A15 power path drops the callback.
Evidence: speed.yaml mapping snippet + CarPropertyService.registerListener not re-invoked on resume.
needs_human_review: true

## Example 2 — localization only
Bug: HMI settings crash (NPE) when opening the seat page.
Tools used:
  hybrid_search("SeatSettings Fragment NullPointerException areaId")
  read_source on the top hit
Ranked candidate files:
1. vendor/oem/hmi/settings/src/com/oem/car/settings/seat/SeatSettingsFragment.java [customer]
Root cause: the OEM HMI fragment reads an areaId that is null for single-zone seats; the fix
belongs in the vendor HMI package, not AOSP CarService.
Evidence: SeatSettingsFragment snippet dereferencing areaId without a null check.
needs_human_review: false

## Example 3 — localization + minimal patch + UT ideas
Bug: Speed not updating in HMI after ignition ON (Android 15). Mapping correct; VHAL emits.
Tools used:
  lookup_vss_signal("Vehicle.Speed")
  read_source on OEM cluster / power-policy class
  hybrid_search("registerCallback power resume")
Ranked candidate files:
1. vendor/oem/hmi/cluster/src/com/oem/car/cluster/SpeedController.java [customer]
2. packages/services/Car/service/src/com/android/car/CarPropertyService.java [carservice]
Root cause: client registers CarPropertyManager callback only in onCreate; after A15 power
policy tear-down the callback is not re-registered. Mapping and VHAL path are fine.
Evidence: single registerCallback in onCreate; no re-register in power-policy / onResume path.

Proposed patch (draft): N/A — describe change in words only (file not verified as fully read in this example).
In the OEM SpeedController (path from tools): extract registerSpeedListener() and call it from
both initial setup and the existing power-policy / onResume path. Do not touch VSS mapping
or DefaultProperties.json.

Unit test ideas:
Framework: JUnit4 + Robolectric (or instrumentation if package uses carservice_test style)
Target module/dir: same package tests/ next to OEM cluster (or packages/services/Car/tests if fix is CarService)
- speedCallbackResumesAfterPowerOn: setup mock CarPropertyManager + register via production helper;
  action simulate CarPowerManager OFF→ON then emit PERF_VEHICLE_SPEED; assert callback invoked after ON
- speedCallbackSurvivesProcessResume: setup same; action re-call register helper without full ignition;
  assert updates received again and no NPE

needs_human_review: true