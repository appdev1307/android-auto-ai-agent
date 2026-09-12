# Horizontal skill — SDV / VSS (COVESA) and property mapping

VSS (Vehicle Signal Specification) is a signal catalog model (COVESA). In AAOS
products it is usually **mapped** to VHAL properties in vendor code — the bug is
often in the mapping or subscription, not in the catalog file alone.

## Artifacts
- COVESA `.vspec` or OEM YAML/JSON signal trees (often under `vendor/`)
- Mapper code: signal path string ↔ VHAL property id / area
- Vendor property configs that expose mapped signals

## Engineering checks
1. **Path exactness** — `Vehicle.Speed` is not an invented synonym of another
   path; only use paths present in catalog or code.
2. **Type / unit** — float vs int, km/h vs m/s mismatches show up as "wrong
   value" not as missing subscription.
3. **Mapping present but UI stale** — do not "fix" by editing VSS leaves;
   check CarPropertyManager subscription and power re-register first.
4. **Missing mapping** — OEM should add vendor mapping / vendor property;
   prefer not to invent system properties that collide with `VehicleProperty`.
5. **Customer-first** — OEM signal extensions live under `vendor/`; search
   there before changing AOSP catalogs.

## Relation to VHAL
Mapped signals still obey VHAL access/changeMode/area rules once exposed as
properties. A continuous signal needs a continuous (or correctly sampled)
property subscription to reach HMI.

## Unit tests
- Mapping table unit tests (path → propId)
- Property config tests for access/changeMode
- VTS only when validating the HAL interface contract itself
