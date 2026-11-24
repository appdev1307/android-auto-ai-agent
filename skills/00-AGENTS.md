# Skill index — AAOS bug localization (customer-first)

Obey `CONTRACT.md`. Pick the skill by the first broken hop, not by the loudest keyword.

| Symptom / artifact in the evidence | Start with |
|---|---|
| Value wrong / stale / missing in HMI, `CarPropertyManager`, VHAL `StatusCode` | `10-android_automotive.md` |
| Writing a patch or unit test | `20-patch_and_ut.md` |
| Car app, Car UI library, RRO, `Car` connection lifecycle | `30-aaos_app.md` |
| `DeadObjectException`, `TransactionTooLargeException`, death recipients | `32-binder_ipc.md` |
| CarService / framework service lifecycle, foreground service types | `33-android_services.md` |
| Phone projection (Android Auto, Apple CarPlay) | `34-aacp_projection.md` |
| COVESA VSS signal ↔ VHAL property mapping, units | `40-sdv_vss.md` |
| VHAL C++ (`DefaultVehicleHal`, `IVehicleHardware`), native clients, watchdog | `50-native_hal.md` |
| `avc: denied`, `neverallow`, sepolicy, init domains | `60-selinux_linux.md` |

Data path for native vehicle data, top to bottom:
`HMI → CarPropertyManager → CarPropertyService (CarService) → IVehicle (AIDL) →
DefaultVehicleHal → IVehicleHardware → vehicle bus / OEM mapping (often VSS under vendor/)`.

Search `vendor/` and `device/` before blaming AOSP code.
