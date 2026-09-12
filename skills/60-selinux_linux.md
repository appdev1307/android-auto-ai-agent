# Horizontal skill — SELinux (Android) + Linux service basics

## Android SELinux model (AOSP)
- Policy sources: `*.te`, `file_contexts`, `genfs_contexts`, etc.
- OEM changes belong in `device/<manufacturer>/<device>/sepolicy` via
  `BOARD_SEPOLICY_DIRS`, not by forking platform `system/sepolicy` for features.
- Android 8+ vendor / platform split: vendor policy must not break platform
  neverallows; vendor types should use `vendor_` prefix to avoid type clashes.

## neverallow
`neverallow` statements forbid dangerous patterns globally. Compatibility tests
enforce them. If a denial is backed by neverallow, the fix is redesign
(relabel, move binary to allowed partition, use existing HAL interfaces) — not
a blanket `allow`.

## Service domain checklist (init daemon)
1. `type foo, domain;` + `type foo_exec, exec_type, file_type, ...`
2. `init_daemon_domain(foo)`
3. `file_contexts`: path → `foo_exec`
4. Minimal `allow` rules for required binder / file / socket access
5. Confirm with `avc: denied` reproduction in enforcing mode

## Reading an avc denial
```
avc: denied { operation } for ... scontext=u:r:source_domain:s0
  tcontext=u:object_r:target_type:s0 tclass=...
```
- **scontext** — who is acting (often your daemon or app domain)
- **tcontext** — what is protected
- **tclass / operation** — binder, file, dir, …

Map the denial to the intentional architecture:
- App needs vehicle data → should use CarService, not open VHAL binder
- Vendor HAL needs bus device node → label the node and allow only that HAL domain

## Linux service basics (relevant to native HALs)
- init `.rc`: `service` entry, user/group, capabilities — least privilege
- Do not run vehicle HAL as root if a dedicated user exists
- Crash loops: init may restart; fix root cause (null deref on bad prop) instead
  of only widening sepolicy
- Capabilities: prefer dropping caps; `sys_ptrace` / broad admin caps are
  neverallow-sensitive

## When SELinux is not the bug
Permission errors from Car Java APIs (`SecurityException`) are often **Android
permissions**, not sepolicy. Confirm `avc: denied` before editing `.te` files.
