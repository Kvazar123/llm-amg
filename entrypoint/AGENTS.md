# Project memory

<!-- Universal (skill-less) AMG activation block for ANY agent environment that reads an
     instruction file (AGENTS.md) and can run shell/Python but lacks Claude Code's specific
     skills / subagents / slash commands / hooks — e.g. OpenAI Codex, Qwen Coder, and other
     AGENTS.md-based agents. Here the model itself drives the loop with direct script calls.
     Loaded at the start of every session. The installer writes this variant for `--env`
     other than claude-code; the paths below are rendered to the configured agent dir /
     entry point. NOTE: this portable mode is experimental — not yet verified on
     non-Claude-Code environments, so stability is not guaranteed. -->

## AMG — Associative Memory Graph

This project uses AMG: a persistent, typed knowledge graph with hierarchical summaries
and spreading-activation retrieval, stored under `.claude/amg/`. It lets you work on one
part of the project in a clean context window while still seeing the strategic surround —
purpose, related code, prior decisions — instead of loading the whole codebase.

Paths below use `.claude` (the agent dir) and `CLAUDE.md` (this entry point) as the Claude
Code defaults; this environment uses its configured names (for example `.agents` /
`AGENTS.md`). This is the **portable block**: there are no skills, subagents, slash
commands, or hooks here — you, the model, run the plain Python scripts directly and do the
semantic steps yourself, following the agent prompts as guidance (not as spawned agents).

### Memory digest
At the start of every session, **read `.claude/amg/digest.md`** — a small auto-generated
block of the most salient standing decisions and open questions, refreshed by
consolidation. Reading it surfaces the memory before any retrieval (the loop's main
failure is memory that exists but is never consulted). It is empty until the first
consolidation.

### Activation gate
Check whether `.claude/amg/config.yml` exists with `active: true`.
- **Not present or `active: false`** → AMG is OFF. Behave as a normal session; do not
  create the graph or change behavior.
- **Present and active** → AMG is ON. Follow the operating loop below.

### Operating loop (when AMG is ON)
There are no hooks here, so **you** run each step at the right moment.

1. **Heal, then sync the graph with disk.** At session start, replay any unfinished write
   and check the store (cheap; this is how a crash or interrupted session self-recovers):
   ```
   python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
   python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
   ```
   Then, if the source folders (`mirror_path` / `absorb_path`) may have changed since last
   session — or this is the first session — build/sync the graph:
   ```
   python .claude/skills/amg-bootstrap/scripts/reconcile.py bootstrap .
   ```
   If it reports `queued_for_semantic > 0`, do the semantic enrichment **yourself**: read
   `.claude/amg/work/queue.json`, and for each unit write a 1–3 phrase summary plus the
   meaningful edges, following the guidance in `.claude/agents/amg-builder.md` (per-unit
   summaries + edges) and `.claude/agents/amg-synth.md` (an overview, hubs, cross-domain
   `documents` edges, and a gap report). Write the result to
   `.claude/amg/work/derived-<batch>.json` (the derivation format) and apply it:
   ```
   python .claude/skills/amg-bootstrap/scripts/reconcile.py apply .claude/amg/work/derived-<batch>.json .
   ```
   Building from empty and reconciling are the same operation — crash-safe and idempotent,
   so re-running never duplicates or loses anything.

2. **Retrieve before you work.** For each task, FIRST assemble a context pack from the
   graph (seed → spreading activation → a budgeted pack), then work from it — do not dump
   the whole codebase into context:
   ```
   python .claude/skills/amg-retrieve/scripts/retrieve.py "<query>" --store .claude/amg
   ```
   Read the written pack at `.claude/amg/cache/pack.md`. Re-run when the focus shifts (a
   new requirement, a different subsystem). For code the pack gives `path:line` pointers —
   edit the real file; the graph is not a copy of the code.

3. **Capture as you go; consolidate at the end.** When a decision, conclusion, open
   question, or forward-looking plan emerges, capture it with the safe note API — do NOT
   hand-edit files under `nodes/`:
   ```
   python .claude/skills/amg-bootstrap/scripts/notes.py add --type decision --summary "..." [--body "..."] [--tags "a,b"]
   ```
   (types: `note` / `decision` / `adr` / `open_question` / `plan`; cheap, transactional, no
   bootstrap needed). At session end, or before clearing context, maintain memory:
   ```
   python .claude/skills/amg-consolidate/scripts/consolidate.py weights .
   python .claude/skills/amg-consolidate/scripts/consolidate.py plan .
   ```
   Then decide what to promote / merge / summarize / compact, following
   `.claude/agents/amg-consolidator.md`; write `.claude/amg/work/actions.json` and apply it:
   ```
   python .claude/skills/amg-consolidate/scripts/consolidate.py apply .claude/amg/work/actions.json .
   ```
   Conclusions that live only in chat are lost when the context clears; the graph is where
   they survive, so capture before clearing.

### Operations on request
The same operations also run on a plain-language request — match intent and synonyms, not
the exact word: "index / sync the project" → step 1; "gather context on X" → step 2; "wrap
up / tidy memory" → step 3; "memory status" → run `graph_store.py verify` and read the
node / queue counts; "enable / disable AMG" → flip `active:` in `.claude/amg/config.yml`.
(Slash commands like `/amg`, the auto `SessionStart`/`SessionEnd` hooks, and the digest
`@`-import are Claude Code conveniences and are absent here — this loop is the portable
equivalent, and it works the same.)

### Where things are
- The graph: `.claude/amg/nodes/` — the source of truth, one file per node — plus `work/`
  (scratch), `journal/` (crash-recovery state, empty when idle), `archive/`, `sessions/`,
  `log.md` (the action log), and `digest.md` (read above).
- Activation / sources / tunables: `.claude/amg/config.yml`.
- Your source folders (whatever `mirror_path` / `absorb_path` point to) are **read-only**
  for memory upkeep — never modify them as a side effect of maintaining the graph.
- AMG's own scripts and agent prompts (`.claude/skills/amg-*`, `.claude/agents/amg-*`) are
  infrastructure. Do **not** edit them as part of task work; report a bug to the user
  instead, so the canonical, tested version stays authoritative.

### If anything looks inconsistent
Run, from the project root:
```
python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
python .claude/skills/amg-bootstrap/scripts/reconcile.py bootstrap .
```
This replays any unfinished write, clears a stale lock, and re-establishes equality with
the code/docs on disk.
