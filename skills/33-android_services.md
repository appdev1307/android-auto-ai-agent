# Skill — Android services (framework + CarService)

## Service kinds on AAOS IVI
- **System server / framework services** — long-lived, permissioned, binder-published.
- **CarService** (`packages/services/Car`) — vehicle-facing Java services:
  `CarPropertyService`, power, audio, input, cluster mediation, etc.
- **App services** — media browser, navigation, projection hosts; must respect
  distraction optimization and Car permissions.
- **Native services** — init `.rc` daemons (VHAL, power policy daemon); own
  SELinux domain.

## CarService / property path
- Car apps obtain `Car` then managers (`CarPropertyManager`, …).
- `CarPropertyService` sits above VHAL; it owns subscription fan-out to apps.
- Only one responsible owner should talk to VHAL for a given client class —
  apps must not open `IVehicle` directly.

## Lifecycle bugs that look like "HAL broken"
1. Service disconnected → manager callbacks stop → UI stale (re-bind missing).
2. Process death of CarService or app → subscriptions dropped.
3. Foreground service type / background limits (A14/A15) kill a listener service.
4. Sticky assumptions: service restarted but in-memory subscription table empty.

## Engineering checks
- `onServiceConnected` / `onServiceDisconnected` pair; null out binders on disconnect.
- Re-register property listeners after reconnect.
- Privileged operations: check Car permissions and system flags.
- Do not hold binder references across death without linkToDeath.

## Instrument cluster / multi-display (when relevant)
CarService mediates navigation/cluster; only permitted apps control cluster
content. Wrong permission → silent no-op, not a VHAL config bug.

## Tests
- Bind → use → unbind → bind again; callbacks resume.
- Kill CarService (debug) → client recovers without reboot.
- Permission denied path returns clear error, not infinite retry on binder.
