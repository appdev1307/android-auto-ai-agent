# Operating rules — Android 15 AAOS (customer-first)

- Obey `skills/CONTRACT.md`.
- Stack order for **native vehicle data**:
  HMI → CarService → Binder/AIDL → VHAL → VSS/vendor mapping → SELinux if avc.
- **Projection (AACP = Android Auto + Apple CarPlay)** is a separate path
  (host/session/transport/audio focus). Do not treat projection failures as
  `Vehicle.*` mapping bugs — see `34-aacp_projection.md`.
- Prefer `vendor/` + `device/` evidence before blaming AOSP.
- Tools first; never invent paths, prop ids, or diffs.
- Skills: binder `32`, services `33`, AAOS apps `30`, AACP `34`, native `50`,
  SELinux `60`, core AAOS `10`, patch/UT `20`.
- Human review: VHAL / AIDL / SELinux / power / binder / projection host / native.
