# Patch generation & unit-test guidance (AAOS / AOSP)

Use this skill **after** localization and root-cause diagnosis are complete.
Generate only what the diagnosed root cause requires.

---

## 1. Compliance baseline (mandatory)

| Language / area | Primary rules |
|-----------------|---------------|
| Java / Kotlin (CarService, HMI, apps) | Official AOSP Java Code Style + Android Kotlin Style Guide |
| C++ (VHAL, native HALs, native services) | AOSP C++ / clang-format **+ MISRA C++ / AUTOSAR C++14** |
| Linux kernel / kernel drivers | Linux kernel coding style + MISRA where applicable |

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
- If a safe fix needs more than ~30–40 lines, describe the change in words and set `needs_human_review: true` instead of inventing a big diff.

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
- Unified diff only.
- Context lines must match the real file (agent already has grounding checks).
- Prefer extracting a small private helper (e.g. `registerSpeedListener()`, `reRegisterOnResume()`) that both the initial path and the resume/power path call.
- Keep event-driven subscriptions. Do **not** replace them with polling.

### Java / Kotlin (HMI, CarProperty client)
- Re-register `CarPropertyManager` callbacks in the power-policy listener or `onResume` / after Car reconnection.
- Use existing executors / handlers already present in the class.
- Null-check `Car` / `CarPropertyManager` before use.

### C++ (VHAL / native)
- Follow MISRA/AUTOSAR discipline: no dynamic allocation in hot paths if avoidable, explicit error handling, no reliance on unspecified behaviour.
- Keep changes local to the diagnosed function / class.
- Match the project’s clang-format.

### What **not** to do
- Do not change VSS mapping or `DefaultProperties.json` when mapping + VHAL emission are already correct.
- Do not widen AIDL interfaces “just in case”.
- Do not add new permissions or SELinux rules unless the root cause is a denial.

---

## 6. Unit-test guidance

Generate **ideas + skeleton**, not a full new test framework.

### Allowed building blocks (prefer these)
- Existing Car test utilities (`CarTestUtils`, etc.)
- Mock / fake VHAL
- Power-policy / ignition state simulation (`CarPowerManager` transitions)
- Robolectric or instrumentation tests already used in the same package

### Required assertions for subscription / resume bugs
- After simulated power OFF → ON (or equivalent resume), the callback is invoked again when the property changes.
- Re-registration restores updates without requiring a full ignition cycle if the process was only suspended.
- No NPE / binder death left unhandled on the resume path.

### Style
- Follow the same AOSP / MISRA rules as production code.
- Keep the test focused on the diagnosed failure mode.
- Name the test after the behaviour, e.g. `speedCallbackResumesAfterPowerOn`.

---

## 7. Output contract (when generating a fix)

Always set `needs_human_review: true` when the patch touches VHAL, VSS, power, SELinux, or AIDL.

---

## 8. Relationship to other skills

- `android_automotive.md` → diagnosis and localization (do that first).
- This skill → only after root cause is known; turns diagnosis into a minimal compliant patch + UT ideas.
- `hints/` → customer-specific requirements, naming, chipset quirks only. Do not put general patch rules in hints.