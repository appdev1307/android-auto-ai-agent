# Horizontal skill pack — AAOS application / Car UI (HMI)

Use when the bug touches Car apps, Car UI lib, CarPropertyManager, or HMI lifecycle.

## Artifacts
- `packages/apps/Car/**`, `packages/apps/Car/libs/**` (Car UI Library)
- Kotlin/Java UI: Activities, Fragments, Compose (Car)
- `CarPropertyManager`, property callbacks, zone/area handling

## Patterns to check
- Property registration and listener lifecycle (`registerCallback` / `unregister`)
- Re-subscribe on resume / power-policy / display-on (A15 power path)
- AreaId / zone mismatches between UI and CarService
- Main-thread vs binder-thread callback handling
- Car UI lib theming / RROs only if the bug is visual (not property data)

## Unit tests
- JUnit4 + Robolectric or instrumentation
- Prefer existing Car test utils / fake CarPropertyManager patterns
- Name: Framework + TestName + setup / action / assert
