# Android 15 AAOS / SDV full stack

## Layers
1. **HMI** — Car UI Library, OEM apps (Android Studio), multi-display
2. **CarService** — packages/services/Car, CarPropertyService, power policy
3. **AIDL** — stable interfaces vendor ↔ system
4. **VHAL** — hardware/interfaces/automotive/vehicle, C/C++
5. **VSS** — COVESA signals, OEM YAML catalogs, signal↔property mapping (often under vendor/)

## Customer component first
OEM bugs often live in `vendor/` and `device/` (mapping, RRO, custom HAL, HMI). Search there before blaming AOSP.

## Android 15
- Stricter foreground service types
- Window insets / edge-to-edge
- Background work limits may affect property listeners if mis-implemented in app layer
