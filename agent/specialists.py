"""Per-layer specialist agents + horizontal skill injection + consensus.

Vertical specialists (graph nodes, capped at MAX_SPECIALISTS=5):
  vhal | aidl | binder | carservice | hmi | vss | startup_power | frameworks
  (+ native, selinux when evidence tags them)

Horizontal skill packs (prompt injection only — no extra LLM call):
  skills/*aaos_app*, *sdv_vss*, *native_hal* (auto-discovered by name)

Router:
  1) keyword-priority layers (power/binder/selinux) forced into the pool
  2) committed diagnosis.layer + candidate layers
  3) path prefixes + remaining bug keywords
  → unique list, capped at 5

Consensus (no extra LLM call):
  Parse AGREE|DISAGREE|PARTIAL from each specialist → majority / conflict flags
  → structured specialist_consensus for finalize + human-review gate
"""

from __future__ import annotations
import re
from pathlib import Path
from typing import Dict, Any, Callable, List, Optional

from langchain_core.messages import SystemMessage, HumanMessage

_ROOT = Path(__file__).resolve().parents[1]
_SPEC_DIR = _ROOT / "prompts" / "specialists"
_SKILLS_DIR = _ROOT / "skills"

_LAYER_FILE = {
    "vhal": "vhal.md",
    "aidl": "aidl.md",
    "binder": "binder.md",
    "carservice": "carservice.md",
    "hmi": "hmi.md",
    "vss": "vss.md",
    "startup_power": "startup_power.md",
    "frameworks": "frameworks.md",
    "native": "native.md",
    "selinux": "selinux.md",
    "customer": "hmi.md",
}

MAX_SPECIALISTS = 5

try:
    import yaml as _yaml
    _cfg_path = _ROOT / "data" / "config.yaml"
    if _cfg_path.exists():
        _cfg = _yaml.safe_load(_cfg_path.read_text()) or {}
        MAX_SPECIALISTS = int((_cfg.get("agent") or {}).get("max_specialists", MAX_SPECIALISTS))
except Exception:
    pass

# High-signal keywords that MUST get a specialist slot when present in the bug
# (priority pass — prevents power/binder/selinux from falling off the end).
_PRIORITY_KEYWORDS = [
    (("ignition", "resume", "suspend", "power policy", "power_policy",
      "boot", "startup", "after power"), "startup_power"),
    (("binder", "death recipient", "ibinder", "oneway", "binder died"), "binder"),
    (("selinux", "avc denied", "sepolicy", "avc:"), "selinux"),
]

_PATH_LAYER_HINTS = [
    (("hardware/interfaces/automotive", "vehiclehal", "/vhal"), "vhal"),
    ((".aidl", "/aidl/"), "aidl"),
    (("binder", "libbinder", "BnVehicle", "BpVehicle", "IBinder"), "binder"),
    (("packages/services/car", "carpropertyservice", "carservice"), "carservice"),
    (("packages/apps/car", "carui", "/hmi", "systemui"), "hmi"),
    (("/vss", "covesa", ".vspec", "signal"), "vss"),
    (("powerpolicy", "power_policy", "suspend", "resume", "ignition",
      "early_init", "init.", "/init/"), "startup_power"),
    (("frameworks/base", "frameworks/av", "system_server"), "frameworks"),
    ((".te", "sepolicy", "file_contexts"), "selinux"),
]

_KEYWORD_LAYER = [
    (("vhal", "vehiclehal", "vehicle prop", "getvalues", "setvalues"), "vhal"),
    (("aidl", "parcelable", ".aidl"), "aidl"),
    (("binder", "death recipient", "ibinder", "oneway"), "binder"),
    (("carservice", "carproperty", "car property"), "carservice"),
    (("hmi", "car ui", "compose", "activity", "fragment"), "hmi"),
    (("vss", "covesa", "vehicle.speed", "signal path"), "vss"),
    (("ignition", "resume", "suspend", "power policy", "boot", "startup"),
     "startup_power"),
    (("frameworks/base", "system server", "framework api"), "frameworks"),
    (("selinux", "avc denied", "sepolicy"), "selinux"),
]

_HORIZONTAL_PACKS = [
    {
        "id": "aaos_app",
        "file_substrings": ("aaos_app",),
        "when_layers": {"hmi", "carservice", "customer"},
        "when_keywords": ("hmi", "car ui", "carproperty", "activity", "fragment",
                          "compose", "registercallback"),
        "when_path": ("packages/apps", "carui", "car/libs"),
    },
    {
        "id": "sdv_vss",
        "file_substrings": ("sdv_vss",),
        "when_layers": {"vss", "vhal"},
        "when_keywords": ("vss", "covesa", "signal", "vehicle.", ".vspec"),
        "when_path": ("/vss", "covesa", ".vspec", "signal"),
    },
    {
        "id": "native_hal",
        "file_substrings": ("native_hal",),
        "when_layers": {"vhal", "native", "binder", "frameworks"},
        "when_keywords": ("native", "hal", ".cpp", "gtest", "vts"),
        "when_path": ("hardware/interfaces", ".cpp", ".cc", ".h"),
    },
    {
        "id": "selinux_linux",
        "file_substrings": ("selinux_linux", "selinux"),
        "when_layers": {"selinux", "native", "vhal"},
        "when_keywords": ("selinux", "avc denied", "neverallow", "sepolicy"),
        "when_path": (".te", "sepolicy", "file_contexts"),
    },
]

