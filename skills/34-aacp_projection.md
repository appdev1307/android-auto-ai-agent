# Skill — AACP: Android Auto + Apple CarPlay (phone projection)

**AACP here means phone projection stacks: Android Auto and Apple CarPlay**,
not AAOS-native app model alone.

## Critical distinction (AOSP)
| | **Android Auto** | **Android Automotive (AAOS)** |
|--|------------------|-------------------------------|
| Runs on | Phone (projects UI to head unit) | Vehicle IVI hardware |
| Head unit role | Display / audio / input sink for projection | Full Android OS on car |
| Apps | Phone apps designed for Auto | AAOS apps + optional Auto-capable apps |

**Apple CarPlay** is Apple's projection protocol into the head unit (USB/wireless
depending on product). On AAOS products it is typically an **OEM/partner
integration** (sessions, audio routing, input, focus) alongside or instead of
Android Auto — not a generic Java app bug in `CarPropertyManager`.

## When the bug is projection (AACP) vs native AAOS property
| Symptom | Prefer AACP / projection path | Prefer VHAL/CarProperty path |
|---------|-------------------------------|------------------------------|
| Phone UI not on HU / black projection | session, USB/wireless, transport, host process | — |
| Media from phone, no audio focus on HU | audio policy / focus / CarAudio | — |
| Navigation card / cluster from phone | projection host + cluster mediation | — |
| Vehicle speed / HVAC on native AAOS UI | — | CarProperty / VHAL / mapping |
| "Android Auto connected but car sensors wrong" | often still VHAL path for native widgets | check both; don't assume phone owns sensors |

## Engineering investigation order (projection)
1. Transport: USB enumeration / wireless stack up? disconnect loops?
2. Host process alive (Android Auto host / CarPlay daemon as integrated by OEM)?
3. Audio and display focus routing to the projection session.
4. Input path (touch / rotary) delivered to projection surface.
5. Only then OEM UI overlays; do not "fix" projection by editing VSS catalogs.

## AAOS-side touch points (typical)
- Car services for media, focus, display policy
- OEM projection modules under `vendor/` / partner partitions
- SELinux domains for projection daemons (avc denials on device nodes or sockets)
- Power policy: projection often drops on sleep — re-start session on resume

## What not to do
- Do not treat CarPlay/Android Auto session failures as `Vehicle.Speed` mapping bugs.
- Do not invent Apple private API fixes in AOSP trees you cannot build.
- Prefer vendor/partner projection packages for CarPlay host issues.

## Tests / validation ideas
- Connect/disconnect Android Auto USB and wireless (if product supports).
- Connect/disconnect CarPlay; verify audio focus returns to native AAOS UI.
- Suspend/resume HU; projection session recovers or fails with clear log.
- avc denials during connect → sepolicy on projection domain, not CarProperty.
