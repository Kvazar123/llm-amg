# Project memory

<!-- Skill-AWARE AMG activation block for OpenAI Codex. Codex HAS skills (.agents/skills),
     subagents (TOML in .codex/agents), and reads AGENTS.md — but lacks Claude Code's
     SessionStart/SessionEnd hooks, the /amg slash command, and the CLAUDE.md @import. So
     the model drives the loop via skills + subagents, and lifecycle (heal at start,
     consolidate at end) is loop discipline, not hooks. The installer writes this for
     `--env codex` and renders the paths to the configured agent dir. NOTE: this mode is
     not yet verified on a live Codex; stability is not guaranteed. -->

## AMG — Associative Memory Graph

This project uses AMG: a persistent, typed knowledge graph with hierarchical summaries
and spreading-activation retrieval, stored under `.claude/amg/`. It lets you work on one
part of the project in a clean context window while still seeing the strategic surround —
purpose, related code, prior decisions — instead of loading the whole codebase.

Paths below use `.claude` (the agent dir) and `CLAUDE.md` (this entry point) as the Claude
Code defaults; here they are your configured names — skills live under `.claude/skills`,
the graph under `.claude/amg`, and the **subagents are TOML files under `.codex/agents`**.
Codex HAS skills and subagents, so use them; what it lacks versus Claude Code is the auto
hooks, the `/amg` command, and the digest `@`-import — replaced here by loop discipline and
reading the digest yourself.

### Memory digest (read at session start)
At the start of every session, **read `.claude/amg/digest.md`** — a small auto-generated
block of the most salient standing decisions and open questions, refreshed by
consolidation. Reading it surfaces the memory before any retrieval (the loop's main failure
is memory that exists but is never consulted). It is empty until the first consolidation.

### Activation gate
Check whether `.claude/amg/config.yml` exists with `active: true`.
- **Not present or `active: false`** → AMG is OFF. Behave normally; do not create the graph.
- **Present and active** → AMG is ON. Follow the operating loop below.

### Operations — skills + subagents, by intent
Trigger by meaning and synonyms, not an exact command — in any language. One guard:
treat a request as a memory operation **only when it explicitly refers to this memory**
(it names the memory, the memory graph, or AMG); a generic "show the graph" or "wrap up"
about something else must NOT trigger one.

| Operation | Skill | Subagents (TOML in `.codex/agents`) |
|---|---|---|
| Build / sync the graph | **amg-bootstrap** | amg-classifier, amg-builder, amg-synth, amg-linker |
| Retrieve context | **amg-retrieve** | amg-retriever |
| Consolidate memory | **amg-consolidate** | amg-consolidator |
| Capture a note | `notes.py add` (direct) | — |
| Repair the graph | `graph_store.py recover` + `verify --repair` (direct) | — |
| Visualize the graph | `export_graph.py --store .claude/amg --open` (direct, read-only) | — |

### Operating loop (when AMG is ON)
There are no SessionStart/SessionEnd hooks here, so **you** run each step at the right moment.

1. **Heal, then sync.** At session start, replay any unfinished write and check the store
   (this is how a crash or interrupted session self-recovers):
   ```
   python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
   python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
   ```
   Then, if the source folders (`mirror_path` / `absorb_path`) may have changed — or it is
   the first session — run the **amg-bootstrap** skill to build/sync the graph; it spawns
   the builder/synth subagents for the semantic half. Building from empty and reconciling
   are the same operation — crash-safe and idempotent.

2. **The graph is the primary context source — retrieve before you work.** Everything
   the graph can give, take from the graph FIRST; only then decide what is left to look
   up in the files. For each task, assemble a context pack via the **amg-retrieve**
   skill (it spawns amg-retriever), then read `.claude/amg/cache/pack.md` and work from
   it. The protocol: **decompose a complex prompt** (a separate retrieval per distinct
   topic, run as you turn to that part — the retrieved branches together form the
   task's context); **a focus shift = a new retrieval**; **graph before filesystem
   search** (open sources point-wise from the pack's `path:line` pointers; search the
   files directly only for what the pack did not cover, and say so in one line);
   **make it visible** (tell the user in one line that context came from memory).
   One exception: when the prompt itself hands you the exact entry points — a file
   and line, a ready-made fix, a fully specified local change — a ritual pack adds
   nothing; the graph is for entering the unfamiliar. Skip the retrieval and start;
   the protocol resumes the moment the task reaches beyond what the prompt gave. For
   code the pack gives `path:line` pointers — edit the real file; the graph is not a
   copy of the code.

3. **Capture as you go; consolidate at the end.** Capture decisions, conclusions, open
   questions, and forward-looking plans with the safe note API — do NOT hand-edit `nodes/`:
   ```
   python .claude/skills/amg-bootstrap/scripts/notes.py add --type decision --summary "..." [--body "..."] [--tags "a,b"]
   ```
   (types: `note` / `decision` / `adr` / `open_question` / `plan`; cheap, transactional).
   Capture conclusions about the PROJECT only: an observation about the AMG engine
   itself (a suspected bug, a limitation) goes to the user in chat, never into the
   project's memory — it would pollute the digest.
   At session end, or before clearing context, run the **amg-consolidate** skill (it spawns
   amg-consolidator) to fold weights, file the session's conclusions, and compact
   over-budget branches. Conclusions that live only in chat are lost when context clears.

### Where things are
- The graph: `.claude/amg/nodes/` — the source of truth, one file per node — plus `work/`
  (scratch), `journal/` (crash-recovery state), `archive/`, `sessions/`, `actions.log`
  (the action log), and `digest.md` (read above).
- Activation / sources / tunables: `.claude/amg/config.yml`.

### Boundaries — use the memory, don't edit its machinery mid-task
Using AMG and developing AMG are different modes; do not mix them inside a task.
- **Source folders are read-only.** Whatever `mirror_path` / `absorb_path` point to
  changes only when the user's task changes it — never as a side effect of upkeep.
- **The engine is infrastructure, not task surface.** Do **not** edit AMG's own skills,
  subagents, or scripts (`.claude/skills/amg-*`, the TOML in `.codex/agents`) as part of
  task work. If one looks buggy or limiting, report it to the user instead of patching it
  live: the installed copy must stay identical to the canonical, tested version, and
  editing the engine you are *currently running* is how a working memory gets silently
  corrupted.
- **Engine state is rebuilt, not hand-edited.** Node files and the disposable caches
  (`cache/`, `work/`) are written only by the scripts, transactionally. If something looks
  inconsistent, run the repair/sync above and let the engine rebuild — the caches are
  derived and safe to delete, never to hand-edit.

### If anything looks inconsistent
Run, from the project root:
```
python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
python .claude/skills/amg-bootstrap/scripts/reconcile.py bootstrap .
```
This replays any unfinished write, clears a stale lock, and re-establishes equality with
the code/docs on disk.
