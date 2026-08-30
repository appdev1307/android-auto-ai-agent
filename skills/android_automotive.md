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

---

# Diagnostic playbook (how to localize, not just what to output)

## Step 1 — read the symptom, map to a starting layer
Use the logcat signature to pick where to search first:
- `FATAL EXCEPTION` / `NullPointerException` in a Car app or Fragment → **HMI** (vendor app)
- `CarService`, `CarPropertyService`, `CarPropertyManager` in the trace → **CarService**
- `binder` / `TransactionFailedException` / `IVehicle` → **AIDL** boundary
- `VehicleHal`, `getValues`/`setValues` timeout, `StatusCode` → **VHAL**
- signal name unknown, mapping/catalog, `Vehicle.*` not found → **VSS mapping** (usually vendor/)
- `SELinux avc: denied` / `neverallow` → **sepolicy** (SELinux), not the app
- `SecurityException permission` → manifest / permission, not the HAL

## Step 2 — trace the data path, don't jump around
For "signal/value not reaching the UI", trace the flow end-to-end and check each hop:
`HMI reads property → CarPropertyManager → CarPropertyService → AIDL IVehicle → VHAL impl → VSS→property mapping`.
Localize the FIRST hop where the value is wrong or missing. For "action from UI has no effect",
trace the same chain in reverse (set path).

## Step 3 — match symptom to common suspects
- **Not updating after ignition/resume/power state** → subscription/callback not re-registered on
  resume; power-policy listener; CarService lifecycle. Look at register/subscribe + power handlers.
- **Wrong/empty value for a signal** → VSS→VHAL mapping (wrong property id / areaId / name), or
  VHAL default config.
- **NPE opening a settings/seat/zone page** → null areaId for single-zone; missing RRO/config overlay.
- **Works on AOSP build, breaks on OEM build** → the bug is in the OEM overlay (vendor/device), not AOSP.
- **Permission/denial** → SELinux policy or manifest, not the feature code.

## Step 4 — suspect the BOUNDARIES between layers
Cross-layer bugs usually live at the seams, not inside one file:
- VSS↔VHAL: mismatched signal name / property id / areaId
- AIDL version skew: interface changed but impl not regenerated
- CarService↔VHAL: property registered in HAL but not exposed by CarService config
When two adjacent layers each "look correct", inspect the contract between them.

## Step 5 — decide AOSP vs customer
If the failing layer has both an AOSP file and a vendor/device override, inspect the **override first**
— the OEM copy is where behavior diverges. Only blame AOSP when no overlay exists for that path.
