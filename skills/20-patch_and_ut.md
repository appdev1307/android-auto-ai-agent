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

### C++ (VHAL / native) — MISRA C++:2008 / AUTOSAR C++14 semantic rules

Apply these to every C++ line you add or change (the review subset — accurate, not
exhaustive). Adapt to the real retrieved code; never demonstrate on invented code.

- **No dynamic allocation on runtime/hot paths** (AUTOSAR A18-5-*): no `new`/`delete`
  or STL allocation inside callbacks, `getValues`/`setValues`, ISR-like paths. Use
  stack, `std::array`, or a pre-allocated pool. For binder objects use RAII refcounting
  (`sp<>`/`wp<>`), not owning raw pointers.
- **No C-style / functional casts** (AUTOSAR A5-2-2, MISRA 5-2-4): use `static_cast` /
  `reinterpret_cast` / `const_cast`; never cast away `const` (A5-2-3).
- **`nullptr`, never `NULL` or `0`** (AUTOSAR A4-10-1).
- **Initialize before use; no narrowing** (A8-5-0/A8-5-2): every variable initialized;
  prefer braced-init `{}` so narrowing conversions fail to compile.
- **Fixed-width integers for HAL/wire data** (`int32_t`/`uint32_t`/…), not bare `int`;
  no implicit lossy conversions (MISRA 5-0-*).
- **Check every non-void return / status** (AUTOSAR A0-1-2, MISRA 0-1-7): never ignore
  a `StatusCode` / `ndk::ScopedAStatus` / binder result; handle the error path explicitly.
- **No exceptions across HAL/native boundaries**: AOSP native is built `-fno-exceptions
  -fno-rtti` — return status codes, don't throw. No RTTI / `dynamic_cast`.
- **Braces on all control statements** even single-line (MISRA 6-3-1/6-4-*); `switch`
  has a `default` and no unintended fall-through; no `goto` (6-6-*).
- **const-correctness** (AUTOSAR A7-1-1): mark methods/params `const`; pass non-trivial
  types by `const&`.
- **No function-like or constant macros** (AUTOSAR A16-*): use `constexpr`/`inline`/
  `enum class` instead of `#define`.
- **Rule of 0/5** (A12-0-1): if you declare any of destructor/copy/move, handle all;
  prefer Rule of Zero.
- **No dead/unreachable code, no reliance on evaluation order / UB** (MISRA 0-1-*, 5-0-1).
- Keep changes local to the diagnosed function/class; match the project's `.clang-format`.

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
- **Consistency with the patch (mandatory):** the test MUST exercise the exact
  symbol the patch adds or changes — call the patched method / observe the patched
  property by its **real name from the retrieved code or your diff**. A test that
  shares no symbol with the patch is wrong: it doesn't test the fix.
- **No invented API:** do not assert on methods, fields, or constants that appear
  in neither the retrieved file nor your patch (e.g. don't invent `isSubscribed()`,
  `PowerState::ON`, or a property id you never saw). If you need an observation hook
  that doesn't exist, say so in words — don't fabricate it.
- Use the real property id / AIDL descriptor / `StatusCode` from the evidence, not a
  plausible-looking guess.
- For subscription/resume bugs, assertions must include:
  - After simulated power OFF→ON (or `CarPowerManager` transition), callback runs again on property change.
  - Re-register path restores updates without requiring a full ignition cycle when only process/resume was involved.
  - No unhandled NPE / binder death on the resume path.

### Output shape for UT section
If the target file was never read, still propose framework + test names + asserts in words;
do not invent test file paths.

### Concrete C++ skeletons (adapt to the retrieved code — do not paste verbatim)

Use real class/method names, property IDs, and service descriptors taken from the
retrieved snippets. Placeholders below (`SpeedController`, `PERF_VEHICLE_SPEED`, paths)
must be replaced with the diagnosed symbols; never invent ones you didn't see.

**GoogleTest + gmock — VHAL / native unit test**

