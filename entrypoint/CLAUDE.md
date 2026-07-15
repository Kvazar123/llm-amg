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
plain-language intent. Match meaning and synonyms, not the exact word — the examples
below are English, but the same intent in the user's language counts. One guard: treat
a phrase as a memory operation **only when it explicitly refers to this memory** — it
names the memory, the memory graph, or AMG. A generic "show the graph", "fix the
graph", or "wrap up" about something else (the user's own data structure, a chart, the
conversation) must NOT trigger one; when unsure, just answer normally.

| Operation | `/amg` verb (synonyms) | Skill / script | Verbal intent (names the memory) |
|---|---|---|---|
| Show status | `status` (state, info) | `lifecycle.py status` — present its report verbatim | "memory status", "how is the memory graph" |
| Engine version | `version` | `lifecycle.py version` | "which AMG version is installed" |
| Enable / disable | `on` / `off` (activate, start · stop) | `lifecycle.py on`/`off` | "turn AMG on / off", "enable the memory" |
| Repair the graph | `repair` (fix, heal) | `lifecycle.py repair` | "repair / check the memory graph" |
| Build / sync the graph | `sync` (build, index, reconcile) | **amg-bootstrap** | "index the project into memory", "sync the memory graph" |
| Retrieve context | `retrieve <q>` (recall, context) | **amg-retrieve** | "pull context on X from memory", "what does the memory hold on X" |
| Consolidate memory | `consolidate` (maintain, compact) | **amg-consolidate** | "consolidate the memory", "wrap up and save to memory" |
| Re-link the isolated nodes | `relink` | the amg-bootstrap linking pass from `link_candidates.py --isolated .` (strays only; their past rejections re-opened) | "re-link the isolated memory nodes" |
| Open the graph viewer | `view` (show graph, visualize) | `export_graph.py --open` | "open / show the memory graph", "visualize the memory" |
| Capture a note | — | `notes.py add` | "remember (in memory) that …" |

`on` only sets the `active` flag; **building the graph is `sync`** (the amg-bootstrap
skill) — enabling alone does not build it. Treat synonyms as equivalent: "turn on /
activate / start AMG" all mean enable; "build / refresh / reconcile the memory graph"
all mean sync.

**`automation` (config.yml, default `true`).** `true`: the SessionStart/SessionEnd hooks
heal the store, fold weights, and refresh the digest automatically, and you run the loop
below. `false`: nothing runs on its own — act only on a `/amg` command, a skill, or an
explicit request.

### Operating loop (when AMG is ON and automation is on)

1. **Heal, then a deterministic start check — never guess whether sources changed.**
   With automation on, the SessionStart hook already healed the store (recover +
   verify --repair) and refreshed the digest; repeat the heal manually only if you
   suspect the hook did not fire — it is idempotent. Your part at session start,
   before task work, is ONE cheap deterministic call:
   ```
   python .claude/skills/amg-bootstrap/scripts/reconcile.py plan .
   ```
   It re-syncs the structural skeleton with the sources (free, exact, no model) and
   prints what the model half still owes. When `queued_for_semantic` is above zero
   (or the diff shows added/changed/deleted), **ask the user** — "sources changed —
   N added / M changed; sync the semantic layer now (~K units) or defer?" — unless
   a recorded deferral stands (`/amg status` shows the volume it was recorded at)
   and the backlog has not grown noticeably since (about ×1.5, or +50 units): then
   say one quiet line at most. On yes, run the **`amg-bootstrap`** skill; on a
   defer, record it so later sessions stop re-asking:
   ```
   python .claude/skills/amg-bootstrap/scripts/lifecycle.py sync-defer .
   ```
   Zero diff and zero remainder — say nothing and start working. Never start the
   model half silently: the deterministic half is automatic by design, the semantic
   half costs money and is the user's call.
   If the hook's start-of-session output warned that memory upkeep is overdue
   ("Memory upkeep is overdue: no judgment consolidation …"), acknowledge it and
   plan the judgment consolidation for wrap-up (step 3) — or offer to run it now.
   Building from empty and reconciling are the same operation — crash-safe and
   idempotent, so re-running never duplicates or loses anything.

