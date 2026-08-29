# Operating rules — Android 15 full stack (customer-first)

- Full stack order of investigation: HMI → CarService → AIDL → VHAL → VSS → vendor mapping.
- Boost customer/OEM paths (`vendor/`, `device/`) when ranking evidence.
- Android 15 (API 35): note FGS types, edge-to-edge/insets, background restrictions when relevant.
- Tools first, then answer. Do not invent file paths.
- Patches: unified diff only; minimal; human review required for HAL/VSS/power.
- Integration-agent hooks for A15 may arrive later; keep interfaces and property contracts stable.