```cpp
// <dir-of-target>/tests/SpeedControllerTest.cpp
#include <gtest/gtest.h>
#include <gmock/gmock.h>

namespace {
using ::testing::_;

// Mock only the collaborator the unit depends on.
class MockVehicleClient : public IVehicleClient {
  public:
    MOCK_METHOD(StatusCode, registerCallback,
                (const sp<IVehicleCallback>& cb, int32_t propId), (override));
};

class SpeedControllerTest : public ::testing::Test {
  protected:
    void SetUp() override {
        mClient = sp<MockVehicleClient>::make();
        mController = std::make_unique<SpeedController>(mClient);
    }
    sp<MockVehicleClient> mClient;
    std::unique_ptr<SpeedController> mController;
};

TEST_F(SpeedControllerTest, speedCallbackResumesAfterPowerOn) {
    EXPECT_CALL(*mClient, registerCallback(_, PERF_VEHICLE_SPEED)).Times(2);
    mController->init();
    mController->onPowerStateChanged(PowerState::ON);   // simulate OFF -> ON
    ASSERT_TRUE(mController->isSubscribed());
}
}  // namespace
```
```
// Android.bp next to the target
cc_test {
    name: "SpeedControllerTest",
    srcs: ["tests/SpeedControllerTest.cpp"],
    static_libs: ["libgmock"],
    shared_libs: ["libbinder", "libutils", "liblog"],
    test_suites: ["general-tests"],
}
```

**VTS — HAL interface compliance (AIDL HAL, parameterized over instances)**

```cpp
// vts/functional/VtsHalVehicleTargetTest.cpp
#include <aidl/android/hardware/automotive/vehicle/IVehicle.h>
#include <android/binder_manager.h>
#include <android/binder_process.h>
#include <gtest/gtest.h>

using ::aidl::android::hardware::automotive::vehicle::IVehicle;

class VehicleHalTest : public ::testing::TestWithParam<std::string> {
  protected:
    void SetUp() override {
        auto* binder = AServiceManager_waitForService(GetParam().c_str());
        mVehicle = IVehicle::fromBinder(ndk::SpAIBinder(binder));
        ASSERT_NE(mVehicle, nullptr);
    }
    std::shared_ptr<IVehicle> mVehicle;
};

TEST_P(VehicleHalTest, SpeedPropertyIsSupported) {
    // getPropConfigs / getValues for PERF_VEHICLE_SPEED; assert ScopedAStatus is ok.
}

INSTANTIATE_TEST_SUITE_P(
    PerInstance, VehicleHalTest,
    ::testing::ValuesIn(::android::getAidlHalInstanceNames(IVehicle::descriptor)),
    ::android::PrintInstanceNameToString);

int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    ABinderProcess_setThreadPoolMaxThreadCount(1);
    ABinderProcess_startThreadPool();
    return RUN_ALL_TESTS();
}
```
```
// VTS Android.bp
cc_test {
    name: "VtsHalVehicleTargetTest",
    defaults: ["VtsHalTargetTestDefaults"],
    srcs: ["VtsHalVehicleTargetTest.cpp"],
    static_libs: ["android.hardware.automotive.vehicle-V3-ndk"],
    test_suites: ["vts"],
}
```

Rules for the skeletons: mock the collaborator, not the unit under test; one behaviour
per `TEST_F`/`TEST_P`; `EXPECT_*` for soft checks, `ASSERT_*` where continuing is unsafe;
follow the `-ndk` backend and `test_suites: ["vts"]` for VTS; reuse the neighbouring
`Android.bp`/test-dir layout rather than inventing one.

---

## 7. Output contract (when generating a fix)

Always set `needs_human_review: true` when the patch touches VHAL, VSS, power, SELinux, or AIDL.

---

## 8. Relationship to other skills

- `android_automotive.md` → diagnosis and localization (do that first).
- This skill → only after root cause is known; turns diagnosis into a minimal compliant patch + UT ideas.
- `hints/` → customer-specific requirements, naming, chipset quirks only. Do not put general patch rules in hints.