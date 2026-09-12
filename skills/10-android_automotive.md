# Android Automotive OS (AAOS) full-stack diagnostic skill

Grounded in AOSP automotive documentation (source.android.com): VHAL AIDL,
property configuration, power policy, CarPropertyManager path, and SELinux
client rules. Target: Android 14+ / 15 (AIDL VHAL). HIDL VHAL is legacy and
must not be used as the fix pattern unless the bug is explicitly a migration.

## 1. Stack data path (localize the first broken hop)

```
App / HMI
  → CarPropertyManager (Java API)
  → CarPropertyService / CarService
  → AIDL IVehicle (android.hardware.automotive.vehicle)
  → VHAL implementation (DefaultVehicleHal + IVehicleHardware)
  → vehicle bus / OEM mapping (often VSS ↔ property under vendor/)
```

Rules:
- Apps must use Car APIs (`CarPropertyManager`), not talk to VHAL directly.
  SELinux blocks direct app→VHAL access (AOSP native-client guidance).
- Native daemons use `libvhalclient` (`IVhalClient`) from Android 13+, not ad-hoc binder.
- OEM behavior often diverges in `vendor/` and `device/` — search those before blaming AOSP.

## 2. Logcat / symptom → starting layer

| Signature | Start layer | What to verify first |
|-----------|-------------|----------------------|
| `FATAL EXCEPTION` / NPE in Car app, Fragment, Compose | HMI | null property value, wrong areaId, callback thread |
| `CarPropertyService` / `CarPropertyManager` | CarService | subscription table, permission, property config cache |
| `TransactionTooLarge` / `DeadObjectException` / `IVehicle` | AIDL / binder | parcel size, binder death, service restart |
| `VehicleHal` / `getValues` / `setValues` / `StatusCode` | VHAL | prop config, StatusCode, hardware backend |
| `Vehicle.*` unknown / mapping / catalog | VSS / vendor mapping | signal path string, type/unit, missing map entry |
| `avc: denied` / `neverallow` | SELinux | domain, allow rule, vendor sepolicy only |
| `PERMISSION_` / `SecurityException` (Car) | permission | Car permission / vendor extension permission |
| Stops updating after ignition / suspend / resume | startup_power + client | power policy listener + re-subscribe |

## 3. VHAL property model (AOSP)

Property identity is not a free integer. System properties are defined in
`VehicleProperty.aidl` / property AIDL (Android 14+ split). Config fields that
must match the published spec:

- **access**: `READ` | `WRITE` | `READ_WRITE` (system props: only allowed modes)
- **changeMode**: `STATIC` | `ON_CHANGE` | `CONTINUOUS`
- **area**: GLOBAL or area bitmasks; per-area access must be consistent
- **sample rate**: continuous properties only; subscription Hz must be within min/max

AIDL `IVehicle` surface (conceptual):

- `getAllPropConfigs` / `getPropConfigs`
- `getValues` / `setValues` (async + callback)
- `subscribe` / `unsubscribe` with `SubscribeOptions` (propId, areaId, sample rate)

Status handling that causes real field bugs:

- `OK` — success
- `TRY_AGAIN` — transient; client should retry (not treat as permanent failure)
- `NOT_AVAILABLE` — e.g. property powered off / not ready
- `INVALID_ARG` — bad propId/areaId/value
- `INTERNAL_ERROR` — HAL/backend failure

Vendor properties: last resort; default permission
`android.car.Car.PERMISSION_VENDOR_EXTENSION`; prefer system properties first
(AOSP special-properties guidance).

## 4. Subscription and "value not updating"

For continuous properties, VHAL emits by sample rate (or bus rate). For
ON_CHANGE, emit on value/status change.

Typical break points when mapping is correct but UI is stale:

1. **Client** did not re-register `CarPropertyManager.registerCallback` /
   listener after Car connection drop or process death.
2. **Power policy** transition tore down listeners; no re-subscribe on
   `CarPowerManager` policy change / lifecycle resume.
3. **CarService** subscription not restored after VHAL binder death.
4. **VHAL** unsubscribe on sleep and never subscribe again on wake.
5. AreaId mismatch: subscribed area ≠ producing area.

Trace one property end-to-end before proposing a patch.

## 5. Power policy (AOSP automotive power)

Car power policy daemon is the system source of truth for power policy state.
It interacts with VHAL via special properties, including:

- `POWER_POLICY_REQ` / `POWER_POLICY_GROUP_REQ` (VHAL → policy daemon)
- `CURRENT_POWER_POLICY` (daemon → VHAL when others change policy)

Native clients can use `ICarPowerPolicyServer` / change callbacks.
Java privileged modules use `CarPowerManager` (get/apply policy, register
listeners). App-layer fixes that ignore power policy will regress on ignition
cycles.

## 6. SELinux (Android model, automotive clients)

- Do not edit platform `system/sepolicy` for OEM features; put policy under
  `device/<oem>/<device>/sepolicy` and `BOARD_SEPOLICY_DIRS`.
- Vendor types should be namespaced (`vendor_`) to avoid duplicate type errors.
- Init-started daemons need their **own domain** (`init_daemon_domain`), not
  shared permissive domains.
- `neverallow` rules are enforced across devices — do not "fix" denials by
  punching holes that violate neverallows; relabel or move the access to an
  allowed path.
- Native VHAL clients need explicit binder allows; apps go through CarService.

Log pattern: `avc: denied { ... } for ... scontext=... tcontext=...`.
Fix domain is sepolicy, not the Java caller, when the call path is intentional.

## 7. Investigation order (customer-first)

1. Confirm symptom and property/signal id from logcat (no invented ids).
2. Search `vendor/` + `device/` overlays for mapping, RRO, custom HAL, HMI.
3. Walk the data path hop-by-hop until the first wrong/missing value.
4. Only then change AOSP paths; prefer minimal OEM overlay fixes.
5. Any VHAL / AIDL / SELinux / power change → human review.

## 8. What this skill is not

Not a full Android framework encyclopedia (Activity, media, telephony, ...).
Not a substitute for code evidence from RAG. Use tools; bind to retrieved paths.
