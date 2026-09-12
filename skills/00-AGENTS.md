# Operating rules — Android 15 full stack (customer-first)

- Obey skills/CONTRACT.md (shared rules for all agents).
- Full stack order of investigation: HMI → CarService → AIDL → Binder → VHAL → VSS → vendor mapping.
- Boost customer/OEM paths (`vendor/`, `device/`) when ranking evidence.
- Android 15 (API 35): note FGS types, edge-to-edge/insets, background restrictions when relevant.
- Tools first, then answer. Do not invent file paths.
- Never invent paths or fake unified diffs; compliance applies only to real retrieved files.
- Patches & UTs: follow skills/*patch_and_ut* (AOSP style; MISRA/AUTOSAR for C++/native/kernel; minimal; customer-first).
- UT frameworks: JUnit4+Robolectric/instrumentation (Java); GoogleTest+gmock (VHAL/native); VTS when applicable.
- Human review required for HAL/VSS/power/SELinux/AIDL/binder/startup/frameworks seams.
- Integration-agent hooks for A15 may arrive later; keep interfaces and property contracts stable.
