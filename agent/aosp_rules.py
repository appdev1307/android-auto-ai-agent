"""AOSP rule oracle for generated patches and unit tests (deterministic, no LLM).

Runs inside the patch loop after "applies + parses" (agent/nodes.py
_grounded_patch_loop): an `error` is fed back to the model and the patch is
regenerated (generate → validate → regenerate, as C4 in the VHAL codegen work); a
`warn` is reported and forces human review but does not regenerate. The same rule
list is rendered into the patch / unit-test prompts (`prompt_rules`), so the model
is told exactly what the oracle checks — one source for instruction and check.

Rules check what the diff ADDS or REMOVES, never the whole file, so code the file
already had (e.g. a HIDL shim being patched on purpose) is not flagged, and a rule
only fires on lines the model wrote.

Sources (cited per rule as `src`):
  [AIDL-HAL]  https://source.android.com/docs/core/architecture/aidl/aidl-hals
  [STABLE]    https://source.android.com/docs/core/architecture/aidl/stable-aidl
  [VHAL]      https://source.android.com/docs/automotive/vhal
  [PROPS]     AOSP hardware/interfaces/automotive/vehicle/aidl/android/hardware/automotive/
              vehicle/{VehiclePropertyGroup,VehicleArea,VehiclePropertyType,RawPropValues}.aidl
  [SELINUX]   https://source.android.com/docs/security/features/selinux
  [STYLE]     https://source.android.com/docs/setup/contribute/code-style
  [C5]        VHAL codegen pipeline C5 (VTS generation): IVhalClient for get/set, the
              -V<N>-ndk lib must match the service on the device
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from retrieval.chunker import HIDL_CONTENT_TOKENS, is_hidl

# ── file kinds ───────────────────────────────────────────────────────────────
_CPP = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"}
_JAVA = {".java", ".kt"}
_SEP_NAMES = ("file_contexts", "service_contexts", "property_contexts",
              "hwservice_contexts", "genfs_contexts", "seapp_contexts")


def file_kind(path: str) -> str:
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    suf = Path(name).suffix.lower()
    if suf in _CPP:
        return "cpp"
    if suf in _JAVA:
        return "java"
    if suf == ".aidl":
        return "aidl"
    if name == "Android.bp" or suf == ".bp":
        return "bp"
    if suf in (".te", ".cil") or name in _SEP_NAMES or "/sepolicy/" in p.lower():
        return "sepolicy"
    if suf in (".rc",):
        return "rc"
    return "other"


def _is_vhal_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return ("automotive/vehicle" in p or "/vhal" in p or "vehiclehal" in p
            or "vehiclehardware" in p or "vehicleproperty" in p)


def _is_vendor_side(path: str, original: str) -> bool:
    p = "/" + path.replace("\\", "/").lower().lstrip("/")
    return (p.startswith(("/vendor/", "/device/", "/hardware/"))
            or bool(re.search(r"\bvendor\s*:\s*true", original or "")))


# ── model ────────────────────────────────────────────────────────────────────
@dataclass
class Violation:
    rule: str
    severity: str          # "error" (regenerate) | "warn" (report + review)
    msg: str
    src: str
    line: str = ""

    def render(self) -> str:
        at = f" — `{self.line.strip()[:120]}`" if self.line else ""
        return f"[{self.rule}] {self.msg}{at} ({self.src})"


@dataclass
class Report:
    checked: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warns(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warn"]

    def feedback(self) -> str:
        return "it breaks AOSP rules: " + " | ".join(v.render() for v in self.errors[:6])

    def render(self, title: str = "AOSP rules") -> str:
        head = f"## {title} ({len(self.checked)} checked)"
        if not self.violations:
            return head + "\n- all passed: " + ", ".join(self.checked)
        out = [head]
        for v in self.violations:
            out.append(f"- {'❌' if v.severity == 'error' else '⚠'} {v.render()}")
        return "\n".join(out)


def _diff_lines(diff: str) -> tuple[list[str], list[str]]:
    added, removed = [], []
    for ln in (diff or "").splitlines():
        if ln.startswith(("+++", "---")):
            continue
        if ln.startswith("+"):
            added.append(ln[1:])
        elif ln.startswith("-"):
            removed.append(ln[1:])
    return added, removed


def _code_part(line: str, kind: str) -> str:
    """Line without its trailing // or # comment (rules should not fire on prose)."""
    if kind in ("cpp", "java", "aidl", "bp"):
        return line.split("//", 1)[0]
    if kind in ("sepolicy", "rc"):
        return line.split("#", 1)[0]
    return line


