# Operating rules — Android 15 full stack (customer-first)

- Obey `skills/CONTRACT.md` (shared rules for all agents).
- Full stack investigation order:
  HMI → CarService → AIDL/Binder → VHAL → VSS/vendor mapping → SELinux when avc.
- Prefer `vendor/` and `device/` evidence before blaming AOSP.
- Android 15: power policy, FGS types, binder death, and background limits matter
  for property listeners — see `10-android_automotive.md`.
- Tools first. Never invent paths, property ids, or unified diffs.
- Patches & UTs: `20-patch_and_ut.md` (AOSP style; MISRA/AUTOSAR mindset for C++).
- UT frameworks: JUnit4+Robolectric/instrumentation | GoogleTest+gmock | VTS.
- Human review required for VHAL / VSS / AIDL / SELinux / power / binder / native seams.
- Skills encode platform engineering rules; RAG supplies the concrete files for this tree.
