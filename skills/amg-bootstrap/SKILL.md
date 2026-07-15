---
name: amg-bootstrap
description: >-
  Build or update the AMG knowledge graph from the project's source folders (any
  mix of code, docs, data, or plain text). USE THIS whenever AMG is active
  (`.claude/amg/config.yml` has active: true) and the graph must be brought in line
  with the files on disk: the first time in a project (build from scratch), at the
  start of a session if sources may have changed, or whenever files were added,
  edited, or deleted. Building from empty and reconciling an existing graph are the
  SAME operation — always run this rather than hand-editing the graph. It is
  crash-safe and idempotent: running it again never duplicates or loses anything.
  Triggers: "bootstrap the memory graph", "index this project into memory", "the
  memory graph is stale", "I added/changed/deleted files", "reconcile AMG", "build
  memory for this project".
---

# AMG Bootstrap / Reconcile

This skill makes the graph under `.claude/amg/` match the current files in the
configured source folders. Greenfield (empty graph) and brownfield (existing files,
or partial docs that don't reflect the code) are identical: bootstrap is just
**reconcile from empty**.

Read `references/consistency-model.md` before relying on the guarantees — it
defines why every step here is crash-safe and idempotent. The heavy lifting is in
`scripts/`; do not hand-edit graph files.

## When to run
- AMG is active and this is the first session in the project → builds the graph.
- Start of a session when any source folder may have changed since last time.
- After the user adds, edits, or deletes files.
- After any crash or interrupted run (it self-heals).

## Input is unified and type-agnostic
You do **not** configure "code" vs "docs". `config.yml` lists folders under
`mirror_path` / `absorb_path` (each a string or a list); a folder may hold any mix
of files. `extract_structure.py` detects each file's TYPE automatically and routes
it to the right chunker:
- **code** — Python via the stdlib `ast` (functions/classes + imports + `calls`);
  other languages via tree-sitter **if installed**, else one unit per file.
- **doc** — markdown/rst by heading section; plain text by paragraph. PDF by page
  and DOCX by heading section **if** `pypdf` / `python-docx` are installed.
- **data** — json/yaml by top-level record; XLSX by sheet (a structural description
  of each sheet, not a cell dump) **if** `openpyxl` is installed.
Binary document formats degrade gracefully: with the library absent, those files are
skipped (never a crash). `--stats` reports which extractors are available.
Caches, dependencies, build output, and other binaries are ignored by built-in
defaults plus the repo's `.gitignore`. The graph engine itself is domain-blind; only
this ingest step is type-aware.

## Principle: split mechanical from semantic
- **Mechanical** (no model): a script extracts the exact structural skeleton and
  content hashes. This is cheap, reproducible, and decides *what changed*.
- **Semantic** (model): subagents read only the *changed* units and write summaries
  and meaning-bearing edges. Spend the strongest model on synthesis, a mid model on
  bulk summaries, no model on extraction. Tiers are declared in `config.yml`
  under `models`.

## Thin orchestration (token hygiene)
Everything in YOUR context is re-sent on every turn — that resend, not the
subagents' work, is the dominant token sink of a build. Keep the orchestration
layer thin:
- never paste `queue.json`, `derived-*.json`, node files, or raw directory listings
  into the conversation — pass subagents **file paths**, they read in their own
  isolated context;
- inspect through aggregates only: `inspect_queue.py` for the queue's shape and the
  progress percentage, the one-line counts that `bootstrap`/`apply` print;
- keep per-batch results to the subagent's single status line; do not echo item
  contents back;
- the queue already carries each unit's text, so neither you nor the builders
  re-read source files during derivation;
- **turn count is the same currency as volume** — every command you run re-sends
  your whole context. One `apply-derived` call per round (never one apply per part
  file); spawn a round's subagents in ONE message, in waves of 5–10 agents (fewer
  waves = fewer of your turns);
- **read `config.yml` once and pass what workers need in their assignments**
  (`working_language` above all): a worker that opens the config spends a whole
  extra turn — with its full context re-sent — on one word;
- **never write ad-hoc scripts that touch the graph.** The shipped CLI covers every
  pipeline step; if a step seems missing, stop and report it to the user instead of
  improvising a mutation script;
- **conclusions about the AMG engine itself go to the user, not into the graph.**
  If you notice an engine bug, limitation, or improvement idea, say it in your
  report; never file it via `notes.py` — the graph is the project's memory, and
  engine observations would pollute its digest.

## Workflow

Run these from the project root. Use bash; keep the main conversation clean by
delegating per-unit reading to subagents.

