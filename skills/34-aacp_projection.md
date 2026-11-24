# Phone projection — Android Auto and Apple CarPlay (AACP)

## Separate stack from AAOS vehicle data
- Android Auto: the app runs on the phone, which projects a driver-optimized UI to the
  car's display. AAOS: apps are installed and run on the vehicle itself. [S1][S2]
- Apple CarPlay is Apple's projection protocol. Its head-unit side is an OEM / partner
  integration; AOSP has no CarPlay source and no public integration guide, so ground every
  claim in vendor code found by the tools. [S3]
- Bugs in the projected UI, the session, the transport, or the phone app are not VHAL / VSS
  mapping bugs. Native AAOS widgets showing vehicle data still use the CarPropertyManager
  path, even while a phone is connected.

## AAOS touch points that do affect projection
- Power policy has a `PROJECTION` component. The built-in "no user interaction" system
  policy (silent mode, garage mode, STR, STD) turns projection off; check the applied
  policy before blaming the projection host. [S4]
- Projection daemons are native services: avc denials on sockets / device nodes are
  fixed in their sepolicy domain (`60-selinux_linux.md`).

## Investigation order
1. Transport up? (USB enumeration or wireless link; disconnect loops in logcat)
2. Projection host process alive? (vendor package; find it with the tools)
3. Power policy: is `PROJECTION` on in the current policy? [S4]
4. Audio, display and input routing to the projection session
5. Only then OEM UI overlays

## Sources
- [S1] https://developer.android.com/training/cars
- [S2] https://developers.google.com/cars
- [S3] No public AOSP or Apple integration documentation; treat as OEM-specific (put facts in `hints/`)
- [S4] https://source.android.com/docs/automotive/power/power_policy
