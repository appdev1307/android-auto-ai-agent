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
import json
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
MAX_DEBATE_ROUNDS = 3   # ReConcile-style round-table; loop stops early on convergence

try:
    import yaml as _yaml
    _cfg_path = _ROOT / "data" / "config.yaml"
    if _cfg_path.exists():
        _cfg = _yaml.safe_load(_cfg_path.read_text()) or {}
        _agent_cfg = _cfg.get("agent") or {}
        MAX_SPECIALISTS = int(_agent_cfg.get("max_specialists", MAX_SPECIALISTS))
        MAX_DEBATE_ROUNDS = int(_agent_cfg.get("max_debate_rounds", MAX_DEBATE_ROUNDS))
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
    (("android auto", "carplay", "apple carplay", "projection", "aap"), "hmi"),
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
                          "compose", "registercallback", "car app"),
        "when_path": ("packages/apps", "carui", "car/libs"),
    },
    {
        "id": "binder_ipc",
        "file_substrings": ("binder_ipc", "32-binder"),
        "when_layers": {"binder", "aidl", "vhal", "carservice"},
        "when_keywords": ("binder", "death recipient", "ibinder", "oneway",
                          "transactiontoolarge", "deadobject"),
        "when_path": ("binder", "libbinder", "Bn", "Bp"),
    },
    {
        "id": "android_services",
        "file_substrings": ("android_services", "33-android"),
        "when_layers": {"carservice", "frameworks", "startup_power"},
        "when_keywords": ("carservice", "service connection", "bindservice",
                          "foreground service", "onserviceconnected"),
        "when_path": ("packages/services/car", "system_server"),
    },
    {
        "id": "aacp_projection",
        "file_substrings": ("aacp_projection", "aacp", "34-aacp"),
        "when_layers": {"hmi", "carservice", "frameworks", "customer"},
        "when_keywords": ("android auto", "carplay", "apple carplay", "projection",
                          "aap ", "wireless android auto"),
        "when_path": ("android.auto", "carplay", "projection", "gal/", "headset"),
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

# A specialist that says the target is "outside my layer" is ABSTAINING (it owns
# no opinion on a file it doesn't own), not disagreeing. Detect that so the tally
# doesn't count every other layer's reflexive "not mine" as a real dissent.
_ABSTAIN_RE = re.compile(r"\boutside (?:my|the)\b|\bnot my layer\b", re.IGNORECASE)


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
    """Fallback verdict extractor for non-JSON output. Default ABSTAIN."""
    if not text:
        return "ABSTAIN"
    m = _VERDICT_RE.search(text)
    if not m:
        return "ABSTAIN"
    v = m.group(1).upper()
    return v if v in ("AGREE", "DISAGREE") else "ABSTAIN"


def parse_specialist_json(text: str) -> dict:
    """Parse a specialist's structured verdict.

    Expected object:
      {"verdict": "AGREE|DISAGREE|ABSTAIN", "owns_layer": bool,
       "confidence": 0..1, "target": "<path>", "reason": "<one line>"}

    Robust to code fences and surrounding prose. Falls back to the legacy
    free-text parser (verdict + "outside my layer" → ABSTAIN) so a model that
    ignores the JSON contract still yields a usable, safe verdict.
    """
    raw = text or ""
    obj = None
    stripped = re.sub(r"```(?:json)?", "", raw).strip()
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            obj = None

    if isinstance(obj, dict):
        v = str(obj.get("verdict", "")).upper().strip()
        if v not in ("AGREE", "DISAGREE", "ABSTAIN"):
            v = "ABSTAIN"
        owns = bool(obj.get("owns_layer", False))
        try:
            conf = float(obj.get("confidence", 0.5))
        except Exception:
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        target = str(obj.get("target", "") or "").strip()
        reason = str(obj.get("reason", "") or "").strip()
    else:
        # legacy fallback
        v = parse_verdict(raw)
        owns = not bool(_ABSTAIN_RE.search(raw))
        if not owns:
            v = "ABSTAIN"
        conf = 0.5
        target = ""
        reason = raw.strip()[:200]

    # a non-owner has no substantive verdict
    if not owns:
        v = "ABSTAIN"
    return {"verdict": v, "owns_layer": owns, "confidence": conf,
            "target": target, "reason": reason}


def build_consensus(notes: List[dict], committed_layer: str = "") -> dict:
    """Weighted, ownership-aware consensus over structured specialist verdicts.

    Only specialists that OWN the committed file's layer cast a substantive vote;
    every other layer ABSTAINS by construction (no more counting "not mine" as a
    dissent). Owning votes are weighted by the specialist's self-reported
    confidence, so a confident owner outweighs a hesitant one.
    """
    by_layer: dict[str, str] = {}
    owners: list[dict] = []
    n_abstain = 0
    for n in notes:
        v = n.get("verdict", "ABSTAIN")
        by_layer[n.get("layer") or ""] = v
        if n.get("owns_layer") and v in ("AGREE", "DISAGREE"):
            owners.append(n)
        else:
            n_abstain += 1

    w_agree = sum(float(n.get("confidence", 0.5)) for n in owners if n["verdict"] == "AGREE")
    w_disagree = sum(float(n.get("confidence", 0.5)) for n in owners if n["verdict"] == "DISAGREE")
    n_agree = sum(1 for n in owners if n["verdict"] == "AGREE")
    n_disagree = sum(1 for n in owners if n["verdict"] == "DISAGREE")
    tot_w = w_agree + w_disagree

    committed_v = by_layer.get(committed_layer or "", "")
    force_review = False
    if tot_w == 0:
        status = "weak"          # no owner engaged — target never validated
        force_review = True
    else:
        share = w_agree / tot_w
        if w_disagree == 0 and w_agree > 0:
            status = "agree"
        elif w_agree == 0 and w_disagree > 0:
            status = "reject"
            force_review = True
        elif share >= 0.67:
            status = "agree_with_dissent"
            force_review = True
        else:
            status = "conflict"
            force_review = True

    if committed_v == "DISAGREE":
        force_review = True

    # owning specialists give no support and at least one rejects → re-localize
    reject_committed = (w_agree == 0 and w_disagree > 0)

    return {
        "status": status,
        "agree": n_agree,
        "disagree": n_disagree,
        "abstain": n_abstain,
        "w_agree": round(w_agree, 3),
        "w_disagree": round(w_disagree, 3),
        "total": len(owners),
        "by_layer": by_layer,
        "force_human_review": force_review,
        "reject_committed": reject_committed,
        "summary": (
            f"consensus={status} AGREE={n_agree}(w{w_agree:.2f}) "
            f"DISAGREE={n_disagree}(w{w_disagree:.2f}) ABSTAIN={n_abstain} "
            f"(n={len(notes)})"
        ),
    }


_OUTPUT_CONTRACT = """
## Specialist output contract (MANDATORY — JSON ONLY)
Return exactly ONE JSON object and nothing else (no prose, no code fence):
{
  "verdict": "AGREE" | "DISAGREE" | "ABSTAIN",
  "owns_layer": true | false,
  "confidence": 0.0 to 1.0,
  "target": "<path you believe is correct; the committed file if you AGREE>",
  "reason": "<one sentence, grounded in a symbol/snippet from the evidence>"
}
Rules:
- owns_layer = is the committed file genuinely part of YOUR layer?
- If it is NOT your layer → owns_layer=false and verdict="ABSTAIN".
- Only AGREE/DISAGREE when owns_layer=true.
- If you DISAGREE, put the path you think is correct in "target".
- Do not invent paths; "target" must be a real path from the evidence.
"""


def _peer_brief(notes: List[dict], exclude_layer: str) -> str:
    lines = []
    for n in notes:
        if n.get("layer") == exclude_layer:
            continue
        lines.append(
            f'- [{n.get("layer")}] verdict={n.get("verdict")} '
            f'owns={n.get("owns_layer")} conf={float(n.get("confidence", 0.5)):.2f} '
            f'target={n.get("target") or "-"} :: {(n.get("reason") or "")[:160]}'
        )
    return "\n".join(lines)


def _verdict_map(notes: List[dict]) -> dict:
    return {n.get("layer"): (n.get("verdict"), n.get("owns_layer")) for n in notes}


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

        facts = (
            f"Committed diagnosis (single source of truth):\n"
            f"- file: {dx.get('file')}\n"
            f"- layer: {dx.get('layer')}\n"
            f"- symbols: {', '.join(dx.get('symbols') or []) or '(none)'}\n"
            f"- property_ids: {', '.join(dx.get('property_ids') or []) or '(none)'}\n"
            f"- root_cause: {dx.get('root_cause')}\n\n"
            f"Target file (excerpt):\n{target_content or '(not available)'}"
        )

        # Build each specialist's system prompt once; reuse across debate rounds.
        sys_prompts: dict[str, str] = {}
        injected: dict[str, bool] = {}
        for layer in layers:
            fname = _LAYER_FILE.get(layer)
            if not fname:
                continue
            vertical = _load_spec(fname)
            if not vertical:
                continue
            horizontal = select_horizontal_skills(layer, bug, paths_for_skills)
            sp = vertical
            if contract:
                sp = contract + "\n\n---\n\n" + sp
            sp = sp + "\n\n" + _OUTPUT_CONTRACT
            if horizontal:
                sp = sp + "\n\n" + horizontal
            sys_prompts[layer] = sp
            injected[layer] = bool(horizontal)

        committed_layer = str(dx.get("layer") or "")
        notes: List[dict] = []
        prev_map = None
        rounds_run = 0

        # ReConcile-style round-table: vote, then re-vote with peers' verdicts in
        # view, until verdicts stabilize (convergence) or the round cap is hit.
        for rnd in range(max(1, MAX_DEBATE_ROUNDS)):
            round_notes: List[dict] = []
            peer_source = notes  # last round's notes
            for layer in sys_prompts:
                if rnd == 0:
                    user = (
                        f"Bug:\n{bug}\n\n{facts}\n\n"
                        f"Validate this diagnosis for the **{layer}** layer only.\n"
                        f"Return ONLY the JSON object per the contract."
                    )
                else:
                    peer = _peer_brief(peer_source, exclude_layer=layer)
                    user = (
                        f"Bug:\n{bug}\n\n{facts}\n\n"
                        f"Other specialists' verdicts (round {rnd}):\n{peer}\n\n"
                        f"Reconsider your verdict for the **{layer}** layer in light of "
                        f"the above. If the evidence shows you were wrong, change it. "
                        f"Return ONLY the JSON object."
                    )
                try:
                    resp = llm.invoke([
                        SystemMessage(content=sys_prompts[layer]),
                        HumanMessage(content=user),
                    ])
                    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
                    parsed = parse_specialist_json(raw)
                    round_notes.append({
                        "layer": layer,
                        "verdict": parsed["verdict"],
                        "owns_layer": parsed["owns_layer"],
                        "confidence": parsed["confidence"],
                        "target": parsed["target"],
                        "reason": parsed["reason"],
                        # keep a text blob so downstream hint-harvesting still works
                        "assessment": f"{parsed['verdict']} target={parsed['target']} :: {parsed['reason']}",
                        "skills_injected": injected.get(layer, False),
                    })
                except Exception:
                    continue

            if not round_notes:
                break
            notes = round_notes
            rounds_run = rnd + 1

            cur_map = _verdict_map(notes)
            cons = build_consensus(notes, committed_layer=committed_layer)
            # convergence: verdicts unchanged from last round, or a decisive,
            # dissent-free agreement was reached.
            stable = (prev_map is not None and cur_map == prev_map)
            decisive = (cons["status"] == "agree")
            if stable or decisive:
                break
            prev_map = cur_map

        consensus = build_consensus(notes, committed_layer=committed_layer)
        consensus["rounds"] = rounds_run
        out = {
            "specialist_notes": notes,
            "specialist_consensus": consensus,
            "specialist_rounds": rounds_run,
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
        rnds = consensus.get("rounds")
        tail = f" · {rnds} debate round(s)" if rnds else ""
        out.append(f"\n**Consensus:** {consensus.get('summary', '')}{tail}")
        if consensus.get("reject_committed"):
            out.append("⚠ Owning specialists reject the committed file "
                       "(no weighted support) → re-localization attempted.")
        elif consensus.get("force_human_review"):
            out.append("⚠ Specialist conflict/dissent → human review required.")
    for n in notes or []:
        inj = " (+horizontal skills)" if n.get("skills_injected") else ""
        ver = n.get("verdict") or "ABSTAIN"
        owns = n.get("owns_layer")
        conf = float(n.get("confidence", 0.5))
        tgt = n.get("target") or "-"
        reason = n.get("reason") or ""
        out.append(
            f"\n## [{n.get('layer')}] {ver} (owns={owns}, conf={conf:.2f}){inj}\n"
            f"target: {tgt}\n{reason}"
        )
    return "\n".join(out)
