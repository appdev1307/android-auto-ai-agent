# Horizontal skill pack — Native HAL / C++ services

Use when the bug is in native services, non-Java HALs, or C/C++ automotive code.

## Artifacts
- `.cpp/.cc/.h` under `hardware/interfaces/**`, vendor native services
- init `.rc`, HIDL-legacy only if the query is about migration (otherwise excluded)

## Patterns to check
- Binder thread safety, `StatusCode` returns, death recipients
- Property config tables (access, changeMode, area configs)
- Power/suspend: subscriptions and timers re-armed after resume
- AOSP clang-format / project C++ style; MISRA/AUTOSAR mindset for safety-critical paths

## Unit tests
- GoogleTest + gmock for native
- VTS when validating the HAL interface contract