# ── property-ID encoding (VehiclePropertyGroup | VehicleArea | VehiclePropertyType) ──
GROUPS = {"SYSTEM": 0x10000000, "VENDOR": 0x20000000, "BACKPORTED": 0x30000000}
AREAS = {"GLOBAL": 0x01000000, "WINDOW": 0x03000000, "MIRROR": 0x04000000,
         "SEAT": 0x05000000, "DOOR": 0x06000000, "WHEEL": 0x07000000, "VENDOR": 0x08000000}
TYPES = {"STRING": 0x00100000, "BOOLEAN": 0x00200000, "INT32": 0x00400000,
         "INT32_VEC": 0x00410000, "INT64": 0x00500000, "INT64_VEC": 0x00510000,
         "FLOAT": 0x00600000, "FLOAT_VEC": 0x00610000, "BYTES": 0x00700000,
         "MIXED": 0x00E00000}
_RAW_FIELDS = ("int32Values", "floatValues", "int64Values", "byteValues", "stringValue")

_PROP_STMT = re.compile(
    r"\b([A-Z][A-Z0-9_]+)\s*=\s*((?:0x[0-9A-Fa-f]+\s*\+\s*)+0x[0-9A-Fa-f]+)\s*,?([^\n]*)")
_PROP_LIT = re.compile(r"\b0x([0-9A-Fa-f]{8})\b")


def _decode(v: int) -> tuple[str | None, str | None, str | None]:
    g = next((k for k, x in GROUPS.items() if x == v & 0xF0000000), None)
    a = next((k for k, x in AREAS.items() if x == v & 0x0F000000), None)
    t = next((k for k, x in TYPES.items() if x == v & 0x00FF0000), None)
    return g, a, t


