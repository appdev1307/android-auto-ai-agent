# Agent contract — shared by every role

Applies to the ReAct agent, the layer specialists, and the patch / unit-test generators.
Where this file and code disagree, code wins: the verdict format, the consensus and the
review gate below mirror `agent/specialists.py` and `agent/nodes.py`.

## 1. Committed diagnosis is the single source of truth
- `commit` turns the ReAct trail into one record: `file`, `layer`, `symbols`,
  `property_ids`, `root_cause`, `candidates`. Code keeps only what appears in this run's
  tool results and recomputes `layer` from the path.
- Specialists validate that record for their own layer. They do not re-rank candidates
  or switch the target file; a different file goes in `target` with `DISAGREE`.
- Finalize renders the record. Patch and unit test bind to the committed `file` and
  `symbols`.

## 2. Grounding
- Every path, symbol and property id must appear verbatim in a tool result of this run.
  If it does not, search for it or leave it out. Never complete, shorten or guess a path.
- No unified diff for a file that `read_source` (or the grounded patch loop) did not read
  in this run. Without it, describe the change in words.

## 3. Specialist verdict (JSON only)
```json
{"verdict": "AGREE|DISAGREE|ABSTAIN", "owns_layer": true, "confidence": 0.0,
 "target": "<path from evidence>", "reason": "<one sentence citing a symbol or snippet>"}
```
- `owns_layer=false` → `verdict` must be `ABSTAIN`. A file outside your layer is not a
  disagreement.
- `DISAGREE` → `target` is the path you believe is correct, taken from the evidence.
- Debate rounds: from round 2 you see the other specialists' verdicts. Change yours only
  if the evidence shows you were wrong; agreement alone is not evidence.

## 4. Consensus and review (computed by code, no extra LLM call)
- Only owning specialists vote; votes are weighted by `confidence`.
- No owner AGREE and at least one owner DISAGREE → the target is rejected → one
  re-localization pass on the files the dissenters named.
- Human review is forced by code for layers `vhal`, `vss`, `aidl`, `binder`, `native`,
  `selinux`, `startup_power`, `frameworks`, `hidl_legacy`; when the committed file or root
  cause mentions power / SELinux / sepolicy; for any specialist dissent or
  no owner engaged; for an ungrounded diagnosis; and for a patch that failed
  apply / parse / style checks. Do not argue against the gate; say what a reviewer must check.

## 5. Skills and hints
- `skills/*.md` are platform knowledge with sources. They refine how to look and what to
  check; they never override evidence or this contract.
- Customer-specific facts (OEM naming, known handlers, chipset quirks, coding standards
  such as MISRA/AUTOSAR when a customer requires them) live in `hints/`, not here.

## 6. Change discipline
- Minimal change for the committed root cause. No refactors, renames or style-only edits.
- Prefer the customer copy (`vendor/`, `device/`) when both it and an AOSP file match.
- Match the surrounding file's style (AOSP rule "BE CONSISTENT", see `20-patch_and_ut.md`).
