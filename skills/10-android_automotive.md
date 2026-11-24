# AAOS vehicle-data diagnosis (CarPropertyManager → VHAL)

Find the first hop where the value is wrong, missing or stale. Each rule cites a source
at the end of this file.

## Workflow
```
- [ ] 1. Pin the property: name / id and areaId from logcat or the bug (no invented ids)
- [ ] 2. Search vendor/ and device/ for the property id, mapping and HMI client
- [ ] 3. Check the property config against the spec (section "Property model")
- [ ] 4. Walk the path hop by hop; stop at the first wrong or missing value
- [ ] 5. Only then commit a target file
```

## Who may talk to VHAL
- CarService is the only Java client of VHAL. Car apps use the Car APIs
  (`CarPropertyManager`); SELinux blocks direct app access. [S1]
- Native clients use `libvhalclient` (`IVhalClient::tryCreate()`) from Android 13, and
  need sepolicy (`hal_client_domain(<domain>, hal_vehicle)`). [S1]
- An app opening `IVehicle` directly is the wrong layer, not a missing permission.

## Property model
- Property id = unique id (16 bits) | type (8) | area type (4) | group (4); group is
  `SYSTEM` (0x10000000) or `VENDOR` (0x20000000). Example: `INFO_VIN` = 0x11100100. [S2]
- Android 14+: system properties are defined in
  `hardware/interfaces/automotive/vehicle/aidl_property/.../VehicleProperty.aidl`. [S3]
- For system properties, `access` and `changeMode` must equal the values documented in
  `VehicleProperty.aidl`. With per-area access, the global `access` is the maximal subset
  (one area `READ`, another `READ_WRITE` → `READ`). [S2]
- `changeMode`: `STATIC` never changes after boot; `ON_CHANGE` is reported on change;
  `CONTINUOUS` is reported at the subscription sample rate, bounded by
  `minSampleRate` / `maxSampleRate`. [S2]
- Zoned properties need `areaConfigs`; an area id is a bit-OR of the area type's enum
  flags (for example `ROW_1_LEFT | ROW_1_RIGHT`). [S2] Check that the client subscribes
  to an area id the config actually lists.
- Android 15: `supportVariableUpdateRate` on continuous properties delivers only changed
  values; a fixed-rate subscriber disables it for that property. [S2]
- Vendor properties are a last resort, must not duplicate existing
  `VehiclePropertyIds`, and default to `android.car.Car.PERMISSION_VENDOR_EXTENSION`. [S4]

## StatusCode semantics (VHAL)
- `TRY_AGAIN`: transient, a retry will likely succeed (for example while booting). [S5]
- `NOT_AVAILABLE` / `NOT_AVAILABLE_DISABLED`: stays unavailable until some other state
  changes, e.g. setting HVAC temperature while HVAC power is off. A retry loop is wrong. [S5][S6]
- `NOT_AVAILABLE_SPEED_LOW|SPEED_HIGH|POOR_VISIBILITY|SAFETY`: detailed unavailability,
  added for ADAS properties. [S5]
- Java clients see errors through `CarPropertyEventCallback.onErrorEvent(propertyId,
  areaId, errorCode)`; prefer overriding the 3-argument form. [S7]

## Client subscription (Java)
- Current API: `CarPropertyManager.subscribePropertyEvents(...)`; the `registerCallback`
  overloads are deprecated. Match what the target file already uses. [S8]
- Obtain `Car` with `Car.createCar(context, handler, waitTimeoutMs, listener)`. On
  `onLifecycleChanged(car, false)` CarService crashed: stop using managers; on
  `ready == true` get managers again and re-subscribe. [S9]

## Power policy (stale after ignition / resume)
- The car power policy daemon is the single source of truth for power policy;
  CarPowerManagementService delegates to it. [S10]
- VHAL requests policies through `POWER_POLICY_REQ` / `POWER_POLICY_GROUP_REQ`; the
  daemon reports changes made by others through `CURRENT_POWER_POLICY`. [S10]
- Listeners: Java `CarPowerManager` power-policy listener (system-privileged modules);
  native `ICarPowerPolicyServer.registerPowerPolicyChangeCallback(callback, filter)`.
  Register with a filter for your components and act when notified. [S10]
- A policy changes only the components it covers; `PROJECTION`, `AUDIO`, `DISPLAY`, … are
  separate components. [S10]
- Defaults are applied on every entry into Wait for VHAL / On, including a cancelled
  shutdown. Code that only initializes at boot misses these transitions. [S10]

## Sources
- [S1] https://source.android.com/docs/automotive/vhal/native-client
- [S2] https://source.android.com/docs/automotive/vhal/property-configuration
- [S3] https://source.android.com/docs/automotive/vhal/system-properties
- [S4] https://source.android.com/docs/automotive/vhal/special-properties
- [S5] `hardware/interfaces/automotive/vehicle/aidl/android/hardware/automotive/vehicle/StatusCode.aidl` (AOSP)
- [S6] https://source.android.com/docs/automotive/vhal/properties
- [S7] https://developer.android.com/reference/android/car/hardware/property/CarPropertyManager.CarPropertyEventCallback
- [S8] https://developer.android.com/reference/android/car/hardware/property/CarPropertyManager
- [S9] https://developer.android.com/reference/android/car/Car.CarServiceLifecycleListener
- [S10] https://source.android.com/docs/automotive/power/power_policy