def _check_prop_statements(patched: str, added: list[str], rep: Report) -> None:
    """VehicleProperty.aidl style `NAME = 0xIDX + 0xGROUP + 0xAREA + 0xTYPE, // comment`.
    Evaluated on the PATCHED file (a statement may span lines) but only for
    statements the diff touched. The sum must decode to a real group / area / type,
    and the `VehiclePropertyType.X` (etc.) in the comment must match the bits."""
    rep.checked.append("VHAL-PROPID")
    touched = [a.strip() for a in added if a.strip()]
    for m in _PROP_STMT.finditer(patched or ""):
        stmt = m.group(0)
        if not any(t in stmt for t in touched):
            continue
        total = sum(int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", m.group(2)))
        g, a, t = _decode(total)
        if None in (g, a, t):
            rep.violations.append(Violation(
                "VHAL-PROPID", "error",
                f"{m.group(1)} = 0x{total:08X} is not a valid group|area|type|index ID",
                "[PROPS]", stmt.split("\n")[0]))
            continue
        for enum, got in (("VehiclePropertyGroup", g), ("VehicleArea", a),
                          ("VehiclePropertyType", t)):
            c = re.search(enum + r"\.([A-Z0-9_]+)", m.group(3))
            if c and c.group(1) != got:
                rep.violations.append(Violation(
                    "VHAL-PROPID", "error",
                    f"{m.group(1)}: comment says {enum}.{c.group(1)} but the value encodes "
                    f"{enum}.{got}", "[PROPS]", stmt.split("\n")[0]))


def _check_prop_literals(added: list[str], rep: Report) -> None:
    """Full 32-bit property-ID literals written in C++ (lines that talk about props)."""
    rep.checked.append("VHAL-PROPID")
    for ln in added:
        code = _code_part(ln, "cpp")
        if not re.search(r"prop|Prop|PROP|VehicleProperty", code):
            continue
        for lit in _PROP_LIT.findall(code):
            v = int(lit, 16)
            if v & 0xF0000000 == 0:   # small index / mask / flag, not a full ID
                continue
            if None in _decode(v):
                rep.violations.append(Violation(
                    "VHAL-PROPID", "error",
                    f"0x{lit} is not a valid 32-bit property ID (group|area|type|index)",
                    "[PROPS]", ln))


# ── patch rules ──────────────────────────────────────────────────────────────
def check_patch(path: str, original: str, patched: str, diff: str,
                aosp_root: str | None = None, bug_text: str = "") -> Report:
    kind = file_kind(path)
    added, removed = _diff_lines(diff)
    rep = Report()
    p = path.replace("\\", "/")
    target_is_hidl = is_hidl(p, original)

    # AIDL-FROZEN — never editable, whatever the change (no regenerate helps).
    rep.checked.append("AIDL-FROZEN")
    if re.search(r"/aidl_api/[^/]+/\d+/", "/" + p):
        rep.violations.append(Violation(
            "AIDL-FROZEN", "error",
            "frozen AIDL API dump (aidl_api/<name>/<N>/) must not be edited; change the "
            "current definition and let the build freeze a new version", "[STABLE]"))

    # HIDL-NEW — no HIDL in new code unless the target itself is a HIDL file.
    rep.checked.append("HIDL-NEW")
    if not target_is_hidl:
        toks = list(HIDL_CONTENT_TOKENS)
        if kind == "cpp":
            toks += ["Return<", "IHwBinder", "hardware::hidl"]
        if kind == "sepolicy":
            toks += ["hwservice_manager", "hwbinder_device", "hidl_base_hwservice",
                     "fwk_vehicle_hwservice"]
        for ln in added:
            hit = [t for t in toks if t in _code_part(ln, kind)]
            if hit:
                rep.violations.append(Violation(
                    "HIDL-NEW", "error",
                    f"HIDL construct {hit[0]!r} in new code — Android 14+/AAOS HALs are AIDL "
                    "(NDK backend: ndk::ScopedAStatus, aidl::android::hardware::...)",
                    "[AIDL-HAL]", ln))
                break
        if p.rsplit("/", 1)[-1] == "hwservice_contexts" and \
                not re.search(r"hidl|hwbinder|@\d\.\d", bug_text, re.I):
            rep.violations.append(Violation(
                "HIDL-NEW", "error", "hwservice_contexts is HIDL service labelling; AIDL "
                "services are labelled in service_contexts", "[AIDL-HAL]"))

    if kind == "cpp":
        _cpp_rules(p, original, added, rep)
    elif kind == "aidl":
        _aidl_rules(p, original, patched, added, removed, rep)
    elif kind == "bp":
        _bp_rules(p, original, added, rep, aosp_root)
    elif kind == "sepolicy":
        _sepolicy_rules(added, rep)
    elif kind == "java":
        _java_rules(p, removed, added, rep)
    return rep


def _cpp_rules(p: str, original: str, added: list[str], rep: Report) -> None:
    vhal = _is_vhal_path(p)
    if vhal:
        rep.checked.append("VHAL-RAWVALUES")
        for ln in added:
            bad = [f for f in ("booleanValues", "boolValues", "doubleValues")
                   if f in _code_part(ln, "cpp")]
            if bad:
                rep.violations.append(Violation(
                    "VHAL-RAWVALUES", "error",
                    f"RawPropValues has no field {bad[0]} (fields: {', '.join(_RAW_FIELDS)}; "
                    "BOOLEAN goes in int32Values as 0/1)", "[PROPS]", ln))
        rep.checked.append("VHAL-STUB")
        for ln in added:
            if re.search(r"\(\s*\*?\s*callback\s*\)\s*\(\s*\{\s*\}\s*\)|callback\s*\(\s*\{\s*\}\s*\)",
                         _code_part(ln, "cpp")):
                rep.violations.append(Violation(
                    "VHAL-STUB", "error",
                    "getValues/setValues answers the callback with an empty result — that "
                    "drops the requests; return one result per request", "[VHAL]", ln))
        rep.checked.append("VHAL-SEAM")
        if not re.search(r"DefaultVehicleHal\.(h|cpp)$", p):
            for ln in added:
                if re.search(r":\s*public\s+(::)?(aidl::[\w:]*::)?Bn(I)?Vehicle\b",
                             _code_part(ln, "cpp")):
                    rep.violations.append(Violation(
                        "VHAL-SEAM", "error",
                        "vendor VHAL implements IVehicleHardware; the binder service "
                        "(BnVehicle) is AOSP's DefaultVehicleHal", "[VHAL]", ln))
        _check_prop_literals(added, rep)
    rep.checked.append("NDK-INCLUDES")
    wrong = {
        "<ndk/ScopedAStatus.h>": "<android/binder_auto_utils.h>",
        "<binder/AServiceManager.h>": "<android/binder_manager.h>",
        "<aidl/android/hardware/automotive/vehicle/IVehicleHardware.h>": "<IVehicleHardware.h>",
        "<aidl/android/hardware/automotive/vehicle/DefaultVehicleHal.h>": "<DefaultVehicleHal.h>",
    }
    for ln in added:
        for bad, good in wrong.items():
            if "#include" in ln and bad in ln:
                rep.violations.append(Violation(
                    "NDK-INCLUDES", "error", f"{bad} does not exist; use {good}",
                    "[VHAL]", ln))
    rep.checked.append("CPP-EXCEPTIONS")
    if not re.search(r"\b(throw|try)\b", original or ""):
        for ln in added:
            if re.search(r"\bthrow\b|\btry\s*\{", _code_part(ln, "cpp")):
                rep.violations.append(Violation(
                    "CPP-EXCEPTIONS", "warn",
                    "C++ exceptions in a file that uses none — AOSP native code reports errors "
                    "through status / android::base::Result", "[STYLE]", ln))
                break


_DECL = re.compile(r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*(?:oneway\s+)?[\w.<>\[\], ]+\s+\w+\s*\([^;]*\)\s*;"
                   r"|^\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*[\w.<>\[\]]+\s+\w+\s*(=\s*[^;]+)?;"
                   r"|^\s*[A-Z][A-Z0-9_]*\s*=\s*[^,;]+,?")
_BLOCK = re.compile(r"\b(interface|parcelable|enum|union)\s+\w+\s*\{")


def _aidl_rules(p: str, original: str, patched: str, added: list[str], removed: list[str],
                rep: Report) -> None:
    stable = "@VintfStability" in (original or "") or "/hardware/interfaces/" in "/" + p
    if not stable:
        return
    rep.checked.append("AIDL-STABLE-CHANGE")
    added_set = {_code_part(a, "aidl").strip() for a in added}
    for ln in removed:
        s = _code_part(ln, "aidl").strip()
        if s and not s.startswith(("*", "/*")) and _DECL.match(s) and s not in added_set:
            rep.violations.append(Violation(
                "AIDL-STABLE-CHANGE", "error",
                "removes or changes an existing declaration of a stable AIDL type; only "
                "additions are compatible (new methods / fields at the end)", "[STABLE]", ln))
    # enum constants may span lines (VehicleProperty.aidl): compare values by name
    def _enum_vals(text: str) -> dict:
        out = {}
        for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*([^,;/]+?(?:\n\s*\+[^,;/]+?)*)\s*[,;}]",
                             text or "", re.M):
            out[m.group(1)] = re.sub(r"\s+", "", m.group(2))
        return out
    before, after = _enum_vals(original), _enum_vals(patched)
    for name, val in before.items():
        if name in after and after[name] != val:
            rep.violations.append(Violation(
                "AIDL-STABLE-CHANGE", "error",
                f"changes the value of existing enum constant {name} ({val} → {after[name]}) "
                "in a stable AIDL type — fix the implementation / config, not the interface",
                "[STABLE]", name))
        elif name not in after:
            rep.violations.append(Violation(
                "AIDL-STABLE-CHANGE", "error",
                f"removes existing enum constant {name} from a stable AIDL type", "[STABLE]", name))
    if "VehicleProperty" in p:
        _check_prop_statements(patched, added, rep)
    # AIDL-STABLE-APPEND: a new method / field must come after every existing one in
    # its interface / parcelable (enum constants carry explicit values; order free).
    rep.checked.append("AIDL-STABLE-APPEND")
    orig_lines = set(_code_part(l, "aidl").strip() for l in (original or "").splitlines())

    def _member(x: str) -> str:
        m = re.search(r"(\w+)\s*\(", x) or re.search(r"(\w+)\s*(?:=|;)", x)
        return m.group(1) if m else ""
    # a member changed in place is already AIDL-STABLE-CHANGE; don't report it twice
    changed = {_member(_code_part(r, "aidl")) for r in removed} - {""}
    block, depth, new_seen = None, 0, None
    for ln in (patched or "").splitlines():
        m = _BLOCK.search(ln)
        if m and depth == 0:
            block, new_seen = m.group(1), None
        depth += ln.count("{") - ln.count("}")
        if depth <= 0:
            block, depth = None, 0
            continue
        s = _code_part(ln, "aidl").strip()
        if block not in ("interface", "parcelable") or not _DECL.match(s):
            continue
        if s not in orig_lines:
            if _member(s) not in changed:
                new_seen = new_seen or ln
        elif new_seen is not None:
            rep.violations.append(Violation(
                "AIDL-STABLE-APPEND", "error",
                f"new {block} member inserted before an existing one; append it at the end",
                "[STABLE]", new_seen))
            new_seen = None


