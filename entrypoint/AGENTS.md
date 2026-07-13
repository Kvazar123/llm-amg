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

1. **Heal, then a deterministic start check — never guess whether sources changed.**
   At session start run this start check — four cheap, deterministic, model-free
   commands (batch them into as few tool calls as your shell allows; this is also
   how a crash or interrupted session self-recovers):
   ```
   python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
   python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
   python .claude/skills/amg-bootstrap/scripts/reconcile.py plan .
   python .claude/skills/amg-bootstrap/scripts/lifecycle.py status .
   ```
   `plan` re-syncs the structural skeleton with the sources (free, exact) and prints
   what the model half still owes; `status` reports, among the rest, whether the
   judgment consolidation is overdue (a `note:` line — react by offering it at
   wrap-up, step 3). When `queued_for_semantic` is above zero (or the diff shows
   added/changed/deleted), **ask the user**: "sources changed — N added / M changed;
   sync the semantic layer now (~K units) or defer?"; on a defer, say one line ("the
   graph lags the sources; sync when ready") and honestly ask again next session.
   Zero diff and zero remainder — say nothing and start working. Never start the
   model half silently: the deterministic half is automatic by design, the semantic
   half costs money and is the user's call.
   On yes, do the semantic enrichment **yourself**: read
   `.claude/amg/work/queue.json` (each unit carries its own `text` — summarize from it,
   do not re-open sources), and for each unit write a 1–3 phrase summary plus the
   meaningful edges, following the guidance in `.claude/agents/amg-builder.md` (per-unit
   summaries + edges) and `.claude/agents/amg-synth.md` (an overview, hubs, cross-domain
   `documents` edges, and a gap report). Write the results to
   `.claude/amg/work/derived-<batch>.json` files (the derivation format) and apply them
   all with ONE call (it consumes every `work/derived-*.json` and moves the applied
   files to `work/applied/` — re-running it is the resume path):
   ```
   python .claude/skills/amg-bootstrap/scripts/reconcile.py apply-derived .
   ```
   Then complete the cross-domain links (doc <-> code <-> example) with the global
   linking pass — per-batch summarizing cannot see across batches:
   ```
   python .claude/skills/amg-bootstrap/scripts/link_candidates.py .
   ```
   Judge each `work/link-batch-*.json` yourself following `.claude/agents/amg-linker.md`
   (confirm only real relations; similarity merely nominates), write the update items to
   `.claude/amg/work/derived-links-<n>.json`, apply them the same way, and check the
   result — `python .claude/skills/amg-bootstrap/scripts/reconcile.py metrics .` should
   report one dominant component (`gate: ok`). Building from empty and reconciling are
   the same operation — crash-safe and idempotent, so re-running never duplicates or
   loses anything.

2. **The graph is the primary context source — retrieve before you work.** Everything
   the graph can give, take from the graph FIRST; only then decide what is left to look
   up in the files. For each task, assemble a context pack (seed → spreading activation
   → a budgeted pack) and work from it:
   ```
   python .claude/skills/amg-retrieve/scripts/retrieve.py "<query>" --store .claude/amg
   ```
   Read the written pack at `.claude/amg/cache/pack.md`. The protocol:
   - **Decompose a complex prompt**: a request with several distinct topics gets a
     separate retrieval per topic, run as you turn to that part — the retrieved
     branches together form the task's context.
   - **A focus shift = a new retrieval** (a new requirement, question, or subsystem).
   - **Graph before filesystem search**: open sources point-wise from the pack's
     `path:line` pointers; search the files directly only for what the pack did not
     cover, and say so in one line. A project-wide grep sweep instead of retrieval is
     the failure mode this loop exists to prevent — and when you do sweep, **exclude
     the agent directory** (`.claude/` here): the memory store, its caches, and its
     work files are not project sources and only flood the results.
   - **Make it visible**: tell the user in one line that context came from memory.
   One exception: when the prompt itself hands you the exact entry points — a file
   and line, a ready-made fix to apply, a fully specified local change — a ritual
   pack adds nothing; the graph is for entering the unfamiliar. Skip the retrieval
   and start; the protocol resumes the moment the task reaches beyond what the
   prompt already gave.
   For code the pack gives `path:line` pointers — edit the real file; the graph is not
   a copy of the code.

3. **Capture as you go; consolidate at the end.** When a decision, conclusion, open
   question, or forward-looking plan emerges, capture it with the safe note API — do NOT
   hand-edit files under `nodes/`:
   ```
   python .claude/skills/amg-bootstrap/scripts/notes.py add --type decision --summary "..." [--body "..."] [--tags "a,b"]
   ```
   (types: `note` / `decision` / `adr` / `open_question` / `plan`; cheap, transactional, no
   bootstrap needed). Capture conclusions about the PROJECT only: an observation about
   the AMG engine itself (a suspected bug, a limitation) goes to the user in chat,
   never into the project's memory — it would pollute the digest.
   **The session's end is invisible to you until the user signals it** — so the
   observable trigger is the user's wrap-up signal: when they say they are
   finishing, wrapping up, done for today (any language — match the meaning),
   maintain memory; offer the same when the session was dense with decisions or the
   start check's `note:` said the judgment pass is overdue. There are no hooks
   here, so run BOTH halves yourself — the deterministic fold and the judgment pass:
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
the exact word, in any language. One guard: treat a phrase as a memory operation **only
when it explicitly refers to this memory** (it names the memory, the memory graph, or
AMG); a generic "show the graph" or "wrap up" about something else must NOT trigger one.
Examples: "index the project into memory / sync the memory graph" → step 1; "gather
context on X from memory" → step 2; "consolidate / tidy the memory" → step 3; "memory
status" → `python .claude/skills/amg-bootstrap/scripts/lifecycle.py status .` (one
screen, including the connectivity verdict); "enable / disable AMG" →
`python .claude/skills/amg-bootstrap/scripts/lifecycle.py on` / `off .`.
"open / show / visualize the memory graph" → render the read-only, offline 3D viewer and
open it:
```
python .claude/skills/amg-retrieve/scripts/export_graph.py --store .claude/amg --open
```
(writes only `.claude/amg/cache/graph.html`; `--json` writes the raw `{nodes, links, meta}`
for external tooling instead). (Slash commands like `/amg`, the auto `SessionStart`/`SessionEnd` hooks, and the digest
`@`-import are Claude Code conveniences and are absent here — this loop is the portable
equivalent, and it works the same.)

### Where things are
- The graph: `.claude/amg/nodes/` — the source of truth, one file per node — plus `work/`
  (scratch), `journal/` (crash-recovery state, empty when idle), `archive/`, `sessions/`,
  `actions.log` (the action log), and `digest.md` (read above).
- Activation / sources / tunables: `.claude/amg/config.yml`.

### Boundaries — use the memory, don't edit its machinery mid-task
Using AMG and developing AMG are different modes; do not mix them inside a task.
- **Source folders are read-only.** Whatever `mirror_path` / `absorb_path` point to
  changes only when the user's task changes it — never as a side effect of upkeep.
- **The engine is infrastructure, not task surface.** Do **not** edit AMG's own scripts
  or agent prompts (`.claude/skills/amg-*`, `.claude/agents/amg-*`) as part of task work.
  If one looks buggy or limiting, report it to the user instead of patching it live: the
  installed copy must stay identical to the canonical, tested version, and editing the
  engine you are *currently running* is how a working memory gets silently corrupted.
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
