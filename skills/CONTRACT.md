# Agent contract — shared rules for every role

All roles (ReAct generalist, vertical specialists, finalize, patch/UT) MUST obey
this contract. It is the single source of consistency across agents.

## 1. Single source of truth
- After `commit_diagnosis`, the committed diagnosis record is authoritative:
  `file`, `layer`, `symbols`, `property_ids`, `root_cause`, `candidates`.
- Specialists **validate** that record for their layer. They do NOT re-diagnose,
  re-rank, or invent a different target file.
- Finalize **renders** the committed diagnosis; it does not re-derive it from prose.

## 2. Evidence grounding
- Every path, symbol, and property id MUST appear in tool results or the committed
  record for this run. Never invent or abbreviate paths.
- Prefer customer/OEM paths (`vendor/`, `device/`) when evidence supports them.
- No unified diff unless `read_source` (or the grounded patch loop) actually read
  that file in this run.

## 3. Layer boundaries
- Vertical specialists judge **only** their layer's artifacts.
- Do not propose fixes in another layer; say "likely outside my layer" instead.
- If the committed file is outside your layer, say so clearly and stop.

## 4. Output discipline
- Specialists: short verdict — AGREE / DISAGREE / PARTIAL, target file (from
  evidence only), one-line why grounded in a snippet or symbol.
- No markdown code fences around the whole answer unless emitting a real diff.
- Human review is mandatory for VHAL / VSS / AIDL / SELinux / power / binder /
  startup_power / frameworks native seams.

## 5. Horizontal skill packs
- Skill packs (`aaos_app`, `sdv_vss`, `native_hal`, …) are **context only**.
- They refine APIs, patterns, and test frameworks; they do not change the
  committed diagnosis or override this contract.

## 6. Safety
- Minimal change only; no large refactors.
- Patches follow `skills/*patch_and_ut*` (AOSP style; MISRA/AUTOSAR for C++/native).
- Unit tests name an existing AAOS framework (JUnit4+Robolectric, GoogleTest, VTS).


## 7. Consensus among specialists
- Each specialist starts with `VERDICT: AGREE|DISAGREE|PARTIAL`.
- Verdicts are aggregated deterministically (no extra LLM):
  - majority AGREE → consensus agree
  - DISAGREE on the committed layer, or DISAGREE ≥ AGREE → conflict → human review
  - only PARTIAL → weak consensus → prefer human review
- Specialists do not negotiate with each other; they only bind to the committed record.
- Finalize must surface the consensus summary and honour `force_human_review`.
