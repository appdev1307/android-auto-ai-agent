# AAOS apps and Car UI (native in-vehicle apps)

AAOS apps run on the vehicle's own Android; Android Auto runs on the phone and projects
to the head unit — use `34-aacp_projection.md` for that. [S1]

## Vehicle data from an app
- Use Car APIs (`CarPropertyManager`), never `IVehicle`; SELinux blocks direct access. [S2]
- Connect with `Car.createCar(context, handler, waitTimeoutMs, CarServiceLifecycleListener)`.
  The listener runs on the main thread. `ready == false` means CarService crashed or was
  killed and will restart: drop manager references; on `ready == true` fetch managers
  again and re-subscribe. [S3]
- Plain `Car.createCar(context)` can return null if CarService is not ready; callers must
  handle that. [S4]
- Subscribe with `CarPropertyManager.subscribePropertyEvents(...)` (the `registerCallback`
  overloads are deprecated) and unsubscribe when the screen or service goes away. [S5]
- Override `onErrorEvent(propertyId, areaId, errorCode)` so set failures are visible. [S6]

## Car UI library and OEM branding
- The Car UI library is statically linked into system, OEM and AAOS apps, and exposes
  customizable resources (colors, dimensions, drawables) as an API. [S7]
- OEM branding is applied with build-time overlays plus generated static RROs, one per
  target app. [S7]
- An RRO cannot add new resource ids and cannot reference ids defined in the target APK
  or in itself; it may reference framework ids (`@*android:...`). A branding bug that
  needs a new resource belongs in a build-time overlay. [S8]
- RROs installed through PackageManager must be pre-installed or signed with the target
  app's key. [S8]
- Android 10+: overlaid layouts need `requireViewById()` instead of `findViewById()`. [S8]

## Checks for "UI shows a stale value"
1. Is the client re-subscribed after `onLifecycleChanged(car, true)`? [S3]
2. Is the subscribed area id one the property config lists? (`10-android_automotive.md`)
3. Did a power policy turn the relevant component off? (`10-android_automotive.md`)

## Sources
- [S1] https://developer.android.com/training/cars
- [S2] https://source.android.com/docs/automotive/vhal/native-client
- [S3] https://developer.android.com/reference/android/car/Car.CarServiceLifecycleListener
- [S4] Android automotive-developers group, CarPropertyManager example (createCar can return null) — https://groups.google.com/a/android.com/g/automotive-developers/c/oCQxyL1QXUs
- [S5] https://developer.android.com/reference/android/car/hardware/property/CarPropertyManager
- [S6] https://developer.android.com/reference/android/car/hardware/property/CarPropertyManager.CarPropertyEventCallback
- [S7] https://source.android.com/docs/automotive/hmi/car_ui
- [S8] https://source.android.com/docs/automotive/hmi/car_ui/customize