1. **Heal first.** Replay any unfinished work and clear stale state:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
   python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
   ```

2. **Diff and write the skeleton.** Compute added/changed/deleted across all source
   folders, write structural nodes crash-safely (with the deterministic edge
   backbone: imports/defines/inherits + resolver-bound calls), and emit the semantic
   work queue:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/reconcile.py bootstrap .
   ```
   The printed counts tell you the scope. Derivations already in the persistent
   cache are restored automatically (`restored_from_cache`), and trivial code units
   (dunders, one-line getters) are summarized by code on the spot
   (`auto_summarized`, `config.yml → trivial_unit_max_lines`) — so
   `queued_for_semantic` is the REAL model work left; on a rebuild over unchanged
   content it is 0. If `queued_for_semantic` is 0 and the graph was already linked,
   stop here.
   To see how files were classified first (and whether tree-sitter is active), run
   `extract_structure.py . --stats`. It lists `ambiguous_files` (extensionless or
   unknown types it defaulted to prose), `resolved_by_override` (already settled),
   and a `classifier_hint` when any remain.
   If files are ambiguous you MAY refine them (optional — the queue is never blocked):
     a. spawn the `amg-classifier` subagent on the `ambiguous_files` list; it returns
        a compact `{ "<path>": {"category": code|doc|data, "language": <grammar|null>} }`
        mapping;
     b. write that mapping to `.claude/amg/work/classification-overrides.json` (merge
        if the file already exists);
     c. re-run `reconcile.py bootstrap .` — extraction reads the override BEFORE its
        own fallback, so each labeled file now routes to the right chunker (code by
        symbol, data by record) instead of the prose default.

3. **Semantic derivation (bulk).** Check the queue's shape with `inspect_queue.py .`
   (counts by category / subtree / kind + the progress block) — do NOT read
   `queue.json` into your own context. Split the queue with the helper:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/partition_queue.py .   # -> work/queue-<part>[-NN].json
   ```
   It groups by subtree AND bounds every batch by unit count / input volume
   (`config.yml → builder`), so a dense directory never becomes one giant batch.
   Then spawn an `amg-builder` subagent per batch **in parallel, in waves of 5–10
   agents per message**, each given only its batch PATH (`work/queue-<part>.json`),
   an output path like `.claude/amg/work/derived-<part>.json`, **and the
   `working_language` value** (you read the config once; a builder must not spend a
   turn reading it). The units carry their own `text`, so the
   builder summarizes straight from the batch (in the given `working_language`
   for docs/notes; code identifiers verbatim), proposes local edges (`documents` is
   mandatory on a doc unit with a real subject), **echoes each unit's `content_sha`**
   into its items, **checkpoints output in numbered parts**
   (`derived-<part>-p01.json`, …) and reports `BATCH COMPLETE: N/M` or
   `BATCH PARTIAL: N/M`. It does **not** write graph files.
   One caveat your wave instructions must never override: a unit **without** `text`
   (oversized, pointer only) is the one case where a builder legitimately reads the
   `source_path` slice — "do not read sources" applies to units that carry text.

4. **Apply derivations — ONE call per round — and verify the counts.** When a round
   of builders has returned, apply every produced part file in a single command:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/reconcile.py apply-derived .
   ```
   It consumes ALL `work/derived-*.json` (checkpoint `-pNN` parts included) in one
   transaction, prints one aggregated result, and moves the consumed files to
   `work/applied/` — so re-running it is a cheap no-op and is also the resume path.
   Never apply part files one command each: every command is a turn that re-sends
   your whole context. Applying sets `derived_from_hash = source_hash` and flips
   nodes to `active`, so a unit counts as derived only once its summary/edges are
   durably committed. A malformed item is repaired or skipped per item
   (`skipped_invalid` + reasons) and a torn checkpoint file lands in `work/invalid/`
   (`malformed_files`) — the batch never aborts; applied items are stored in the
   persistent derivation cache, so a future rebuild restores them for free.
   **Do not take a builder's word for completion**: compare the aggregated
   `applied + skipped_*` with the round's unit count, and treat any `BATCH PARTIAL`
   or shortfall as an interrupted batch — re-spawn a builder on the remainder (or
   just re-run step 2: an underived unit re-queues by construction, nothing is
   lost). **Every between-rounds report to the user carries the progress percentage**
   from `inspect_queue.py .` (`progress.derived_percent`) and names any interrupted
   batch explicitly («batch X: PARTIAL n/m — finishing the remainder»).

