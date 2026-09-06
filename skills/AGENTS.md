# Operating rules — Android 15 full stack (customer-first)

- Full stack order of investigation: HMI → CarService → AIDL → VHAL → VSS → vendor mapping.
- Boost customer/OEM paths (`vendor/`, `device/`) when ranking evidence.
- Android 15 (API 35): note FGS types, edge-to-edge/insets, background restrictions when relevant.
- Tools first, then answer. Do not invent file paths.
- Patches & UTs: follow skills/patch_and_ut.md (AOSP style; MISRA/AUTOSAR for C++/native/kernel; minimal; customer-first).
- Human review required for HAL/VSS/power/SELinux/AIDL.
- Integration-agent hooks for A15 may arrive later; keep interfaces and property contracts stable.