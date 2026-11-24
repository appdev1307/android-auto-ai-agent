# Patch and unit-test generation (after the diagnosis is committed)

## Patch loop (the code runs it; produce what it can validate)
1. Code feeds the real file, or the enclosing method / function of a committed symbol.
2. Output only a unified diff against exactly that text, or `N/A` if no change is needed.
   Context lines must be copied from the text you were given.
3. Code applies the diff in memory, parses the result with tree-sitter, and for C/C++
   runs `clang-format` on the changed lines. On failure you get the error back:
   fix that error only, do not rewrite the patch.

## Style (AOSP)
- BE CONSISTENT: match the surrounding code, even where it differs from the rules below. [S1]
- Java: 4-space indent, 8 for line wraps, no tabs; max 100 columns; `m` prefix for
  non-public non-static fields, `s` for static fields, `ALL_CAPS` constants; braces on the
  same line; `@Override` on overrides; explicit imports. [S1]
- Java: never ignore an exception and never catch `Exception` / `Throwable` in production
  code; rethrow, wrap, or handle with a stated fallback. [S1]
- Java logging: no `System.out.println()`; never log private data; never log successes
  above VERBOSE. [S1]
- C/C++: the tree's own `.clang-format` decides style (the patch loop enforces it). Use the
  status types the file already uses (for AIDL NDK services, `ndk::ScopedAStatus`) and
  check every returned status.
- AIDL: a frozen version under `aidl_api/<name>/<N>` must never be modified; only the
  current definition is editable. Methods return status implicitly — don't add status
  codes as return values. Clients calling a new method on an older server get
  `UNKNOWN_TRANSACTION`, which must be handled. [S2]
- Customer coding standards (MISRA, AUTOSAR, …) apply only when a `hints/` file says so.

## Size and location
- Only the change the committed root cause needs; no refactors or drive-by cleanups.
- Patch the `vendor/` / `device/` copy when it overrides the AOSP file.
- Keep event-driven subscriptions; don't replace them with polling.

## Unit test (choose by the patched file)
| Patched file | Framework | Where it lives in AOSP |
|---|---|---|
| CarService Java (`packages/services/Car/service/`) | JUnit4 + Mockito, run with `atest CarServiceUnitTest` | `packages/services/Car/tests/carservice_unit_test/` [S3][S4] |
| Car API behaviour with mocked VHAL | instrumentation | `packages/services/Car/tests/carservice_test/` [S3] |
| Car app / HMI Java or Kotlin | JUnit4 (Robolectric or instrumentation, whichever the app's existing tests use) | next to the app's existing tests |
| VHAL C++ | GoogleTest + gmock | next to the implementation's existing `test/` dir |
| VHAL contract on a device | VTS `VtsHalAutomotiveVehicle_TargetTest` [S5] | a `vts/` folder under `hardware/interfaces/automotive/` [S3] |

Rules:
- The test must call or observe the symbol the patch changed, by its real name.
- Use only classes, methods and constants that appear in the target file, the patch, or
  the committed record. If a hook to observe the fix doesn't exist, say so in words.
- Name Java tests `testMethod_specificCase`. [S1]
- Reuse the neighbouring `Android.bp` test module; don't invent a new test framework.
- Mock collaborators, not the unit under test. In CarService tests, mock `Car` rather
  than using the real one: a real `Car` can kill the client when CarService restarts. [S4]

## Sources
- [S1] https://source.android.com/docs/setup/contribute/code-style
- [S2] https://source.android.com/docs/core/architecture/aidl/stable-aidl-apis
- [S3] `packages/services/Car/tests/README.md` (AOSP): test directory roles and priorities
- [S4] AOSP commit becef5b6c5 "Use mocked Car in CarPropertyManagerUnitTest" (`Test: atest CarServiceUnitTest`)
- [S5] https://source.android.com/docs/automotive/vhal/vhal_debug
