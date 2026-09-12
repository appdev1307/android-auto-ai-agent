# Horizontal skill — AAOS application / Car UI (HMI)

Grounded in AOSP Car API guidance: apps use `CarPropertyManager` and Car
library APIs; they must not bind to VHAL directly (SELinux and architecture).

## Artifacts
- `packages/apps/Car/**` (Settings, launcher, OEM apps)
- `packages/apps/Car/libs/**` (Car UI Library)
- App code using `Car`, `CarPropertyManager`, zone/area APIs

## Engineering checks
1. **API boundary** — only Car APIs for vehicle data; no `IVehicle` from app UID.
2. **Lifecycle** — register listeners when Car is connected; unregister on
   disconnect/destroy; re-register after reconnect and after power/resume.
3. **Threading** — property callbacks may arrive off the main thread; UI updates
   must post to main; never block binder threads.
4. **AreaId / zone** — match the area config exposed by property config; global
   vs seat/window/mirror areas are a common mismatch source.
5. **Permissions** — missing Car permissions surface as SecurityException, not
   as "HAL broken".
6. **RRO / theming** — only relevant for visual bugs; do not "fix" missing
   speed data by changing layouts.

## Power / resume (A15-relevant)
Foreground service type rules and background limits can kill long-lived
listeners if the app mis-declares lifecycle. Prefer explicit re-subscribe on:

- `onResume` / app visible again
- Car lifecycle connected callbacks
- Power policy change listeners when the app is privileged enough to use them

## Unit tests
- JUnit4 + Robolectric or instrumentation
- Fake/mock `CarPropertyManager` patterns from Car test helpers when present
- Assert: register → event delivered → unregister → no further delivery
- After simulated disconnect/resume: callbacks flow again
