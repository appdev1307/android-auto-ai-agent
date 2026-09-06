# Patch generation & unit-test guidance (AAOS / AOSP)

Use this skill **after** localization and root-cause diagnosis are complete.
Generate only what the diagnosed root cause requires.

---

## 0. Anti-hallucination (hard rules)

- NEVER invent file paths. Every path in a patch or candidate list MUST appear
  verbatim in a tool result from THIS run.
- NEVER emit a unified diff (`diff --git`, `---`, `+++`, `@@`) for a file you
  have not successfully read with `read_source` in this conversation.
- If the target file was not read, or grounding failed, output:

  Proposed patch (draft): N/A — describe change in words only.
  <file path from tools>: <what to change, where, before → after>

- Do not invent class names, method names, or line numbers.
- Compliance (AOSP / MISRA / AUTOSAR / SELinux) applies only when editing a
  real retrieved file. Do not demonstrate compliance on fictional code.

---

## 1. Compliance baseline (mandatory)

| Language / area | Primary rules |
|-----------------|---------------|
| Java / Kotlin (CarService, HMI, apps) | Official AOSP Java Code Style + Android Kotlin Style Guide |
| C++ (VHAL, native HALs, native services) | AOSP C++ / clang-format **+ MISRA C++ / AUTOSAR C++14** |
| Linux kernel / kernel drivers | Linux kernel coding style + MISRA where applicable |
| SELinux | Do not add or change sepolicy unless root cause is an `avc: denied` / neverallow |

- 4-space indent (Java/Kotlin/C++ user-space). Never tabs.
- Match surrounding file style when editing existing code.
- No ignored exceptions, no dead code, no drive-by cleanups.

---

## 2. Location preference (customer-first)

1. If both an AOSP file and a `vendor/` or `device/` override exist → **patch the vendor/device copy**.
2. Only patch AOSP when no OEM override exists for that path.
3. Prefer a small helper in the OEM HMI / power-policy class over changing CarService or VHAL.

---

## 3. Change size rule

- Emit **only the minimal change** that fixes the diagnosed root cause.
- Forbidden: large refactors, renames, style-only edits, “while I’m here” improvements.
- If a safe fix needs more than ~30–40 lines, describe the change in words and set
  `needs_human_review: true` instead of inventing a big diff.

---

## 4. Safety gate (non-negotiable)

Any proposed change that touches:

- VHAL
- VSS / signal mapping
- Power policy / ignition / suspend-resume
- SELinux / sepolicy
- AIDL interfaces

→ **must** end with `needs_human_review: true`.

Do not claim the patch is ready to merge on these paths.

---

## 5. How to write the patch

### Preferred shape
- Unified diff ONLY when all of the following are true:
  1. You called `read_source` on that exact path in this run
  2. The change is minimal (diagnosed root cause only)
  3. Context lines are copied from the read content, not invented
- Otherwise: **words only** — no `diff --git`, no `@@`, no fake index hashes.
- Prefer a small private helper called from both init and resume/power paths.
- Keep event-driven subscriptions. Do **not** replace them with polling.

### Java / Kotlin (HMI, CarProperty client)
- Re-register `CarPropertyManager` callbacks in the power-policy listener or
  `onResume` / after Car reconnection.
- Use existing executors / handlers already present in the class.
- Null-check `Car` / `CarPropertyManager` before use.

### C++ (VHAL / native)
- Follow MISRA/AUTOSAR discipline: no dynamic allocation in hot paths if avoidable,
  explicit error handling, no reliance on unspecified behaviour.
- Keep changes local to the diagnosed function / class.
- Match the project’s clang-format.

### SELinux
- Only touch sepolicy when the diagnosed root cause is a denial.
- Prefer the smallest allow rule consistent with existing policy style; never invent domains.

### What **not** to do
- Do not change VSS mapping or `DefaultProperties.json` when mapping + VHAL emission are already correct.
- Do not widen AIDL interfaces “just in case”.
- Do not add new permissions or SELinux rules unless the root cause is a denial.

---

## 6. Unit-test guidance (AAOS-native frameworks only)

Generate **concrete test skeletons**, not vague ideas.
Choose framework by layer of the fix:

| Fix location | Framework | Style |
|--------------|-----------|--------|
| Java/Kotlin HMI or Car client | JUnit4 + Robolectric and/or instrumentation (`AndroidJUnitRunner`) | `@RunWith`, `@Test`, Truth/JUnit asserts |
| CarService Java | Same; prefer patterns in `packages/services/Car/tests/` (`carservice_unit_test`, robotests, `carservice_test`) | Mock VHAL when suite does |
| VHAL / native C++ | **GoogleTest + gmock** (`libgtest`, `libgmock`) | `TEST`, `TEST_F`, `EXPECT_*` / `ASSERT_*` |
| HAL interface compliance | VTS (gtest-based) when applicable | Follow existing VTS module layout |
| Cross-process / device | Instrumentation + atest / Trade Federation | Only if unit-level is insufficient |

### Hard rules
- Do **not** invent a new test framework or a free-standing test app.
- Prefer the test directory and `Android.bp` patterns already used next to the target code.
- Name tests after behaviour, e.g. `speedCallbackResumesAfterPowerOn`.
- For subscription/resume bugs, assertions must include:
  - After simulated power OFF→ON (or `CarPowerManager` transition), callback runs again on property change.
  - Re-register path restores updates without requiring a full ignition cycle when only process/resume was involved.
  - No unhandled NPE / binder death on the resume path.

### Output shape for UT section
If the target file was never read, still propose framework + test names + asserts in words;
do not invent test file paths.

---

## 7. Output contract (when generating a fix)

Always set `needs_human_review: true` when the patch touches VHAL, VSS, power, SELinux, or AIDL.

---

## 8. Relationship to other skills

- `android_automotive.md` → diagnosis and localization (do that first).
- This skill → only after root cause is known; turns diagnosis into a minimal compliant patch + UT ideas.
- `hints/` → customer-specific requirements, naming, chipset quirks only. Do not put general patch rules in hints.