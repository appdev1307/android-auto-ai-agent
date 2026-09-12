# Skill — Binder / AIDL IPC (Android)

Android cross-process calls use Binder. AIDL generates stubs (`Bn*` server,
`Bp*` proxy). Vehicle stack: apps → CarService → `IVehicle` (AIDL) → VHAL.

## Hard rules
1. **Do not ship large payloads over Binder.** Process binder buffer is on the
   order of ~1MB shared across concurrent transactions; single large parcels
   often fail earlier. Prefer FD / ashmem / shared memory / URI / path — not
   giant byte arrays or bitmaps in the Parcel.
2. **`TransactionTooLargeException`** — shrink the Parcel; never "fix" by
   catching and ignoring on a critical path without changing the data plane.
3. **Death recipients** — clients that hold a remote binder to CarService/VHAL
   must `linkToDeath` / native equivalent, clear local state on death, and
   re-bind / re-subscribe. Missing death handling → stuck UI after HAL crash.
4. **Threading** — sync binder calls block the calling thread. Do not call slow
   HAL work from the main thread (ANR). Server: do not block binder threads on
   bus I/O; finish async via callback.
5. **`oneway`** — async, no return value; misuse for methods that must report
   `StatusCode` hides failures.
6. **Identity** — `IVehicle` / Car APIs are permission-checked; binder identity
   is not a substitute for Car permissions.

## AAOS-relevant failure signatures
| Symptom | Likely Binder angle |
|---------|---------------------|
| `DeadObjectException` / `binderDied` after VHAL restart | no death recipient / no resubscribe |
| `TransactionTooLargeException` | oversized property batch or debug dump over binder |
| UI hangs on get property | sync binder on main thread or server blocked on binder thread |
| Works until process kill | missing re-bind on Car/Service reconnect |

## Native (C++)
- Use generated NDK/AIDL stubs; check `ScopedAStatus` / service-specific errors.
- Unlink death recipient in destructor paths; avoid double-unlink.
- For VHAL clients prefer `libvhalclient` rather than ad-hoc `IVehicle` binds
  from random daemons (SELinux + lifecycle).

## Tests
- Death: kill server process → client cleans up and recovers.
- Parcel size: reject oversized payloads at API boundary.
- Thread: callback not invoked on binder thread if API promises main/executor.
