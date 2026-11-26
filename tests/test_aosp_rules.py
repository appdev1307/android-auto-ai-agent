"""Offline tests for agent/aosp_rules.py: python tests/test_aosp_rules.py (exit 1 on failure)."""
import sys; from pathlib import Path as _P; sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
from agent.aosp_rules import check_patch, check_ut, prompt_rules
from retrieval.chunker import apply_unified_diff
def run(name, path, orig, diff, exp, root=None):
    patched = apply_unified_diff(orig, diff)
    assert patched is not None, name+": diff did not apply"
    r = check_patch(path, orig, patched, diff, aosp_root=root)
    got = sorted({v.rule for v in r.violations})
    ok = got == sorted(exp)
    print(("OK  " if ok else "FAIL"), name, got, "" if ok else f"expected {exp}")
    if not ok:
        for v in r.violations: print("    ", v.render())
    return ok
res=[]
VP = """package android.hardware.automotive.vehicle;
@VintfStability
@Backing(type="int")
enum VehicleProperty {
    INVALID = 0x00000000,
    PERF_VEHICLE_SPEED = 0x0207 + 0x10000000 + 0x01000000
            + 0x00600000, // VehiclePropertyGroup.SYSTEM, VehicleArea.GLOBAL, VehiclePropertyType.FLOAT
    HVAC_POWER_ON = 0x0510 + 0x10000000 + 0x05000000
            + 0x00200000, // VehiclePropertyGroup.SYSTEM, VehicleArea.SEAT, VehiclePropertyType.BOOLEAN
}
"""
P="hardware/interfaces/automotive/vehicle/aidl/android/hardware/automotive/vehicle/VehicleProperty.aidl"
res.append(run("aidl change existing enum value", P, VP, """--- a/x
+++ b/x
@@ -6,2 +6,2 @@
     PERF_VEHICLE_SPEED = 0x0207 + 0x10000000 + 0x01000000
-            + 0x00600000, // VehiclePropertyGroup.SYSTEM, VehicleArea.GLOBAL, VehiclePropertyType.FLOAT
+            + 0x00400000, // VehiclePropertyGroup.SYSTEM, VehicleArea.GLOBAL, VehiclePropertyType.FLOAT
""", ["AIDL-STABLE-CHANGE", "VHAL-PROPID"]))
res.append(run("aidl add valid new prop at end", P, VP, """--- a/x
+++ b/x
@@ -9,2 +9,4 @@
             + 0x00200000, // VehiclePropertyGroup.SYSTEM, VehicleArea.SEAT, VehiclePropertyType.BOOLEAN
+    NEW_PROP = 0x0999 + 0x20000000 + 0x01000000
+            + 0x00400000, // VehiclePropertyGroup.VENDOR, VehicleArea.GLOBAL, VehiclePropertyType.INT32
 }
""", []))
res.append(run("aidl add prop with bad area", P, VP, """--- a/x
+++ b/x
@@ -9,2 +9,3 @@
             + 0x00200000, // VehiclePropertyGroup.SYSTEM, VehicleArea.SEAT, VehiclePropertyType.BOOLEAN
+    NEW_PROP = 0x0999 + 0x20000000 + 0x02000000 + 0x00400000, // VehiclePropertyGroup.VENDOR
 }
""", ["VHAL-PROPID"]))
IF = """package android.hardware.automotive.vehicle;
@VintfStability
interface IFoo {
    void a(int x);
    int b();
}
"""
PI="hardware/interfaces/automotive/foo/aidl/android/hardware/automotive/foo/IFoo.aidl"
res.append(run("aidl insert method in middle", PI, IF, """--- a/x
+++ b/x
@@ -3,4 +3,5 @@
 interface IFoo {
     void a(int x);
+    void c();
     int b();
 }
""", ["AIDL-STABLE-APPEND"]))
res.append(run("aidl append method", PI, IF, """--- a/x
+++ b/x
@@ -4,3 +4,4 @@
     void a(int x);
     int b();
+    void c();
 }
""", []))
res.append(run("aidl change signature", PI, IF, """--- a/x
+++ b/x
@@ -4,3 +4,3 @@
-    void a(int x);
+    void a(long x);
     int b();
 }
""", ["AIDL-STABLE-CHANGE"]))
res.append(run("frozen dump", "hardware/interfaces/automotive/vehicle/aidl/aidl_api/android.hardware.automotive.vehicle/3/android/hardware/automotive/vehicle/IVehicle.aidl", IF, """--- a/x
+++ b/x
@@ -4,3 +4,4 @@
     void a(int x);
     int b();
+    void c();
 }
""", ["AIDL-FROZEN"]))
CPP = """#include "FakeVehicleHardware.h"
namespace android::hardware::automotive::vehicle::fake {
StatusCode FakeVehicleHardware::getValues(std::shared_ptr<const GetValuesCallback> callback,
                                          const std::vector<GetValueRequest>& requests) const {
    std::vector<GetValueResult> results;
    for (auto& r : requests) { results.push_back(getOne(r)); }
    (*callback)(std::move(results));
    return StatusCode::OK;
}
}
"""
PC="hardware/interfaces/automotive/vehicle/aidl/impl/fake_impl/hardware/src/FakeVehicleHardware.cpp"
res.append(run("cpp HIDL + bool field + stub", PC, CPP, """--- a/x
+++ b/x
@@ -5,4 +5,6 @@
     std::vector<GetValueResult> results;
     for (auto& r : requests) { results.push_back(getOne(r)); }
-    (*callback)(std::move(results));
+    hidl_vec<int> v;
+    value.booleanValues = {1};
+    (*callback)({});
     return StatusCode::OK;
""", ["HIDL-NEW", "VHAL-RAWVALUES", "VHAL-STUB"]))
res.append(run("cpp clean fix", PC, CPP, """--- a/x
+++ b/x
@@ -5,4 +5,5 @@
     std::vector<GetValueResult> results;
+    results.reserve(requests.size());
     for (auto& r : requests) { results.push_back(getOne(r)); }
     (*callback)(std::move(results));
     return StatusCode::OK;
""", []))
res.append(run("cpp bad include + BnVehicle + throw + bad propid", "vendor/oem/vhal/OemVhal.cpp", CPP, """--- a/x
+++ b/x
@@ -1,2 +1,6 @@
 #include "FakeVehicleHardware.h"
+#include <ndk/ScopedAStatus.h>
+class OemVhal : public BnVehicle {};
+static constexpr int32_t kOemProp = 0x2F400999;
+void f() { throw 1; }
 namespace android::hardware::automotive::vehicle::fake {
""", ["CPP-EXCEPTIONS", "NDK-INCLUDES", "VHAL-PROPID", "VHAL-SEAM"]))
HC = """#include <android/hardware/automotive/vehicle/2.0/IVehicle.h>
using ::android::hardware::automotive::vehicle::V2_0::VehiclePropValue;
Return<void> get() { hidl_vec<int> v; }
"""
res.append(run("HIDL target file exempt", "vendor/oem/legacy/OemHidl.cpp", HC, """--- a/x
+++ b/x
@@ -3,1 +3,2 @@
 Return<void> get() { hidl_vec<int> v; }
+Return<void> set() { hidl_vec<int> w; }
""", []))
BP = """cc_binary {
    name: "android.hardware.automotive.vehicle@V3-oem-service",
    vendor: true,
    shared_libs: [
        "android.hardware.automotive.vehicle-V3-ndk",
    ],
}
"""
res.append(run("bp version mismatch + cpp backend + hidl lib", "vendor/oem/vhal/Android.bp", BP, """--- a/x
+++ b/x
@@ -4,3 +4,6 @@
     shared_libs: [
         "android.hardware.automotive.vehicle-V3-ndk",
+        "android.hardware.automotive.vehicle-V4-ndk",
+        "android.hardware.automotive.vehicle-V3-cpp",
+        "libhidlbase",
     ],
""", ["HIDL-NEW", "VHAL-NDK-BACKEND", "VHAL-NDK-VERSION"]))
res.append(run("sepolicy permissive + hwservice", "vendor/oem/sepolicy/hal_vehicle.te", "type hal_vehicle_oem, domain;\n", """--- a/x
+++ b/x
@@ -1,1 +1,3 @@
 type hal_vehicle_oem, domain;
+permissive hal_vehicle_oem;
+add_hwservice(hal_vehicle_oem, fwk_vehicle_hwservice)
""", ["HIDL-NEW", "SEPOLICY-PERMISSIVE"]))
JV = """package android.car.hardware.property;
public class CarPropertyManager {
    public float getFloatProperty(int prop, int area) {
        return 0f;
    }
}
"""
res.append(run("car-lib api change", "packages/services/Car/car-lib/src/android/car/hardware/property/CarPropertyManager.java", JV, """--- a/x
+++ b/x
@@ -2,3 +2,3 @@
 public class CarPropertyManager {
-    public float getFloatProperty(int prop, int area) {
+    public float getFloatProperty(int prop, int area, boolean cached) {
         return 0f;
""", ["CAR-API-CHANGE"]))
# UT checks
ut_bad = "Target test path: t/FooTest.cpp\n```cpp\n#include <gtest/gtest.h>\nTEST(F, a) { auto v = mVehicle->getValues(req); EXPECT_TRUE(x.booleanValues[0]); }\n```"
r = check_ut(ut_bad, PC, [{"id":"XK-1","kind":"unit"}])
print("UT bad:", sorted({v.rule for v in r.violations}))
ut_java_for_cpp = "```java\nimport org.junit.Test;\npublic class T { @Test public void t(){} }\n```"
print("UT java for cpp:", sorted({v.rule for v in check_ut(ut_java_for_cpp, PC).violations}))
ut_ok = "```cpp\n#include <gtest/gtest.h>\n// XK-1\nTEST(FakeVehicleHardwareTest, getValues_returnsOneResultPerRequest) { }\n```"
print("UT ok:", [v.render() for v in check_ut(ut_ok, PC, [{"id":"XK-1","kind":"unit"}]).violations])
print(prompt_rules(PC))
print(all(res), sum(res), "/", len(res))
sys.exit(0 if all(res) else 1)
