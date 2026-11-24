# Native VHAL and native clients (C++)

## Reference architecture (AIDL VHAL)
- Two layers: `DefaultVehicleHal` implements `IVehicle` and holds the generic logic;
  `IVehicleHardware` is the device-specific part. The reference `IVehicleHardware` is
  `FakeVehicleHardware` (in-memory, emulator only); vendors must not ship it as-is. [S1]
- Main service thread: `VehicleService.cpp`; interface implementation:
  `DefaultVehicleHal.cpp` under `hardware/interfaces/automotive/vehicle/aidl/impl/`. [S1]
- `DefaultVehicleHal` does the input checks (including duplicate request ids), allocates
  per-request client objects, checks read / write permission, and periodically calls
  `IVehicleHardware::checkHealth` and sends the heartbeat. [S1]
- The list of property configs must be static after VHAL initialization. [S1]
- Android 14+: `FakeVehicleHardware` reads configs at runtime from
  `/vendor/etc/automotive/vhalconfig/` (JSON); `persist.vendor.vhal_init_value_override`
  (set at build time or very early boot) loads overrides from
  `/vendor/etc/automotive/vhaloverride/`. [S1]

## Where a VHAL bug usually is
| Symptom | Look at |
|---|---|
| Property missing in CarService | config list in the hardware layer (static after init) [S1] |
| `INVALID_ARG` on get / set | prop id / area id not in config; duplicate request id [S1] |
| Wrong value type, e.g. speed reads 0 | config type vs `VehicleProperty.aidl`; which `*Values` field the hardware layer fills (`floatValues` for `FLOAT`) [S2] |
| `TRY_AGAIN` forever | backend not ready; must become `OK` or a permanent code [S3] |
| VHAL killed by watchdog | heartbeat / `checkHealth` path blocked [S4] |

## Native VHAL clients
- Use `libvhalclient`: `IVhalClient::tryCreate()`, null check, then
  `getValueSync(*client->createHalPropValue(toInt(VehicleProperty::...)))`. [S5]
- The client needs its own domain with `init_daemon_domain`, `binder_use` and
  `hal_client_domain(<domain>, hal_vehicle)`; `hwbinder_use` only for HIDL VHAL. [S5]

## Debugging on a device
- `adb shell dumpsys android.hardware.automotive.vehicle.IVehicle/default --help`;
  read a value with `--get INFO_VIN` (or the hex id), set with `--set`. [S6]
- VTS `VtsHalAutomotiveVehicle_TargetTest` checks the configs of all supported system
  properties and basic VHAL operations. [S6]

## Sources
- [S1] https://source.android.com/docs/automotive/vhal/reference-implementation
- [S2] https://source.android.com/docs/automotive/vhal/property-configuration (property type → value field)
- [S3] `StatusCode.aidl` in AOSP (`TRY_AGAIN`: transient; retry will likely succeed)
- [S4] https://source.android.com/docs/automotive/watchdog/wd_system_health
- [S5] https://source.android.com/docs/automotive/vhal/native-client
- [S6] https://source.android.com/docs/automotive/vhal/vhal_debug
