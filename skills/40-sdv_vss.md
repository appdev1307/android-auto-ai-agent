# Horizontal skill pack — SDV / VSS (COVESA)

Use when the bug involves Vehicle Signal Specification, signal catalogs, or
VSS↔VHAL mapping.

## Artifacts
- COVESA `.vspec` / YAML catalogs under `vendor/vss` or signal trees
- VSS→property mappers, signal path strings (`Vehicle.Speed`, …)
- Vendor overlay signal lists

## Patterns to check
- Signal path exactness (dotted path, no invented names)
- Type / unit mismatch between VSS leaf and VHAL property config
- Missing mapping entry vs missing subscription (mapping present but no updates)
- OEM signal extensions under `vendor/` preferred over rewriting AOSP catalogs

## Unit tests
- Prefer mapping unit tests and VHAL property config checks
- VTS only when the HAL interface surface is involved
