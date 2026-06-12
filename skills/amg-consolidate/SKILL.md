---
name: amg-consolidate
description: >-
  Maintain AMG memory: fold the co-activation log into edge weights (Hebbian +
  decay), file the session's conclusions/decisions as notes by value, and run
  threshold-gated branch compaction so the graph stays sharp and bounded. USE THIS
  at the END of a working session (before /clear), on a schedule, or when asked to
  "consolidate / clean up / compact memory", or when retrieval feels noisy. Memory
  is NOT compressed by default — compaction only triggers on branches over budget.
  Crash-safe and idempotent. Triggers: "consolidate memory", "update weights", "file
  what we decided", "the graph is getting noisy/large", "wrap up this session".
---

# AMG Consolidate

This closes the memory loop. Three concerns, in order of cost and safety:

1. **Weights** — strengthen what was used, fade what wasn't. Deterministic, no model.
2. **Salience** — promote the session's worthwhile conclusions to long-term memory.
   Capture during a session is cheap and broad; *selection happens here*, with the
   full context, so we don't have to be right in the moment.
3. **Compaction** — only when a branch is over budget, compress it the minimal staged
   amount, preserving the valuable and archiving the rest (reversibly).

The crash-safety, archival, and idempotency guarantees are in
`../amg-bootstrap/references/consistency-model.md`. The mechanical work is in
`scripts/consolidate.py`; model judgment is delegated to the `amg-consolidator`
subagent, which emits an action list that the script applies transactionally.

## When to run
End of a session (before `/clear`), on a schedule, or on request. Running it on an
unchanged, in-budget graph is cheap and safe (weights decay slightly; nothing is
compacted).

## Workflow

1. **Fold weights** (deterministic, run directly):
   ```bash
   python .claude/skills/amg-consolidate/scripts/consolidate.py weights .
   ```
   This reads `.claude/amg/work/coactivation.log` (written by retrieval), applies
   Hebbian reinforcement + decay, prunes faded edges, renormalizes `part_of`, and
   rotates the log into the archive.

2. **Capture this session's conclusions.** Before consolidating, write any decisions,
   conclusions, or forward-looking plans reached in the conversation as `notes` nodes
   (status `captured`). These are the episodic inputs the salience step judges. When
   in doubt, capture — selection happens next, not now.

3. **Plan** (deterministic):
   ```bash
   python .claude/skills/amg-consolidate/scripts/consolidate.py plan .
   ```
   Writes `.claude/amg/work/consolidation-plan.json`: branches over budget (with
   staged steps), near-duplicate candidates, and episodic notes ranked by a
   deterministic salience score (recency, frequency, bridging, provenance, type).

4. **Judge** — spawn the `amg-consolidator` subagent with the plan. It reads the plan
   and the relevant node bodies and decides, per the salience rubric and branch
   budgets, which notes to **promote / retire**, which near-duplicates to **merge**,
   which stale episodes to **summarize**, where to **introduce a sub-hub**, and what
   low-value detail to **shorten**. It emits `.claude/amg/work/actions.json`. It does
   **not** edit the graph directly.

5. **Apply** (deterministic, transactional, archived):
   ```bash
   python .claude/skills/amg-consolidate/scripts/consolidate.py apply .claude/amg/work/actions.json .
   ```

## Salience rubric (what gets promoted / protected)
Value-of-information, not topic: **novelty/surprise** (does it change the model — a
duplicate is low, a contradiction high), **decisions/commitments/plans** (high,
referenced repeatedly), **bridging** (connects many nodes / clusters),
**reusability** (a principle that holds in future sessions vs a one-off),
**provenance** (grounded in code/docs vs an unsupported guess → file as an open
question, not a fact), **recency/frequency**. Promotion raises status; it does not
delete. Decisions/ADRs and high-centrality hubs are **protected** — never collapsed
or shortened.

## Compaction is gated — by budget and by the eval
- A branch is compacted **only** if `size > branch_budget` (per-hub override, else the
  config default). Otherwise it is left untouched.
- Steps run bottom-up and stop the moment the branch is back under budget:
  `summarize_episodes → merge_near_duplicates → introduce_subhub → lossy_shorten`.
- Lossy shortening is the last resort and archives the full text first.
- **Verify the compression was safe with the eval harness**: run recall before and
  after on your labeled cases (`../amg-retrieve/scripts/eval_retrieval.py`). If recall
  drops, the compaction was too aggressive — loosen the budget or revert (the archive
  and git history make this trivial). Safety here is a measured number, not a hope.

## Reference
- `scripts/consolidate.py` — `weights`, `plan`, `apply` (+ `selftest_consolidate.py`).
- Subagent: `../../agents/amg-consolidator.md`.
- `../amg-bootstrap/references/consistency-model.md` — crash-safety & archival model.
