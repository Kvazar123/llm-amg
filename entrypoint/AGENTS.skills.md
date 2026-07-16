# Project memory

<!-- Skill-AWARE portable AMG activation block for agent environments that support
     SKILL.md skills but are not Claude Code — OpenCode, Qwen Code, and the like. The
     installer writes this for `--env opencode|qwen` and renders the paths to the
     configured agent dir / entry point. Skills carry the orchestration procedures;
     subagent prompt files are rendered where the environment reads them (Qwen Code:
     markdown agents; OpenCode: .opencode/agent) — spawn them when your environment
     supports subagents, else follow them as guidance. Session hooks are written only
     where the environment reads a Claude-shaped hooks block (Qwen Code); the start
     check below is idempotent, so it is safe to run whether or not hooks also ran.
     NOTE: this mode is not yet verified live; stability is not guaranteed. -->

## AMG — Associative Memory Graph

This project uses AMG: a persistent, typed knowledge graph with hierarchical summaries
and spreading-activation retrieval, stored under `.claude/amg/`. It lets you work on one
part of the project in a clean context window while still seeing the strategic surround —
purpose, related code, prior decisions — instead of loading the whole codebase.

Paths below use `.claude` (the agent dir) and `CLAUDE.md` (this entry point) as the Claude
Code defaults; here they are your configured names. Your environment discovers the
**amg-* skills** under `.claude/skills` — use them: they carry the orchestration
procedures (batching, checkpoints, one-call application) that a from-scratch improvisation
loses. The worker prompts installed under `.claude/agents` describe the builder / synth /
linker / consolidator roles: when your environment supports subagents, spawn them by
those definitions; when it does not, follow the prompt files as guidance in your own
context. What no environment outside Claude Code has is the `/amg` slash command and the
digest `@`-import — replaced here by verbal intent and by reading the digest yourself.

### Memory digest (read at session start)
At the start of every session, **read `.claude/amg/digest.md`** — a small auto-generated
block of the most salient standing decisions and open questions, refreshed by
consolidation. Reading it surfaces the memory before any retrieval (the loop's main
failure is memory that exists but is never consulted). It is empty until the first
consolidation.

### Activation gate
Check whether `.claude/amg/config.yml` exists with `active: true`.
- **Not present or `active: false`** → AMG is OFF. Behave normally; do not create the graph.
- **Present and active** → AMG is ON. Follow the operating loop below.

### Common tasks — the quick reference
Match intent and synonyms, not the exact word — in any language. One guard: treat a
request as a memory operation **only when it explicitly refers to this memory** (it
names the memory, the memory graph, or AMG); a generic "show the graph" or "wrap up"
about something else must NOT trigger one. A literal `/amg <verb>` typed by the user is
the same request in command clothes — run the matching row yourself.

| Task | How |
|---|---|
| Memory status (verbatim, incl. engine version) | `python .claude/skills/amg-bootstrap/scripts/lifecycle.py status .` — present its report as printed, do not paraphrase |
| Engine version | `python .claude/skills/amg-bootstrap/scripts/lifecycle.py version` |
| Enable / disable | `python .claude/skills/amg-bootstrap/scripts/lifecycle.py on` / `off .` |
| Repair after a crash | `python .claude/skills/amg-bootstrap/scripts/lifecycle.py repair .` |
| Build / sync the graph | the **amg-bootstrap** skill |
| Context for a task | the **amg-retrieve** skill (direct `retrieve.py` call — step 2 below) |
| Targeted pointer lookup ("where is X") | the same call with `--compact` |
| Capture a note / decision | `python .claude/skills/amg-bootstrap/scripts/notes.py add --type decision --summary "..."` |
| Consolidate memory (wrap-up) | the **amg-consolidate** skill |
| Re-link the isolated nodes (`/amg relink`) | the FULL linking cycle of the **amg-bootstrap** skill, scoped to strays: `python .claude/skills/amg-bootstrap/scripts/link_candidates.py --isolated .` nominates (past rejections re-opened) → judge every `work/link-batch-*.json` per amg-linker → ONE `reconcile.py apply-derived .` → repeat until zero batches. **Nomination alone applies nothing.** Deleting `work/judged/` is the FULL re-open — last resort |
| Open the 3D graph viewer | `python .claude/skills/amg-retrieve/scripts/export_graph.py --store .claude/amg --open` |

### Operating loop (when AMG is ON)

