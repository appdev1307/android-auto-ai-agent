# Android services on AAOS (CarService, framework, app services)

## CarService and its clients
- CarService is the only Java client of VHAL; it fans property events out to apps through
  `CarPropertyManager`. [S1]
- Apps reach CarService through `Car`; each Car manager has its own binder to the
  corresponding CarService component. [S2]
- CarService crash handling is the client's job: use `CarServiceLifecycleListener` (or a
  `ServiceConnection`), otherwise the Car library crashes the client. [S3]

## Lifecycle bugs that look like "the HAL is broken"
1. CarService restarted → managers obtained before the restart are stale → events stop.
   Re-init on `onLifecycleChanged(car, true)`. [S4]
2. Client uses a raw `ServiceConnection` → check that `onServiceDisconnected` clears the
   old binder and the reconnect path re-subscribes.
3. Power policy turned a component off → no events by design. (`10-android_automotive.md`)

## Foreground services (Android 14+)
- An app targeting Android 14 must declare `android:foregroundServiceType` for each
  foreground service, or `startForeground()` throws `MissingForegroundServiceTypeException`. [S5]
- Each type also needs its `FOREGROUND_SERVICE_<TYPE>` permission and runtime
  prerequisites. [S5]

## Health monitoring (car watchdog)
- Vendor services register a health-check client with the car watchdog with a timeout:
  critical (3 s), moderate (5 s) or normal (10 s). A client that doesn't answer in time is
  considered unhealthy and is terminated. [S6][S7]
- Java: `CarWatchdogManager.registerClient(executor, callback, TIMEOUT_*)`; answer in
  `onCheckHealthStatus`, or call `tellClientAlive` later. Unregister when done. [S6]
- Native: `ICarWatchdog::registerClient` on `android.automotive.watchdog.ICarWatchdog/default`,
  respond with `tellClientAlive(client, sessionId)`. [S6]
- VHAL health is monitored through the `VHAL_HEARTBEAT` property; a missing heartbeat
  gets VHAL killed. [S6]
- "Watchdog killed my app after boot" → check that the health callback isn't blocked by
  slow work on the same thread.

## Sources
- [S1] https://source.android.com/docs/automotive/vhal/native-client
- [S2] AAOS 101 car framework slides — https://source.android.com/static/docs/automotive/car-framework-core/gapb-2024-aaos-101-day3-carframework-core.pdf
- [S3] AOSP commit ee22003 "Force car service crash handling to client" (packages/services/Car)
- [S4] https://developer.android.com/reference/android/car/Car.CarServiceLifecycleListener
- [S5] https://developer.android.com/about/versions/14/changes/fgs-types-required
- [S6] https://source.android.com/docs/automotive/watchdog/wd_system_health
- [S7] https://source.android.com/docs/automotive/vhal/previous/car_watchdog
