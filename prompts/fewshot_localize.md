## Example 1
Bug: Speed not shown on cluster after ignition ON (Android 15).
Tools used: lookup_vss_signal("Vehicle.Speed") → vendor mapping; find_aidl_interface("IVehicle"); hybrid_search("CarPropertyService subscribe timeout")
Result:
1. vendor/oem/vss/mapping/speed.yaml [vss|customer]
2. hardware/interfaces/automotive/vehicle/.../VehicleHal.cpp [vhal]
3. packages/services/Car/.../CarPropertyService.java [carservice]
Hypothesis: VSS→VHAL map OK; subscription not re-established after resume (A15 power path).

## Example 2
Bug: HMI settings crash when opening seat page.
Tools: hybrid_search("SeatPreference Fragment NPE"); read_source on top hit
Result: OEM HMI app null areaId; fix in vendor packages, not AOSP CarService.
