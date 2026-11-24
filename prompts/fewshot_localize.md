# Few-shot examples (output FORMAT only)

The paths below show the format. `vendor/oem/...` paths are fictional; AOSP paths may not
exist in the indexed tree. In a real run every path must come verbatim from a tool
result of this run. Human review and the patch are decided by code, not by you.

## Example 1 — vehicle data stale after resume
Bug: Speed not shown on cluster after ignition ON (Android 15).
Tools used:
  lookup_vss_signal("Vehicle.Speed")
  find_symbol("PERF_VEHICLE_SPEED")
  hybrid_search("cluster speed subscribe power policy resume")
Ranked candidate files:
1. vendor/oem/hmi/cluster/src/com/oem/car/cluster/SpeedController.java [customer]
2. vendor/oem/vss/mapping/speed.yaml [vss]
3. packages/services/Car/service/src/com/android/car/CarPropertyService.java [carservice]
Root cause: the mapping resolves Vehicle.Speed to PERF_VEHICLE_SPEED and the VHAL emits it,
but SpeedController subscribes only in onCreate; after the power-policy transition it never
subscribes again. Evidence: single subscribePropertyEvents call in onCreate (SpeedController
snippet); no subscription in the power-policy listener.

## Example 2 — crash in OEM HMI
Bug: HMI settings crash (NPE) when opening the seat page.
Tools used:
  hybrid_search("SeatSettings Fragment NullPointerException areaId")
  read_source on the top hit
Ranked candidate files:
1. vendor/oem/hmi/settings/src/com/oem/car/settings/seat/SeatSettingsFragment.java [customer]
Root cause: the fragment dereferences an area id that is absent for single-zone seats; the
fix belongs in the vendor HMI package, not CarService. Evidence: SeatSettingsFragment snippet
using the area id without a null check.

## Example 3 — wrong value, mapping layer
Bug: Speed shown 3.6× too low in the OEM cluster.
Tools used:
  lookup_vss_signal("Vehicle.Speed")
  read_source on the mapper class
Ranked candidate files:
1. vendor/oem/vss/mapper/src/SpeedMapper.cpp [customer]
2. vendor/oem/vss/mapping/speed.yaml [vss]
Root cause: VSS Vehicle.Speed is km/h, VHAL PERF_VEHICLE_SPEED is m/s; the mapper copies the
value without the ×3.6 conversion. Evidence: SpeedMapper snippet assigning floatValues[0]
directly; speed.yaml declares unit km/h.
