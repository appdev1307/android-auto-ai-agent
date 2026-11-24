# SELinux on Android (vendor / OEM policy)

Confirm an `avc: denied` line in the evidence before touching policy. A Java
`SecurityException` from a Car API is an Android permission problem, not SELinux.

## Where policy goes
- Don't modify `system/sepolicy`; add or edit device policy under
  `device/<manufacturer>/<device>/sepolicy`, included via `BOARD_SEPOLICY_DIRS`. Update
  existing files where possible. [S1][S2]
- Android 8.0+: policy in `BOARD_SEPOLICY_DIRS` goes only into the vendor image. [S2]
- Prefix vendor types and attributes with `vendor_` (`type vendor_foo, domain;`):
  duplicate declarations fail to compile. [S3]
- Every new `exec_type` on the vendor partition needs the `vendor_file_type` attribute
  (enforced by neverallow); avoid labelling vendor files other than `exec_types`. [S3]

## neverallow
- neverallow rules are enforced across devices by compatibility tests. A denial that only
  a neverallow-violating `allow` would fix needs a redesign (relabel, use the existing HAL
  interface, move the code), not a broader rule. [S2]

## New native daemon checklist
```
type vendor_foo, domain;
type vendor_foo_exec, exec_type, vendor_file_type, file_type;
init_daemon_domain(vendor_foo)
binder_use(vendor_foo)                       # AIDL services
hal_client_domain(vendor_foo, hal_vehicle)   # only if it is a VHAL client
```
Plus a `file_contexts` entry labelling the binary as `vendor_foo_exec`. Start from the
least privilege that the denials prove necessary. [S2][S3][S4]

## Reading a denial
`avc: denied { <perm> } for ... scontext=u:r:<source>:s0 tcontext=u:object_r:<target>:s0 tclass=<class>`
- `scontext` is the acting domain, `tcontext` the object's type, `tclass` + perm the access.
- An app domain asking for the VHAL binder means the architecture is wrong: apps go
  through CarService. [S4]

## Sources
- [S1] https://source.android.com/docs/security/features/selinux/implement
- [S2] https://source.android.com/docs/security/features/selinux/customize
- [S3] https://source.android.com/docs/security/features/selinux/compatibility
- [S4] https://source.android.com/docs/automotive/vhal/native-client