1. **Heal, then a deterministic start check — never guess whether sources changed.**
   At session start run this start check — four cheap, deterministic, model-free
   commands (batch them into as few tool calls as your shell allows; idempotent, so
   it is safe whether or not the environment's session hooks also ran; this is also
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
   wrap-up, step 3) and the volume at which a sync was last deferred. When
   `queued_for_semantic` is above zero, **ask the user** — "sources changed — N
   added / M changed; sync the semantic layer now (~K units) or defer?" — unless a
   recorded deferral stands and the backlog has not grown noticeably since (about
   ×1.5, or +50 units): then say one quiet line at most. On a defer, record it:
   ```
   python .claude/skills/amg-bootstrap/scripts/lifecycle.py sync-defer .
   ```
   On yes, run the **amg-bootstrap** skill for the semantic half. Zero diff and zero
   remainder — say nothing and start working. Never start the model half silently:
   the deterministic half is automatic by design, the semantic half costs money and
   is the user's call. Building from empty and reconciling are the same operation —
   crash-safe and idempotent.

2. **The graph is the primary context source — retrieve before you work.** Everything
   the graph can give, take from the graph FIRST; only then decide what is left to look
   up in the files. For each task, retrieve **directly** (the amg-retrieve skill's
   default path) — one cheap call whose printed pack becomes your working context:
   ```
   python .claude/skills/amg-retrieve/scripts/retrieve.py "<query>" --store .claude/amg
   ```
   (add `--intent history|conflict` for a history / contradictions question — you
   classify that from meaning, in any language; add `--compact` for a targeted
   pointer lookup — pointer lines instead of unfolded bodies at a fraction of the
   size; entering an unfamiliar topic keeps the full profile). **Read the pack in
   full** — printed, or from `.claude/amg/cache/pack.md`: it is already the
   selection, assembled under a token budget; skimming its head loses exactly the
   tiers the budget paid for. Spawn the **amg-retriever** subagent instead only when
   the pack should NOT enter your window — a summary question to the memory, or an
   already-crowded context. No prompt-time mechanism nudges you mid-session here —
   this retrieval discipline rests on you alone. The protocol: **decompose a complex
   prompt** (a separate retrieval per distinct topic, run as you turn to that part —
   the retrieved branches together form the task's context); **a focus shift = a new
   retrieval**; **graph before filesystem search** (open sources point-wise from the
   pack's `path:line` pointers; search the files directly only for what the pack did
   not cover, and say so in one line — and when you do sweep, **exclude the agent
   directory** `.claude/`: the memory store is not project sources and only floods
   the results); **make it visible** (tell the user in one line that context came
   from memory). One exception: when the prompt itself hands you the exact entry
   points — a file and line, a ready-made fix, a fully specified local change — a
   ritual pack adds nothing; the graph is for entering the unfamiliar. Skip the
   retrieval and start; the protocol resumes the moment the task reaches beyond what
   the prompt gave. For code the pack gives `path:line` pointers — edit the real
   file; the graph is not a copy of the code. The pack is memory, not ground truth:
   before stating a code fact from it — especially a node flagged `⟨stale /
   unverified / contradicted / low confidence⟩` — confirm it against the live source
   with
   `python .claude/skills/amg-retrieve/scripts/verify_claims.py <id> --store .claude/amg`
   (read-only; on any conflict the current source wins).

3. **Capture as you go; consolidate at the end.** Capture decisions, conclusions, open
   questions, and forward-looking plans with the safe note API — do NOT hand-edit `nodes/`:
   ```
   python .claude/skills/amg-bootstrap/scripts/notes.py add --type decision --summary "..." [--body "..."] [--tags "a,b"]
   ```
   (types: `note` / `decision` / `adr` / `open_question` / `plan`; cheap, transactional).
   Capture conclusions about the PROJECT only: an observation about the AMG engine
   itself (a suspected bug, a limitation) goes to the user in chat, never into the
   project's memory — it would pollute the digest.
   **The session's end is invisible to you until the user signals it** — so the
   observable trigger is the user's wrap-up signal: when they say they are
   finishing, wrapping up, done for today (any language — match the meaning), first
   **capture the session's conclusions as notes** — unless your environment's hooks
   dump the transcript, those notes are the only record of this dialogue — then run
   the **amg-consolidate** skill (it spawns amg-consolidator) to fold weights, file
   the conclusions, and compact over-budget branches; offer the same when the
   session was dense with decisions or the start check's `note:` said the judgment
   pass is overdue. Conclusions that live only in chat are lost when context clears.

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
  subagent definitions, or scripts (`.claude/skills/amg-*`, the installed agent files)
  as part of task work. If one looks buggy or limiting, report it to the user instead of
  patching it live: the installed copy must stay identical to the canonical, tested
  version, and editing the engine you are *currently running* is how a working memory
  gets silently corrupted.
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
