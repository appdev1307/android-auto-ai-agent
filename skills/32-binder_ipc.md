# Binder / AIDL IPC

Path: app → CarService (`ICar` and per-manager binders) → `IVehicle` (AIDL) → VHAL. [S1]

## Transaction size
- The binder transaction buffer is a fixed 1 MB per process, shared by all transactions
  in progress, so `TransactionTooLargeException` can happen even when each call is
  moderate in size. [S2]
- When it is thrown, either the request or the reply did not fit; the caller can't tell
  which and must assume partial failure. [S2]
- Fix the data plane: split large requests, cap result sizes, send essentials first. Don't
  catch and ignore it. [S2]

## Remote death
- `IBinder.linkToDeath(recipient, flags)` registers a `DeathRecipient` that is called when
  the hosting process dies. Calls to a dead binder throw `DeadObjectException`. [S3]
- `binderDied()` runs on a binder thread; synchronize shared state. Calling
  `unlinkToDeath` inside `binderDied` is a no-op. [S4]
- For CarService, prefer `Car.CarServiceLifecycleListener` (`ready=false` → stop,
  `ready=true` → re-init) over hand-written death handling. [S5]
- After CarService restarts, a client that handles the crash (ServiceConnection or
  CarServiceLifecycleListener) stays alive; a client that doesn't is crashed by the Car
  library. [S6]

## Stable AIDL (HAL interfaces)
- HAL interfaces between system and vendor use stable AIDL: `@VintfStability` and
  `stability: "vintf"` in `aidl_interface`. [S7]
- Frozen versions in `aidl_api/<name>/<N>` must never be edited; the owner may only add
  methods at the end or fields to parcelables. [S7][S8]
- New method on an older server → `UNKNOWN_TRANSACTION`; clients must handle it. [S8]

## Failure signatures
| Symptom | Check |
|---|---|
| `DeadObjectException` / UI stuck after CarService or VHAL restart | lifecycle listener / death recipient re-initializes and re-subscribes |
| `TransactionTooLargeException` | payload size, batch size, dumps over binder |
| Works until process kill | re-bind and re-subscribe path |

## Sources
- [S1] AAOS 101 car framework slides — https://source.android.com/static/docs/automotive/car-framework-core/gapb-2024-aaos-101-day3-carframework-core.pdf
- [S2] https://developer.android.com/reference/android/os/TransactionTooLargeException
- [S3] https://developer.android.com/reference/android/os/IBinder
- [S4] https://developer.android.com/reference/android/os/IBinder.DeathRecipient
- [S5] https://developer.android.com/reference/android/car/Car.CarServiceLifecycleListener
- [S6] AOSP commit ee22003 "Force car service crash handling to client" (packages/services/Car)
- [S7] https://source.android.com/docs/core/architecture/aidl/aidl-hals
- [S8] https://source.android.com/docs/core/architecture/aidl/stable-aidl-apis
