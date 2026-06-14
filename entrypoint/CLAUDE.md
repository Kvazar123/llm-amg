# Project memory

<!-- Standard Claude Code project memory. Loaded at the start of every session,
     including after /clear. Keep this file small; it is paid on every session. -->

## AMG — Associative Memory Graph

This project uses AMG: a persistent, typed knowledge graph with hierarchical
summaries and spreading-activation retrieval, stored under `.claude/amg/`. It lets
you work on one part of the project in a clean context window while still seeing the
strategic surround — purpose, related code, prior decisions — instead of loading the
whole codebase.

### Activation gate
At session start, check whether `.claude/amg/config.yml` exists with `active: true`.
- **Not present or `active: false`** → AMG is OFF. Behave as a normal Claude Code
  session. Do not create the graph and do not change behavior.
- **Present and active** → AMG is ON. Follow the operating loop below.

### Operating loop (only when AMG is ON)

1. **Heal, then sync the graph with disk.** At session start, first replay any
   unfinished write from a previous run — this is cheap and is how the system
   self-recovers after a crash or an interrupted session:
   ```
   python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
   python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
   ```
   Then, if the configured source folders (`mirror_path` / `absorb_path`) may have
   changed since last session (or this is the first session), run the
   **`amg-bootstrap`** skill before task work. Building from empty and reconciling are
   the same operation — crash-safe and idempotent, so re-running never duplicates or
   loses anything.

2. **Retrieve before you work.** For each task, FIRST assemble context from the graph
   via the **`amg-retrieve`** skill (seed → spreading activation → a budgeted context
   pack), then do the work. Do not dump the whole codebase into context.

3. **Capture as you go; consolidate at the end.** When a decision, conclusion, open
   question, or forward-looking plan emerges, capture it with the safe note API — do
   NOT hand-edit files under `nodes/`:
   ```
   python .claude/skills/amg-bootstrap/scripts/notes.py add --type decision --summary "..." [--body "..."] [--tags "a,b"]
   ```
   (types: `note` / `decision` / `adr` / `open_question` / `plan`). This is cheap,
   transactional, and does NOT require a bootstrap (bootstrap is only for source files,
   not for your reasoning). At session end, or before `/clear`, run the
   **`amg-consolidate`** skill once to fold the session's co-activations into edge
   weights and maintain memory. Conclusions that live only in chat are lost on
   `/clear`; the graph is where they survive, so capture before clearing.

### Where things are
- The graph: `.claude/amg/nodes/` — the source of truth, one file per node — plus
  `work/` (scratch), `journal/` (crash-recovery state, empty when idle), `archive/`,
  and `log.md` (the human-readable action log).
- Activation / sources / tunables: `.claude/amg/config.yml`.
- Your source folders (whatever `mirror_path` / `absorb_path` point to) are
  **read-only**. Never modify them as a side effect of maintaining the graph; they
  change only when the user's task changes them.
- AMG's own skills, agents, and scripts (`.claude/skills/amg-*`, `.claude/agents/amg-*`)
  are infrastructure. Do **not** edit them as part of task work. If one appears to
  have a bug or limitation, report it to the user rather than patching it live, so the
  canonical, tested version stays authoritative.

### If anything looks inconsistent
Run, from the project root:
```
python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
python .claude/skills/amg-bootstrap/scripts/reconcile.py bootstrap .
```
This replays any unfinished write, clears a stale lock, and re-establishes equality
with the code/docs on disk.