5. **Synthesis and gap report (strong model) — hubs BEFORE linking.** First write
   the deterministic hub anchors AND the one-file synthesis input, then spawn one
   `amg-synth` subagent:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/link_candidates.py --hubs .          # -> work/hub-candidates.json
   python .claude/skills/amg-bootstrap/scripts/link_candidates.py --synth-input .   # -> work/synth-input.json
   ```
   The synth works from those two files ONLY (`synth-input.json` is the whole
   summary layer as one sheet, gap material included — it never scans `nodes/`) and
   produces: top-level architecture/overview nodes anchored to the stable suggested
   ids, hub->member `documents` edges, weighted multi-membership (`part_of`) for
   cross-cutting topics, pattern nodes, and a **gap report** — undocumented code,
   drifted docs, and contradictions (from the sheet's `gaps` block). Give it the
   two input paths, the output paths, and the `working_language` value in the
   assignment. **When `--synth-input` reports `parts: N > 1`** (the layer outgrew
   `linker.synth_input_max_chars` — e.g. a smaller-window model), run the synth
   SEQUENTIALLY, one spawn per `synth-input-pNN.json`: part 1 does the full job
   (hubs + gap report), then apply its derivation (step 4) so the next spawn's
   `existing_hubs` already carries the taxonomy; each later part only attaches its
   rows (memberships, edges, patterns) to the existing hubs — never a second
   taxonomy. With one file (the normal case) it is a single spawn. Apply each
   derivation with the same single `apply-derived` call (step 4) and surface the
   gap report to the user.

6. **Global semantic linking (parallel).** The builders were each locked to their
   batch, so cross-domain edges (doc <-> code, example <-> guide, ADR -> code) need a
   global pass. Nominate candidates deterministically, then confirm them by meaning:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/link_candidates.py .   # -> work/link-batch-*.json
   ```
   (Uses cached embeddings for similarity when a backend is installed, else a
   lexical fallback — it degrades softly, never blocks.) Spawn an `amg-linker`
   subagent per batch **in parallel, in waves of 5–10 agents per message**, each
   given its `work/link-batch-<n>.json` and
   an output path like `.claude/amg/work/derived-links-<n>.json` — the linker writes
   it in checkpoint parts (`-p01.json`, …), so an interrupted batch keeps its judged
   nodes; apply the whole round with one `apply-derived` call (step 4). **Report
   linking progress the same way as building**: after every wave, tell the user
   "X/Y link batches judged" (plus any PARTIAL by name). The pass is
   incrementally re-runnable and it CONVERGES: already-linked pairs are never
   re-nominated, and rejected pairs are remembered too — each linker part ends with
   a judged record, and `apply-derived` retires a fully judged batch into
   `work/judged/`, whose pairs the nominator never re-proposes (only a crashed,
   under-covered batch is re-nominated). So the **completeness criterion is built
   in — repeat `link_candidates.py` + the wave until it emits zero new batches, or
   `metrics` reports `gate: ok`**; typically one or two waves suffice, and a second
   wave carries only genuinely new pairs, never re-rejections.
   **Re-checking the strays (`/amg relink`, "re-link the isolated nodes"):** when a
   COMPLETED build still shows isolated nodes (status/viewer), a plain re-run
   honestly reports "nothing new" — those pairs were already ruled on. The scoped
   re-open is `link_candidates.py --isolated .`: it nominates candidates ONLY for
   nodes with no resolved relation, deliberately ignoring their past rejections;
   judge and apply the batches exactly as above. Deleting `work/judged/` remains
   the FULL re-open (every judgment re-paid) — never the first resort.