# Structured output line specialists must emit (consensus parser key).
_VERDICT_RE = re.compile(
    r"\b(AGREE|DISAGREE|PARTIAL)\b",
    re.IGNORECASE,
)


def _load_spec(fname: str) -> str:
    p = _SPEC_DIR / fname
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _load_contract() -> str:
    for name in ("CONTRACT.md", "00-CONTRACT.md"):
        p = _SKILLS_DIR / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def _find_skill_files(substrings: tuple) -> list:
    if not _SKILLS_DIR.is_dir():
        return []
    out = []
    for f in sorted(_SKILLS_DIR.glob("*.md")):
        low = f.name.lower()
        if any(s in low for s in substrings):
            out.append(f.read_text(encoding="utf-8"))
    return out


def route_specialist_layers(diagnosis, bug, evidence_paths=None, max_n=MAX_SPECIALISTS):
    """Pick ≤max_n vertical specialists with keyword priority pass."""
    ordered: List[str] = []

    def add(layer: str) -> None:
        if layer in _LAYER_FILE and layer not in ordered:
            ordered.append(layer)

    q = (bug or "").lower()

    # 0) Priority keyword pass — force high-signal layers first
    for keywords, layer in _PRIORITY_KEYWORDS:
        if any(k in q for k in keywords):
            add(layer)

    # 1) committed + candidates
    if diagnosis.get("layer"):
        add(str(diagnosis["layer"]))
    for c in diagnosis.get("candidates") or []:
        if isinstance(c, dict) and c.get("layer"):
            add(str(c["layer"]))

    # 2) path prefixes
    paths = []
    if diagnosis.get("file"):
        paths.append(str(diagnosis["file"]))
    for c in diagnosis.get("candidates") or []:
        if isinstance(c, dict) and c.get("path"):
            paths.append(str(c["path"]))
    for p in evidence_paths or []:
        paths.append(str(p))
    blob_paths = " ".join(paths).lower()
    for markers, layer in _PATH_LAYER_HINTS:
        if any(m.lower() in blob_paths for m in markers):
            add(layer)

    # 3) remaining bug keywords
    for keywords, layer in _KEYWORD_LAYER:
        if any(k in q for k in keywords):
            add(layer)

    return ordered[:max_n]


def select_horizontal_skills(layer, bug, paths):
    q = (bug or "").lower()
    path_blob = " ".join(paths).lower()
    chunks = []
    seen = set()
    for pack in _HORIZONTAL_PACKS:
        hit = (
            layer in pack["when_layers"]
            or any(k in q for k in pack["when_keywords"])
            or any(p in path_blob for p in pack["when_path"])
        )
        if not hit:
            continue
        for text in _find_skill_files(pack["file_substrings"]):
            key = text[:80]
            if key in seen:
                continue
            seen.add(key)
            chunks.append(text)
    if not chunks:
        return ""
    return "\n\n# Horizontal skill packs (context only)\n\n" + "\n\n".join(chunks)


def parse_verdict(text: str) -> str:
    """Extract AGREE|DISAGREE|PARTIAL from specialist text. Default PARTIAL."""
    if not text:
        return "PARTIAL"
    m = _VERDICT_RE.search(text)
    if not m:
        return "PARTIAL"
    return m.group(1).upper()