def _bp_rules(p: str, original: str, added: list[str], rep: Report,
              aosp_root: str | None) -> None:
    rep.checked.append("VHAL-NDK-BACKEND")
    vendor = _is_vendor_side(p, original)
    for ln in added:
        m = re.search(r"android\.hardware\.automotive\.vehicle(?:\.property)?-V(\d+)-(cpp|java|ndk)",
                      ln)
        if not m:
            continue
        if vendor and m.group(2) != "ndk":
            rep.violations.append(Violation(
                "VHAL-NDK-BACKEND", "error",
                f"vendor VHAL code links the NDK backend (-V{m.group(1)}-ndk), not -{m.group(2)}",
                "[AIDL-HAL]", ln))
    rep.checked.append("VHAL-NDK-VERSION")
    have = set(re.findall(r"android\.hardware\.automotive\.vehicle(?:\.property)?-V(\d+)-",
                          original or ""))
    frozen = _frozen_versions(aosp_root)
    for ln in added:
        for v in re.findall(r"android\.hardware\.automotive\.vehicle(?:\.property)?-V(\d+)-", ln):
            if have and v not in have:
                rep.violations.append(Violation(
                    "VHAL-NDK-VERSION", "error",
                    f"V{v} differs from the V{'/V'.join(sorted(have))} this module already "
                    "uses; the lib version must match the VHAL service on the device", "[C5]", ln))
            elif frozen and int(v) > max(frozen) + 1:
                rep.violations.append(Violation(
                    "VHAL-NDK-VERSION", "error",
                    f"V{v} does not exist in this tree (frozen: {sorted(frozen)}, current: "
                    f"V{max(frozen) + 1})", "[STABLE]", ln))


