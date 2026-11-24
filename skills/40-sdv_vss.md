# COVESA VSS ↔ VHAL property mapping

VSS is a catalog of vehicle signals; AAOS does not ship it. On products it lives in OEM
code (often under `vendor/`) and is mapped to VHAL properties. [S1]

## VSS data entries
- Every leaf has `type` (`sensor`, `actuator`, `attribute`; parents are `branch`),
  `datatype` and `description`. Sensors are read-only, actuators can also be set,
  attributes are (near-)static values. [S2]
- `unit` is a free string in `.vspec` files; the syntax does not restrict it. Check the
  unit on both sides explicitly. [S3]
- Example leaf: `Vehicle.Speed` — `type: sensor`, `datatype: float`, `unit: km/h`. [S2]

## Mapping checks
1. **Path exactness** — use only signal paths that appear in the catalog or mapper code.
2. **Unit** — VHAL `PERF_VEHICLE_SPEED` is in `METER_PER_SEC`, negative when reversing;
   VSS `Vehicle.Speed` is `km/h`. A missing ×3.6 (or a sign drop) shows up as a "wrong
   value", not a missing subscription. [S2][S4]
3. **Type and change mode** — a VSS `float` sensor needs a `FLOAT` property; a
   continuously changing sensor maps to a `CONTINUOUS` property with sample rates, an
   attribute to `STATIC`. For system properties, type / access / change mode are fixed by
   `VehicleProperty.aidl`. [S5]
4. **No new system ids** — a signal with no system property becomes a vendor property
   (group `VENDOR`), and must not duplicate an existing `VehiclePropertyIds` entry. [S6]
5. **Mapping correct but UI stale** — don't edit the catalog; follow the subscription and
   power-policy checks in `10-android_automotive.md`.

## Tests
- Mapping table: signal path → property id / area id, including unit conversion.
- Property config: type, access, change mode, sample rates for each mapped property.

## Sources
- [S1] Project constraint: AOSP ships no VSS catalog; it comes from COVESA (cloned to `vendor/vss` in the Colab notebook) or the customer's `vendor/` tree
- [S2] https://covesa.github.io/vehicle_signal_specification/rule_set/data_entry/sensor_actuator
- [S3] https://covesa.github.io/vehicle_signal_specification/rule_set/data_entry/data_units
- [S4] `PERF_VEHICLE_SPEED` definition in AOSP `VehicleProperty` (`@unit VehicleUnit:METER_PER_SEC`, sign by direction)
- [S5] https://source.android.com/docs/automotive/vhal/property-configuration
- [S6] https://source.android.com/docs/automotive/vhal/special-properties