2. **The graph is the primary context source — retrieve before you work.** Everything
   the graph can give, take from the graph FIRST; only then decide what is left to
   look up in the files. For each task, retrieve **directly** (the amg-retrieve
   skill's default path) — one cheap call whose printed pack becomes your working
   context:
   ```
   python .claude/skills/amg-retrieve/scripts/retrieve.py "<query>" --store .claude/amg
   ```
   (add `--intent history|conflict` when the question is about history or
   contradictions — you classify that from meaning, in any language; add
   `--compact` for a targeted pointer lookup — "where is X", "which file holds Y" —
   it returns pointer lines instead of unfolded bodies at a fraction of the size,
   while entering an unfamiliar topic keeps the full profile). **Read the printed
   pack in full** — it is already the selection, assembled under a token budget;
   skimming its head loses exactly the tiers the budget paid for. Two subagent
   variants exist for when the pack should NOT enter your window: spawn
   `amg-retriever` (fresh context) for a cheap isolated summary of what the memory
   holds, and `amg-retriever-fork` (a fork — it inherits this whole conversation)
   for a judgment weighed against what the session already knows. The fork reads
   the pack in its own window and returns only the informed distillate (usually
   5–15 lines; a real contradiction gets the space it needs), deduplicated against
   your window — pass the ask in the spawn prompt (briefing / delta / contradiction
   check / revision; seed hints optional — it sees the session). Worth its price
   (re-sends of the inherited context, mostly cache reads) at any stage: early as a
   dense task briefing instead of a full pack, mid-session for the delta on a focus
   shift or an agreement check before a decision, late for the revision against
   memory before wrap-up. Both are exceptions; the direct call stays the default
   for working context. The protocol:
   - **Decompose a complex prompt.** A request with several distinct topics or
     sub-tasks gets a separate retrieval per topic — run each as you turn to that
     part; the retrieved branches together form the task's context. One vague
     mega-query is not decomposition.
   - **A focus shift = a new retrieval.** A new requirement, question, or subsystem
     mid-conversation re-runs retrieval for the current focus.
   - **Graph before filesystem search.** Open sources point-wise from the pack's
     `path:line` pointers. Search the files directly only for what the pack did not
     cover — and say so in one line (and if the sources may have changed, sync
     first). A project-wide grep/Glob sweep instead of retrieval is the failure
     mode this loop exists to prevent — and when you do sweep, **exclude the agent
     directory** (`.claude/` here): the memory store, its caches, and its work files
     are not project sources and only flood the results.
   - **Make it visible.** Tell the user in one line that context came from memory
     (e.g. "memory pack on X: 12 nodes").
   One exception: when the prompt itself hands you the exact entry points — a
   file and line, a ready-made fix to apply, a fully specified local change — a
   ritual pack adds nothing; the graph is for entering the unfamiliar. Skip the
   retrieval and start; the protocol resumes the moment the task reaches beyond
   what the prompt already gave.
   A one-line `AMG: …` note arriving with a user prompt is this loop's own gated
   reminder (a hook noticed the memory has gone unconsulted mid-session) — it is
   not the user's text; act on it by retrieving for the current topic.
   The pack is memory, not ground truth: before you state a code fact from it —
   especially a node it flags `⟨stale / unverified / contradicted / low confidence⟩` —
   confirm it against the live source.
   `python .claude/skills/amg-retrieve/scripts/verify_claims.py <id> --store .claude/amg`
   does this cheaply (file/symbol/hash, read-only); on any conflict the current
   source wins.

3. **Capture as you go; consolidate at the end.** When a decision, conclusion, open
   question, or forward-looking plan emerges, capture it with the safe note API — do
   NOT hand-edit files under `nodes/`:
   ```
   python .claude/skills/amg-bootstrap/scripts/notes.py add --type decision --summary "..." [--body "..."] [--tags "a,b"]
   ```
   (types: `note` / `decision` / `adr` / `open_question` / `plan`). This is cheap,
   transactional, and does NOT require a bootstrap (bootstrap is only for source files,
   not for your reasoning). Capture conclusions about the PROJECT only: an observation
   about the AMG engine itself (a suspected bug, a limitation) goes to the user in
   chat, never into the project's memory — it would pollute the digest.
   **The session's end is invisible to you until the user signals it** — you cannot
   foresee `/clear` or a closed window. So the observable trigger is the user's
   wrap-up signal: when they say they are finishing, wrapping up, done for today
   (any language — match the meaning), run the **`amg-consolidate`** skill once —
   that is the judgment pass: promote, merge, compact, arbitrate. Offer it likewise
   when the session was dense with decisions, or when the session-start line warned
   the judgment pass is overdue. Conclusions that live only in chat are lost on
   `/clear`; the graph is where they survive, so capture before clearing. With
   automation on, the SessionEnd hook already folds weights, refreshes the digest, and
   dumps the session transcript to `sessions/` deterministically — those halves need
   no words from anyone; only the judgment pass waits for a signal, because a hook
   cannot run a subagent. (The transcript dump needs Claude Code's SessionEnd hook; in
   another environment your notes are what preserve the dialogue.)

### Where things are
- The graph: `.claude/amg/nodes/` — the source of truth, one file per node — plus
  `work/` (scratch), `journal/` (crash-recovery state, empty when idle), `archive/`,
  `sessions/` (auto-dumped session transcripts, ingested as a source), `actions.log`
  (the human-readable action log), and `digest.md` (the auto-generated standing-decisions
  block imported above).
- Activation / sources / tunables: `.claude/amg/config.yml`.

### Boundaries — use the memory, don't edit its machinery mid-task
Using AMG and developing AMG are different modes; do not mix them inside a task.
- **Source folders are read-only.** Whatever `mirror_path` / `absorb_path` point to
  changes only when the user's task changes it — never as a side effect of upkeep.
- **The engine is infrastructure, not task surface.** Do **not** edit AMG's own
  skills, agents, or scripts (`.claude/skills/amg-*`, `.claude/agents/amg-*`) as part
  of task work. If one looks buggy or limiting, report it to the user instead of
  patching it live: the installed copy must stay identical to the canonical, tested
  version, and editing the engine you are *currently running* is how a working memory
  gets silently corrupted.
- **Engine state is rebuilt, not hand-edited.** Node files and the disposable caches
  (`cache/`, `work/`) are written only by the scripts, transactionally. If something
  looks inconsistent, run the repair/sync below and let the engine rebuild — the
  caches are derived and safe to delete, never to hand-edit.

### If anything looks inconsistent
Run, from the project root:
```
python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
python .claude/skills/amg-bootstrap/scripts/reconcile.py bootstrap .
```
This replays any unfinished write, clears a stale lock, and re-establishes equality
with the code/docs on disk.