def _frozen_versions(aosp_root: str | None) -> list[int]:
    if not aosp_root:
        return []
    d = Path(aosp_root) / "hardware/interfaces/automotive/vehicle/aidl/aidl_api" \
        / "android.hardware.automotive.vehicle"
    try:
        return sorted(int(x.name) for x in d.iterdir() if x.is_dir() and x.name.isdigit())
    except OSError:
        return []


def _sepolicy_rules(added: list[str], rep: Report) -> None:
    rep.checked.append("SEPOLICY-PERMISSIVE")
    for ln in added:
        if re.match(r"\s*permissive\s+\w+\s*;", _code_part(ln, "sepolicy")):
            rep.violations.append(Violation(
                "SEPOLICY-PERMISSIVE", "error",
                "permissive domain — all domains are enforcing; add the specific allow rule "
                "for the avc denial instead", "[SELINUX]", ln))


def _java_rules(p: str, removed: list[str], added: list[str], rep: Report) -> None:
    if "/car-lib/" not in "/" + p:
        return
    rep.checked.append("CAR-API-CHANGE")
    added_set = {a.strip() for a in added}
    for ln in removed:
        s = ln.strip()
        if re.match(r"(public|protected)\s+[\w<>\[\], .]+\s+\w+\s*\(", s) and s not in added_set:
            rep.violations.append(Violation(
                "CAR-API-CHANGE", "warn",
                "changes a public Car API signature: needs the car-lib api/*.txt update and API "
                "review; prefer a fix that keeps the signature", "[STYLE]", ln))
            break


