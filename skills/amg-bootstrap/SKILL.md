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
  Triggers: "bootstrap the graph", "index this project", "the graph is stale", "I
  added/changed/deleted files", "reconcile AMG", "build memory for this project".
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

## Workflow

Run these from the project root. Use bash; keep the main conversation clean by
delegating per-unit reading to subagents.

1. **Heal first.** Replay any unfinished work and clear stale state:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/graph_store.py recover
   python .claude/skills/amg-bootstrap/scripts/graph_store.py verify --repair
   ```

2. **Diff and write the skeleton.** Compute added/changed/deleted across all source
   folders, write structural nodes crash-safely, and emit the semantic work queue:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/reconcile.py bootstrap .
   ```
   The printed counts tell you the scope. If `queued_for_semantic` is 0, the graph
   is already current — stop here. To see how files were classified first (and
   whether tree-sitter is active), run `extract_structure.py . --stats`. It lists
   `ambiguous_files` (extensionless or unknown types it defaulted to prose),
   `resolved_by_override` (already settled), and a `classifier_hint` when any remain.
   If files are ambiguous you MAY refine them (optional — the queue is never blocked):
     a. spawn the `amg-classifier` subagent on the `ambiguous_files` list; it returns
        a compact `{ "<path>": {"category": code|doc|data, "language": <grammar|null>} }`
        mapping;
     b. write that mapping to `.claude/amg/work/classification-overrides.json` (merge
        if the file already exists);
     c. re-run `reconcile.py bootstrap .` — extraction reads the override BEFORE its
        own fallback, so each labeled file now routes to the right chunker (code by
        symbol, data by record) instead of the prose default.

3. **Semantic derivation (bulk).** Read `.claude/amg/work/queue.json`. To see its shape
   first, run `inspect_queue.py .` (counts by category / subtree / kind). If it is large,
   split it into batches by subtree with the helper rather than an ad-hoc script:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/partition_queue.py .   # -> work/queue-<part>.json
   ```
   Then spawn an `amg-builder` subagent per batch **in parallel**, each given its
   `work/queue-<part>.json` and an output path like `.claude/amg/work/derived-<part>.json`.
   The subagent reads the queued units, writes summaries (in the configured
   `working_language` for docs/notes; keep code identifiers verbatim) and local edges,
   **echoes each unit's `content_sha`** into its output item (so a re-run re-derives only
   what changed), and returns a one-line summary. It does **not** write graph files.

4. **Apply derivations.** For each produced file, apply it transactionally:
   ```bash
   python .claude/skills/amg-bootstrap/scripts/reconcile.py apply .claude/amg/work/derived-<batch>.json .
   ```
   This sets `derived_from_hash = source_hash` and flips nodes to `active`, so a unit
   counts as derived only once its summary/edges are durably committed.

5. **Synthesis and gap report (strong model).** Spawn one `amg-synth` subagent. It
   reads the now-populated nodes and produces: top-level architecture/overview nodes,
   cross-domain edges (e.g. `documents` from a doc section to the code it describes),
   weighted multi-membership (`part_of`) for cross-cutting topics, and a **gap
   report** — undocumented code (code nodes with no inbound `documents` edge),
   drifted docs (docs referencing changed/removed code), and contradictions. Apply
   its derivation file the same way (step 4) and surface the gap report to the user.

6. **Log.** The scripts append a txid-stamped line to `.claude/amg/log.md`. Confirm
   to the user with the counts and the gap-report highlights.

## Scale and safety notes
- For very large repos, run step 3 as several scoped subagents rather than one; each
  works in its own isolated context so nothing overflows.
- **Resume after an interrupted run:** if a prior run left `work/derived-*.json`
  (builders wrote them, but `apply` never ran), apply those FIRST (step 4) before
  re-running step 2. It is freshness-safe — each item carries its `content_sha`, so
  `apply` skips any whose source has since changed (they re-queue) and you re-derive
  only the remainder, not the whole queue.
- Every write goes through the journal, so an interruption at any point recovers via
  step 1 on the next run.
- Re-running the whole skill on an unchanged repo is free: extraction is exact and
  unchanged units are skipped (no model calls).
- Never modify the configured source folders as a side effect — they are read-only.

## Reference
- `references/consistency-model.md` — guarantees, journal protocol, recovery,
  reconcile rules, crash-point analysis.
- `scripts/graph_store.py` — transactional store (`recover`, `verify`).
- `scripts/reconcile.py` — diff engine (`bootstrap`, `plan`, `apply`).
- `scripts/extract_structure.py` — deterministic source → units: classifier +
  chunker registry (python/tree-sitter/markdown/text/json/pdf/docx/xlsx), ignore
  defaults, `--stats` for a classification + extractor-availability summary.
- `scripts/partition_queue.py` — split `work/queue.json` into `work/queue-<part>.json`
  batches by subtree (for parallel step 3); `scripts/inspect_queue.py` — a read-only
  queue summary (counts by category / subtree / kind, units carrying pre-extracted text).
- Subagents: `../../agents/amg-builder.md`, `../../agents/amg-synth.md`,
  `../../agents/amg-classifier.md` (optional, for ambiguous files).
