# Horizontal skill — Native HAL / C++ (VHAL and related)

Grounded in AOSP AIDL VHAL reference structure (`DefaultVehicleHal` +
`IVehicleHardware`), native client library guidance, and practical C++/Linux
service rules for Android HALs.

## Architecture (AOSP reference)
- Upper: `DefaultVehicleHal` — AIDL `IVehicle`, subscription manager, permission
  checks, large parcelable handling, health heartbeat.
- Lower: `IVehicleHardware` — device/bus-specific get/set/subscribe.
- Vendors implement hardware/backend logic; do not ship emulator fake hardware
  as production.

Android 14+: property configs often loaded from vendor config paths
(e.g. under `/vendor/etc/automotive/vhalconfig/` in reference designs).

## AIDL interface responsibilities
Implementations must honor async `getValues` / `setValues` callbacks and
`subscribe` options (property, area, sample rate). Duplicate request IDs and
null callbacks are invalid argument paths in the reference code — treat them as
contract bugs if reproduced.

## C++ engineering rules (HAL-relevant)
Aligned with common automotive / Android native practice (MISRA/AUTOSAR mindset,
not a full standard dump):

1. **No blocking work on binder threads** — dispatch bus I/O to worker threads;
   complete binder calls quickly; use callbacks for async results.
2. **Lifetime management** — clear ownership for callbacks and subscribed clients;
   handle binder death (unlink death recipient, drop subscription state).
3. **Deterministic error returns** — map backend failures to `StatusCode`; do not
   crash the HAL process on bad client input.
4. **Thread safety** — protect subscription maps and property caches with clear
   locking; avoid data races on publish paths.
5. **No dynamic policy in hot paths** — prefer preconfigured property tables;
   avoid unbounded allocation on every frame of continuous properties.
6. **Style** — AOSP clang-format for C++; keep patches minimal.

## Linux / process rules
- HAL runs as a native service (init `.rc`); crashes take down vehicle property
  traffic — prefer recoverable `StatusCode` over `abort` on bad input.
- File descriptors, sockets to the vehicle bus: check return codes; handle
  `EINTR` / `EAGAIN` where appropriate.
- Logging: use Android log (`ALOG*`) with prop ids, not `printf` only.

## SELinux interaction
Native VHAL clients need sepolicy allows for binder to vehicle HAL domains.
New OEM native daemons: own domain via `init_daemon_domain`, labeled exec in
`file_contexts`, vendor policy under device sepolicy tree — not platform
neverallow violations.

## Unit tests
- GoogleTest + gmock for hardware backend and subscription manager
- Tests for: subscribe → publish → unsubscribe; binder death clears client
- VTS for interface conformance when changing AIDL-facing behavior