# ── unit-test rules ──────────────────────────────────────────────────────────
def check_ut(ut_text: str, target_path: str, criteria: list[dict] | None = None) -> Report:
    rep = Report()
    if not ut_text or "unavailable" in ut_text[:200]:
        return rep
    kind = file_kind(target_path)
    code = "\n".join(re.findall(r"```[\w+]*\n(.*?)```", ut_text, re.S)) or ut_text
    rep.checked.append("UT-HIDL")
    hit = [t for t in HIDL_CONTENT_TOKENS + ("Return<",) if t in code]
    if hit and not is_hidl(target_path):
        rep.violations.append(Violation(
            "UT-HIDL", "error", f"HIDL construct {hit[0]!r} in the test — test the AIDL "
            "interface (aidl::android::hardware::automotive::vehicle)", "[AIDL-HAL]"))
    rep.checked.append("UT-FRAMEWORK")
    gtest = bool(re.search(r"#include\s*<gtest/gtest\.h>|#include\s*<gmock/gmock\.h>", code)) and \
        bool(re.search(r"\bTEST(_F|_P)?\s*\(", code))
    junit = bool(re.search(r"\borg\.junit\b|androidx\.test", code)) and "@Test" in code
    if kind == "cpp" and not gtest:
        rep.violations.append(Violation(
            "UT-FRAMEWORK", "error", "C++ target: the test must be GoogleTest "
            "(#include <gtest/gtest.h>, TEST/TEST_F)", "[STYLE]"))
    if kind == "java" and not junit:
        rep.violations.append(Violation(
            "UT-FRAMEWORK", "error", "Java/Kotlin target: the test must be JUnit4 "
            "(org.junit / androidx.test, @Test)", "[STYLE]"))
    if _is_vhal_path(target_path) and kind == "cpp":
        rep.checked.append("UT-VHAL-CLIENT")
        if "AServiceManager_getService(" in code:
            rep.violations.append(Violation(
                "UT-VHAL-CLIENT", "warn", "use AServiceManager_waitForService(IVehicle::descriptor "
                "+ \"/default\") — getService can return null while the HAL starts", "[C5]"))
        if re.search(r"(vehicle|mVehicle|hal)\s*->\s*(getValues|setValues)\s*\(", code) and \
                "Callback" not in code:
            rep.violations.append(Violation(
                "UT-VHAL-CLIENT", "warn", "IVehicle::getValues/setValues deliver results through "
                "IVehicleCallback; use IVhalClient getValueSync/setValueSync (as AOSP "
                "VtsHalAutomotiveVehicle_TargetTest) or a callback", "[C5]"))
        bad = [f for f in ("booleanValues", "boolValues") if f in code]
        if bad:
            rep.violations.append(Violation(
                "VHAL-RAWVALUES", "error", f"RawPropValues has no field {bad[0]}", "[PROPS]"))
    rep.checked.append("UT-CRITERIA")
    for c in criteria or []:
        if c.get("kind") == "unit" and c.get("id") and c["id"] not in ut_text:
            rep.violations.append(Violation(
                "UT-CRITERIA", "warn", f"{c['id']} (unit) has no test citing it", "gate"))
    return rep


