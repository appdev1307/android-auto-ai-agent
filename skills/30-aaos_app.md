# Skill — AAOS applications / Car UI (native car apps)

Native **Android Automotive** apps run on the vehicle (not phone projection).
Use Car APIs; do not bind to VHAL from app UID (SELinux + architecture).

## Artifacts
- `packages/apps/Car/**`, OEM apps, Car UI Library (`packages/apps/Car/libs`)
- `Car`, `CarPropertyManager`, Car App Library / templates host where used
- RRO / Car UI overlays for branding (visual only)

## API boundary
- Vehicle data: `CarPropertyManager` (and related Car managers).
- Direct `IVehicle` from an app is the wrong layer — expect avc or design rejection.
- Templated 3P apps may render via Automotive App Host; OEM styles host components.

## Lifecycle & threading
- Register property callbacks when Car is connected; unregister on destroy/disconnect.
- Re-register after reconnect and after power/resume (stale speed UI is often this).
- Callbacks may be off main thread — post UI updates; never block binder threads.

## AreaId / permissions
- Subscribe to the area the property config actually provides.
- Missing Car permission → `SecurityException`, not a HAL `StatusCode` mystery.

## Multi-display / cluster
- Cluster content is mediated; requires the right Car permissions.
- Wrong display target is a display/cluster integration issue, not VSS.

## Relation to AACP
If the failing UI is **phone projection** (Android Auto / CarPlay), switch to
`34-aacp_projection.md` — different stack (host/session/transport).

## Unit tests
- JUnit4 + Robolectric or instrumentation; fake CarPropertyManager when available.
- register → event → unregister; resume/reconnect restores events.