7. **Acceptance gate (connectivity) + verification stamp.** Verify the build is one
   connected graph:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/reconcile.py metrics .
   ```
   `gate: ok` means: one dominant component, no unresolved internal edge targets,
   doc nodes carry `documents`. On `attention`, read the samples it prints —
   typically the fix is re-running step 6 over the remaining islands (or step 3 for
   still-stale nodes); unresolved `imports` to stdlib/third-party are legitimate
   and never flagged. The same verdict shows in `/amg status`.
   Then stamp verification in one deterministic sweep (seconds, no model):
   ```bash
   python .claude/skills/amg-retrieve/scripts/verify_claims.py --all --write --store .claude/amg
   ```
   A just-derived summary matches its live source by construction, so the sweep
   flips fresh nodes to `verified` — after it, an `unverified`/`stale` flag in a
   pack means "changed since the last sweep", a signal that actually
   discriminates, instead of burning on every node.

8. **Log.** The scripts append a txid-stamped line to `.claude/amg/actions.log`. Confirm
   to the user with the counts, the final progress percentage (`inspect_queue.py .`),
   the gap-report highlights, and the gate verdict — and say honestly whether every
   batch completed or something remains for the next run (an interrupted batch is
   normal: the next bootstrap re-queues exactly the remainder).

## Derivation strategy: eager vs lazy

`config.yml → derivation` controls how much of the semantic layer you build up front:

- **`eager`** (default) — derive every queued unit (steps 3–4 over the whole `queue.json`),
  then synthesize (step 5). This is the workflow above.
- **`lazy`** — build the structural map now, defer leaf detail until a query needs it. Use
  it only on a graph far larger than it is queried; the default stays `eager`.

In **`lazy`** mode, after step 2 split the queue into a priority batch and a deferred
remainder, and derive only the priority batch:
```bash
python .claude/skills/amg-bootstrap/scripts/partition_queue.py --priority .
#  -> work/queue-priority.json  (the map: module/class/package/file units)
#  -> work/queue-deferred.json  (leaf detail: functions, doc sections, records — deferred)
```
Run steps 3–4 over `work/queue-priority.json` **only**, and **always** run synthesis
(step 5) and the linking pass (step 6 — it works over whatever is derived and is
incrementally re-runnable, so deferred nodes join it as they are derived; the
connectivity gate never counts deferred `stale` nodes as fragmentation). The
structural skeleton (step 2) and the strategic synthesis are **never** deferred —
that is the safeguard: the map is always present, only fine detail waits.

**Background fill (over sessions, used-first).** A deferred unit is derived on first touch
by the retrieve skill (when a query activates it — phase B), and an idle or later bootstrap
pass derives the next batch by usage priority:
```bash
python .claude/skills/amg-bootstrap/scripts/partition_queue.py --priority --usage .
```
`--usage` also promotes units whose node appears in `work/usage.log` (actually used), so the
graph converges to fully derived — most-used first — without a bootstrap spike.

## Scale and safety notes
- For very large repos, run step 3 as several scoped subagents rather than one; each
  works in its own isolated context so nothing overflows.
- **Resume after an interrupted run — apply leftovers FIRST:** if a prior run left
  `work/derived-*.json` files (including `-pNN` checkpoint parts of a batch that
  never finished), one `apply-derived` call (step 4) consumes them all before you
  re-run step 2. It is freshness-safe — each item carries its `content_sha`, so
  `apply` skips any whose source has since changed (they re-queue) and you re-derive
  only the remainder, not the whole queue. The checkpoints make this loss-bounded: a
  builder, linker, or synth that died mid-batch still left every finished part on
  disk.
- Every write goes through the journal, so an interruption at any point recovers via
  step 1 on the next run.
- Re-running the whole skill on an unchanged repo is free: extraction is exact and
  unchanged units are skipped (no model calls).
- Never modify the configured source folders as a side effect — they are read-only.

## Reference
- `references/consistency-model.md` — guarantees, journal protocol, recovery,
  reconcile rules, crash-point analysis.
- `scripts/graph_store.py` — transactional store (`recover`, `verify`).
- `scripts/reconcile.py` — diff engine (`bootstrap`, `plan`, `apply`, `apply-derived`
  — every `work/derived-*.json` in one call, consumed files move to `work/applied/`).
- `scripts/extract_structure.py` — deterministic source → units: classifier +
  chunker registry (python/tree-sitter/markdown/text/json/pdf/docx/xlsx), ignore
  defaults, `--stats` for a classification + extractor-availability summary.
- `scripts/partition_queue.py` — split `work/queue.json` into `work/queue-<part>[-NN].json`
  batches by subtree, bounded by unit count and input volume (`config.yml → builder`);
  `scripts/inspect_queue.py` — a read-only queue summary (counts by category / subtree /
  kind, units carrying text) plus the build-progress block (derived vs stale, percent).
- `scripts/link_candidates.py` — deterministic prep for the global passes:
  candidate nomination by cached-embedding similarity (lexical fallback) into
  `work/link-batch-*.json`, `--hubs` for the stable hub anchors
  (`work/hub-candidates.json`), `--synth-input` for the synthesis sheet
  (`work/synth-input.json`, split into `-pNN` parts over the cap), and
  `--isolated` for the scoped stray re-check (`/amg relink`).
- Subagents: `../../agents/amg-builder.md`, `../../agents/amg-synth.md`,
  `../../agents/amg-linker.md` (global linking, step 6),
  `../../agents/amg-classifier.md` (optional, for ambiguous files).