# ── prompt text (same rules the oracle checks) ───────────────────────────────
_PROMPT = {
    "all": [
        "Android 14+/AAOS HALs are AIDL. Never write HIDL in new code: no hidl_vec / "
        "hidl_string / Return<> / HIDL_FETCH_ / ::V2_0:: / @2.0 / <hidl/...> / libhidlbase / "
        "hwservice / hwbinder (unless the file you are editing is itself HIDL).",
        "Never edit a frozen AIDL dump under aidl_api/<name>/<N>/.",
    ],
    "cpp": [
        "AIDL NDK backend: aidl::android::hardware::... types, ndk::ScopedAStatus "
        "(<android/binder_auto_utils.h>), <android/binder_manager.h>; check every status.",
        "No C++ exceptions unless the file already uses them; report errors via status / "
        "android::base::Result.",
    ],
    "vhal": [
        "Vendor VHAL implements IVehicleHardware (<IVehicleHardware.h>); DefaultVehicleHal owns "
        "the binder service — do not subclass BnVehicle.",
        "RawPropValues fields are int32Values, floatValues, int64Values, byteValues, "
        "stringValue; BOOLEAN uses int32Values 0/1; the field must match the property type.",
        "getValues/setValues must answer every request (one result per request, real status), "
        "never callback({}).",
        "Property IDs = index | VehiclePropertyGroup | VehicleArea | VehiclePropertyType; the "
        "type bits must match the declared type.",
    ],
    "aidl": [
        "Stable AIDL: do not remove, rename, reorder or retype existing methods, fields or "
        "enum values; add new methods / fields at the END of the type.",
    ],
    "bp": [
        "Vendor VHAL modules link android.hardware.automotive.vehicle-V<N>-ndk with the SAME "
        "<N> the module already uses (it must match the service on the device). No HIDL libs.",
    ],
    "sepolicy": [
        "No permissive domains; AIDL services use service_contexts / add_service / "
        "hal_attribute_service, never hwservice_* macros.",
    ],
    "java": [
        "Keep public Car API (car-lib) signatures unchanged; a signature change needs the "
        "api/*.txt update and API review.",
    ],
}


def prompt_rules(path: str, for_test: bool = False) -> str:
    kind = file_kind(path)
    rules = list(_PROMPT["all"]) + _PROMPT.get(kind, [])
    if _is_vhal_path(path) and kind in ("cpp", "aidl"):
        rules += _PROMPT["vhal"]
    if for_test:
        rules = [r for r in rules if "aidl_api" not in r]
        if kind == "cpp" and _is_vhal_path(path):
            rules.append("A VHAL test that talks to the service gets it with "
                         "AServiceManager_waitForService(IVehicle::descriptor + \"/default\") "
                         "and reads / writes through IVhalClient getValueSync / setValueSync "
                         "(results of IVehicle::getValues arrive via IVehicleCallback).")
    return "AOSP rules checked by code (a violation is rejected):\n" + \
        "\n".join(f"- {r}" for r in rules)
