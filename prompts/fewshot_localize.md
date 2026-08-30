# Few-shot examples (illustrative format only)

These show the tool→evidence→ranked-output pattern. The paths below are real AOSP paths
used to demonstrate FORMAT — in a real run, every path you output must come verbatim from
a tool result, never abbreviated and never invented.

## Example 1
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

## Example 2
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
