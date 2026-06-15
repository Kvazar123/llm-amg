# Project memory

<!-- Standard Claude Code project memory. Loaded at the start of every session,
     including after /clear. Keep this file small; it is paid on every session. -->

## AMG — Associative Memory Graph

This project uses AMG: a persistent, typed knowledge graph with hierarchical
summaries and spreading-activation retrieval, stored under `.claude/amg/`. It lets
you work on one part of the project in a clean context window while still seeing the
strategic surround — purpose, related code, prior decisions — instead of loading the
whole codebase.

Paths below use `.claude` (the agent dir) and `CLAUDE.md` (this entry point) — the
Claude Code defaults; another agent environment uses its configured names (for example
`.agents` / `AGENTS.md`). Slash commands, hooks, and the digest import are Claude Code
mechanisms; the portable substitute is this same loop plus verbal requests and direct
script calls, which work in any environment.

### Memory digest (loaded every session)
@.claude/amg/digest.md

Above is the auto-generated digest of the most salient standing decisions and open
questions, refreshed by consolidation so it rides in every session — surfacing the
memory even before any retrieval (the loop's main failure is memory that exists but is
never consulted). It is empty until the first consolidation.

### Activation gate
At session start, check whether `.claude/amg/config.yml` exists with `active: true`.
- **Not present or `active: false`** → AMG is OFF. Behave as a normal Claude Code
  session. Do not create the graph and do not change behavior.
- **Present and active** → AMG is ON. Follow the operating loop below.

### Operations — manual and by request
Every operation is available three ways: a `/amg <verb>` command, a direct skill, and
plain-language intent. Match meaning and synonyms, not the exact word.

| Operation | `/amg` verb (synonyms) | Skill / script | Verbal intent |
|---|---|---|---|
| Show status | `status` | `lifecycle.py status` | "memory status", "статус памяти" |
| Enable / disable | `on` / `off` (activate, start · stop) | `lifecycle.py on`/`off` | "включи / выключи / запусти AMG" |
| Repair the graph | `repair` (fix, heal) | `lifecycle.py repair` | "почини / проверь граф" |
| Build / sync the graph | `sync` (build, index, reconcile) | **amg-bootstrap** | "проиндексируй / синхронизируй проект" |
| Retrieve context | `retrieve <q>` (recall, context) | **amg-retrieve** | "собери контекст по X" |
| Consolidate memory | `consolidate` (maintain, compact) | **amg-consolidate** | "подведём итоги", "прибери память" |
| Capture a note | — | `notes.py add` | "запомни, что …" |

`on` only sets the `active` flag; **building the graph is `sync`** (the amg-bootstrap
skill) — enabling alone does not build it. Treat synonyms as equivalent: "включи /
активируй / запусти AMG" all mean enable; "построй / обнови / сверь граф" all mean sync.

**`automation` (config.yml, default `true`).** `true`: the SessionStart/SessionEnd hooks
heal the store, fold weights, and refresh the digest automatically, and you run the loop
below. `false`: nothing runs on its own — act only on a `/amg` command, a skill, or an
explicit request.

### Operating loop (when AMG is ON and automation is on)

1. **Heal, then sync the graph with disk.** At session start, first replay any
   unfinished write from a previous run — this is cheap and is how the system
   self-recovers after a crash or an interrupted session:
   ```
   python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
   python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
   ```
   With automation on, the SessionStart hook already ran this heal and refreshed the
   digest; repeat it only if you suspect the hook did not fire — it is idempotent.
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
   `/clear`; the graph is where they survive, so capture before clearing. With
   automation on, the SessionEnd hook already folds weights and refreshes the digest
   deterministically; running the skill adds the consolidator subagent's judgment —
   promote, merge, compact — which a hook cannot do.

### Where things are
- The graph: `.claude/amg/nodes/` — the source of truth, one file per node — plus
  `work/` (scratch), `journal/` (crash-recovery state, empty when idle), `archive/`,
  `log.md` (the human-readable action log), and `digest.md` (the auto-generated
  standing-decisions block imported above).
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