def build_consensus(notes: List[dict], committed_layer: str = "") -> dict:
    """Deterministic consensus over specialist verdicts — no extra LLM call.

    Rules:
    - majority of AGREE among parsed verdicts → status=agree
    - any DISAGREE on the committed layer → status=conflict + force review
    - any DISAGREE at all → conflict if agree count does not strictly dominate
    - all PARTIAL or empty → status=weak
    - tie AGREE vs DISAGREE → conflict
    """
    verdicts = []
    by_layer = {}
    for n in notes:
        v = parse_verdict(n.get("assessment") or "")
        layer = n.get("layer") or ""
        verdicts.append(v)
        by_layer[layer] = v
        n["verdict"] = v

    n_agree = sum(1 for v in verdicts if v == "AGREE")
    n_disagree = sum(1 for v in verdicts if v == "DISAGREE")
    n_partial = sum(1 for v in verdicts if v == "PARTIAL")
    total = len(verdicts) or 1

    committed_v = by_layer.get(committed_layer or "", "")
    force_review = False
    if committed_v == "DISAGREE":
        status = "conflict"
        force_review = True
    elif n_disagree == 0 and n_agree >= max(1, (total + 1) // 2):
        status = "agree"
    elif n_disagree > 0 and n_agree > n_disagree:
        status = "agree_with_dissent"
        force_review = True
    elif n_disagree > n_agree:
        status = "conflict"
        force_review = True
    elif n_agree == n_disagree and n_disagree > 0:
        status = "conflict"
        force_review = True
    else:
        status = "weak"
        force_review = n_partial == total

    return {
        "status": status,
        "agree": n_agree,
        "disagree": n_disagree,
        "partial": n_partial,
        "total": len(verdicts),
        "by_layer": by_layer,
        "force_human_review": force_review,
        "summary": (
            f"consensus={status} AGREE={n_agree} DISAGREE={n_disagree} "
            f"PARTIAL={n_partial} (n={len(verdicts)})"
        ),
    }


_OUTPUT_CONTRACT = """
## Specialist output contract (mandatory)
First line MUST be exactly one of:
  VERDICT: AGREE
  VERDICT: DISAGREE
  VERDICT: PARTIAL
Then 2–5 lines:
  - target file from the committed record only (or "outside my layer")
  - one-line why, grounded in a symbol/snippet from the evidence
Do not invent paths. Do not re-diagnose a different root file.
Do not propose fixes outside your layer.
"""


def make_specialists_node(llm, get_retriever):
    def specialists(state):
        dx = state.get("diagnosis") or {}
        r = get_retriever()
        bug = state.get("bug_report") or ""

        evidence_paths = []
        for e in state.get("evidence") or []:
            if isinstance(e, dict) and e.get("path"):
                evidence_paths.append(str(e["path"]))
            elif isinstance(e, str):
                evidence_paths.append(e)

        layers = route_specialist_layers(dx, bug, evidence_paths, MAX_SPECIALISTS)

        committed_file = dx.get("file") or ""
        target_content = ""
        if r is not None and committed_file:
            try:
                target_content = r.read_file(committed_file, max_chars=1600)
            except Exception:
                target_content = ""

        paths_for_skills = [committed_file] + evidence_paths
        contract = _load_contract()
        notes = []

        for layer in layers:
            fname = _LAYER_FILE.get(layer)
            if not fname:
                continue
            vertical = _load_spec(fname)
            if not vertical:
                continue
            horizontal = select_horizontal_skills(layer, bug, paths_for_skills)
            sys_prompt = vertical
            if contract:
                sys_prompt = contract + "\n\n---\n\n" + sys_prompt
            sys_prompt = sys_prompt + "\n\n" + _OUTPUT_CONTRACT
            if horizontal:
                sys_prompt = sys_prompt + "\n\n" + horizontal

            facts = (
                f"Committed diagnosis (single source of truth):\n"
                f"- file: {dx.get('file')}\n"
                f"- layer: {dx.get('layer')}\n"
                f"- symbols: {', '.join(dx.get('symbols') or []) or '(none)'}\n"
                f"- property_ids: {', '.join(dx.get('property_ids') or []) or '(none)'}\n"
                f"- root_cause: {dx.get('root_cause')}\n\n"
                f"Target file (excerpt):\n{target_content or '(not available)'}"
            )
            try:
                resp = llm.invoke([
                    SystemMessage(content=sys_prompt),
                    HumanMessage(content=(
                        f"Bug:\n{bug}\n\n{facts}\n\n"
                        f"Validate this diagnosis for the **{layer}** layer only.\n"
                        f"Start with `VERDICT: AGREE|DISAGREE|PARTIAL` then the short body."
                    )),
                ])
                txt = resp.content if isinstance(resp.content, str) else str(resp.content)
                notes.append({
                    "layer": layer,
                    "assessment": txt.strip(),
                    "skills_injected": bool(horizontal),
                })
            except Exception:
                continue

        consensus = build_consensus(notes, committed_layer=str(dx.get("layer") or ""))
        out = {
            "specialist_notes": notes,
            "specialist_consensus": consensus,
            "status": "specialists_done",
        }
        if consensus.get("force_human_review"):
            out["needs_human_review"] = True
        return out

    return specialists


def format_specialist_notes(notes, consensus: Optional[dict] = None) -> str:
    if not notes and not consensus:
        return ""
    out = ["\n\n# Specialist assessments (per layer)"]
    if consensus:
        out.append(f"\n**Consensus:** {consensus.get('summary', '')}")
        if consensus.get("force_human_review"):
            out.append("⚠ Specialist conflict/dissent → human review required.")
    for n in notes or []:
        inj = " (+horizontal skills)" if n.get("skills_injected") else ""
        ver = n.get("verdict") or parse_verdict(n.get("assessment") or "")
        out.append(f"\n## [{n.get('layer')}] {ver}{inj}\n{n.get('assessment', '')}")
    return "\n".join(out)
