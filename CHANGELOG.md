# Changelog

All notable changes to AMG are documented in this file. Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer in pre-1.0 mode (rules: CLAUDE.md §10, roadmap §5 granularity rule).

## [Unreleased]

## [0.8.0] — 2026-06-15

Stage 6 closed: the `amg-classifier` verdict is now applied in code — ambiguous files route to the right chunker through overrides instead of defaulting to prose; an integration sandbox is built; and the Hebbian-weights question is settled by measurement — the blind rule is *harmful* on a realistic sparse graph, so the default stays off pending a redesigned rule.

### Added
- Classifier overrides in `extract_structure.py` (`load_overrides` / `_route_override` / `_classify_path`): extraction reads `work/classification-overrides.json` **before** its fallback classification and routes each labeled file to the matching chunker (`code`+`python`→ast, `code`+grammar→tree-sitter, `data`→json, `doc`→paragraphs). An override wins even over a known extension (manual correction); a missing or malformed file degrades to an empty map, so bootstrap is never blocked. `--stats` now reports `resolved_by_override` + `ambiguous_files` (unresolved only) + `classifier_hint`.
- `selftest_extract_overrides.py` (route / load robustness / ambiguous→resolved / precedence).
- `sync_testbed.py` — the manual predecessor of the Stage 10 installer: idempotently mirrors `skills/` + `agents/` (incl. `notes.py`) into `<testbed>/.claude/` and the `entrypoint/CLAUDE.md` activation block into the sandbox root, never touching the sandbox's own graph / config / sources.
- Integration sandboxes (live outside the repo, uncommitted): `../amg-testbed` (a delivery-service toy where the classification cycle was reproduced end-to-end) and `../amg-bigtest` (a 190-node sparse `depends_on`-chain graph — the stand for the Hebbian re-measurement, with `gen_big.py`, `_measure_big.py`, and a README).

### Changed
- `reconcile.plan` passes `amg_root` to `extract`. Workflow prompts updated: `amg-bootstrap/SKILL.md` (step 2) and `agents/amg-classifier.md` describe the ambiguous → classifier → write-overrides → re-run loop.
- Docs synced for the classifier: `04-ingest.md` (new "Overrides: вердикт классификатора, действующий в коде" + `--stats` fields), `08-agents-skills.md` (§ `amg-classifier` / `amg-bootstrap`).
- THEORY gains §8.2 (a human-readable account of what measuring the Hebbian rule showed); `07-consolidation.md` and `GUIDE.md` record the measured harm next to the `apply_hebbian`-off rationale. Roadmap gains §3.4 ("документация пишется для людей": narrative over terse theses) and detailed Stage 13/14 tasks for a redesigned Hebbian rule + its validation protocol on `amg-bigtest`.

### Fixed
- Audit 1.13: the `amg-classifier` result is applied by code, not just described in a prompt.
- `06-retrieval.md` doc↔code drift: the default embedding model is `potion-retrieval-32M`, not the older `potion-base-8M`; noted that `backend: auto` tries backends light→heavy.

### Decided
- `weights.apply_hebbian` stays **false** — now by direct measurement, not only caution. Blind co-activation Hebb is *inert* on a small dense graph (weights barely move the ranking) but **monotonically harmful** on a large sparse one (recall ≈ −0.10, hop-recall ≈ −0.14 over 8 folds, across BM25 / model2vec / ST seeds): reinforcing the already-central edges into conductance "highways" starves the multi-hop periphery where the gold lives (THEORY §8.1–8.2). A useful Hebb needs an outcome-gated, discriminative rule (roadmap Stage 13 task 9 + Stage 14 task 8), validated on the `amg-bigtest` stand.
- Embedding defaults need no change. The light multilingual default (`potion-multilingual-128M`) recovers a Russian cross-lingual gold as well as the heavy ST `paraphrase-multilingual-MiniLM-L12-v2` (recall 1.0 vs BM25 0.0 on the xlang demo), so the `embed.py` backend order is **not** reordered (ST needs torch, and a segfault at encode-time is uncatchable). ST stays an optional, measured upgrade; installer guidance for non-English recorded under Stage 10.

## [0.7.0] — 2026-06-14

Stage 5 closed: decisions, conclusions, open questions and plans are captured into the graph through a safe, crash-safe API — no hand-editing of node files.

### Added
- `notes.py add` (new, in `amg-bootstrap/scripts`): writes an authored node through a `graph_store` transaction (recover → atomic commit), reusing reconcile's node helpers (`serialize_node`/`node_relpath`/`_merge_edges`/`_merge_part_of`). Types `note`/`decision`/`adr`/`open_question`/`plan`; fields `source_kind: authored` + `policy: authored` (the preservation rule — a note survives any later bootstrap, since the deletion/move passes only ever touch derived_from_file+mirror nodes), `status` (default `captured`), plus `created`/`updated`/`lang`/`tags`/`part_of`/`edges`. Content-addressed id `note:<slug>-<hash8>` (identical re-capture → one node, not a duplicate; `--id` for a stable, revisable node).
- `created` and `tags` authored-node fields, and edge `origin: authored`; documented in `02-data-model.md` (types, status, fields, origin).
- `selftest_notes.py` (7 checks): fields/bucket, content-addressed identity + created-preserving update, explicit-id merge, crash recovery mid-commit, survives a bootstrap unpurged, found in retrieval by summary and by tag, episodic in the consolidation plan.

### Changed
- `episodic_types` default gains `open_question` and `plan` (`config.yml` + `consolidate.DEFAULTS`): they are transient authored states consolidation must **revisit** (an answered question / a done plan is promoted or retired) rather than living forever `active` — otherwise a long-answered question is surfaced as open (confidently-stale memory). `decision`/`adr` stay out (they are protected, not episodic).
- `retrieve.load_nodes` folds a note's `tags` into the BM25 bag, so a note is findable by tag, not only by summary/body.
- Capture loop switched to the API everywhere it was described as hand-writing nodes: `amg-consolidate` SKILL (step 2), `entrypoint/CLAUDE.md`, `08-agents-skills.md`; user how-to in `GUIDE.md`; `THEORY §13` (capture is a safe transactional API; transient types are revisited at consolidation).

### Decided
- The note id uses a neutral `note:` namespace for every type (not `<type>:`), because a later `promote` changes `type` while the id is immutable. `status: captured` is intentionally absent from `status_prior` (multiplier 1.0 — a fresh note is found immediately, not hidden); consolidation raises it to `active` via `promote`.

## [0.6.0] — 2026-06-14

Stage 4 closed: compaction can no longer silently hurt retrieval — it passes an automatic recall check (eval gate) measured on a clone of the graph before the real graph is touched. The Hebbian-default question is resolved: the mechanism is proven correct by a synthetic control, but the default stays off pending a measurement on real data.

### Added
- Eval gate in `consolidate.apply_actions` (`_eval_gate`): a baseline eval on the real graph, the same actions applied to a **clone** (`_clone_for_eval`, recursive `apply` with `_run_gate=False`), then a re-measure. The real graph is committed only if recall holds, so a `reject` needs no rollback (`revert` ≡ `reject` — measuring before commit makes a post-commit, archive-based revert unnecessary and brittle, since `redirect_inbound`/`merge` would already have rewritten neighbors). Metric: `pack_recall` (compaction changes pack composition, not just top-K ranking) under `min_recall_delta` plus aggregate `hop_recall` under `min_hop_recall_delta`.
- `eval_gate` config block (`enabled` / `cases` / `min_recall_delta` / `min_hop_recall_delta` / `on_fail`) in `config.yml` and `consolidate.DEFAULTS`/`load_config`.
- Report `work/eval-gate-report.json`: status, deltas, thresholds, and per-case `regressions` — which gold ids dropped out of the pack and which actions touched them (`_action_ids` attribution).
- `eval_retrieval.build_hebbian_demo` — a positive control proving the Hebbian mechanism: a gold node behind a deliberately weak edge is missed with static weights (hop-recall 0) and recovered after folding a co-activation journal (hop-recall 1). `evaluate_case` now also returns `pack_gold` (gold present in the assembled pack) for gate attribution.
- `selftest_consolidate.py` grows to 16 checks: `test_eval_gate` (harmful compaction rejected with the graph intact + attributed; safe applied; `warn` applies and records), `test_gate_robust` (no/dead cases → skip + apply, never a false reject), `test_hebbian_demo` (off→on hop-recall 0→1 positive control; recall held on good weights — negative control).

### Changed
- Compaction safety is now automatic, not a manual step: `amg-consolidate` SKILL and the `amg-consolidator` prompt note that the driver auto-gates compaction by recall, so the subagent proposes the smallest safe compaction and the gate catches over-aggressive cuts.
- `skills/amg-retrieve/evals/cases.json` rewritten from the stale FastMVC reference into a neutral template; its placeholder `gold_ids` resolve in no real graph, so a fresh install's gate stays safely disarmed (`status: skipped`) until the user labels real cases.
- Docs synced: `07-consolidation.md` (new "Автоматическая проверка полноты" section + `eval_gate` keys), `09-config.md` (`eval_gate` block), `10-eval-tools.md` ("Роль в консолидации"), `THEORY.md` §10.3 (forgetting is auto-measured) and §8.1 (Hebbian: mechanism proven, default deferred), `GUIDE.md`, `08-agents-skills.md`. Term cleanup in touched files: «харнесс» → «измерительный стенд», «гейт» → «проверка качества».

### Decided
- `weights.apply_hebbian` stays **false** by default. The synthetic demo proves only that the mechanism *can* help (a flattering graph always would); it does not show Hebbian helps on average, and the co-activation signal is partly circular (THEORY §8.1). Flipping the default requires a measured uplift on a real/representative graph with a real journal (Stage 6+); the tooling for that measurement is now in place.

## [0.5.0] — 2026-06-14

Stage 3 closed: consolidation is made safe, reversible, and honestly documented; edge weights are put under measurement. Compaction is gated by a real switch and protects valuable nodes in code; merge/sub-hub mutations preserve earned signal and memberships; created nodes match the synthesized canon; branches are computable on a real graph; and Hebbian weight updates are OFF by default until eval proves they help.

### Added
- `weights.apply_hebbian` (default **false**): `fold_weights` always accumulates `coact` (which feeds salience) but changes edge weight `w` only when enabled AND a new co-activation journal exists. Default-off breaks the partly-circular co-activation loop (weights → pack → pairs → same weights) until an eval on/off comparison proves the uplift (THEORY §8.1); tying decay to the journal makes a no-signal re-run a w-no-op (audit 1.9).
- Code-enforced compaction safety: `_is_protected` (protected types + high centrality via the shared `_degree_map`) spares valuable nodes from shorten/retire/merge-drop/summarize-archive without `force`; `compaction.enabled: false` blocks over-budget flagging and every compression action (audit 1.8, 1.11).
- `_branch_members` downward traversal — from a hub via containment edges (`HUB_DOWN_RELS` = documents/defines/specifies/implements/contains), stopping at any other hub — so branches are non-empty when a leaf's primary `part_of` is the directory string, and over-budget compaction is no longer inert (audit 1.20).
- `selftest_consolidate.py` grows to 13 checks (protect/force, centrality, enabled-gate, shorten idempotent, merge quality, sub-hub memberships, node schema, grounded inbound, branch downward, two-mode weights).

### Changed
- `merge` rewritten: edges fold by `(rel,to)` with max weight + **summed** `coact`, `part_of` combined (simplex), self-edges and edges into the drop set dropped, and a neighbor's edges deduped after `redirect_inbound` (audit 1.22).
- `introduce_subhub` replaces only the `parent_topic` membership with the sub-hub, preserving the node's other memberships, renormalized (audit 1.21).
- Consolidation-created nodes (`summarize_episodes`, `introduce_subhub`) match the synthesized canon — `policy: authored`, `source_hash`/`derived_from_hash: null`, `lang` from `working_language` — and land in the `_hubs` bucket (2.8 p.5).
- `salience` provenance counts inbound `documents`/`implements`/`specifies` edges, not only outgoing (2.8 p.6).
- `near_duplicate_sim`/`episodic_types`/`stale_age_days` are read from config (top-level) and added to the template (audit 1.17).
- `weights.default_edge_weight` is passed through `reconcile._merge_edges`, `retrieve.build_adjacency`, and `consolidate.fold_weights` — no longer a dead key (audit 1.23).
- Docs synced with the stage: THEORY §8 reworked (+ §8.1 "why Hebbian is off by default", the self-reinforcement risk) and §13; `07-consolidation.md`, `09-config.md`, `GUIDE.md`; roadmap compacted.

### Fixed
- `shorten` archives the full original once, so a repeated `apply` no longer overwrites it with the shortened body (audit 1.10).
- `weights` no longer claims strict idempotency it lacks: decay/reinforcement run only with a journal (audit 1.9).

## [0.4.0] — 2026-06-13

Stage 2 closed: retrieval is aligned with the data model — status-aware ranking, fixed tiers, consistent edge priors, by-key config merge, multilingual embedding defaults, an off-vs-on eval harness, activation explainability, and a lightweight pre-answer verification rule.

### Added
- Status prior (`status_prior`, `_apply_status_prior`): a per-status multiplier on the FINAL activation so a `superseded` claim (×0.2) never competes as an active fact, while `stale` is not penalized but flagged in the pack (`_STALE_MARK`); `disputed` (0.5) is forward-looking (Stage 14). It re-ranks by node validity without gating multi-hop flow (audit 1.12).
- `retrieve.py --explain`: decomposes PPR inflow on the raw (pre-prior) activation — `inflow(u→v)=d·π[u]·c/outsum[u]` — to show the edges that drove each top node, grounding the explainability claim (THEORY §14.2).
- `eval_retrieval.py --compare-embeddings`: runs labelled cases with embeddings off then on (a separate isolated cross-language demo, `build_embeddings_demo`); `run(cfg=...)` override.
- `selftest_retrieve.py` (new, 7 checks: status prior, stale mark, decision tier + body render, by-key merge, `--explain`, inspect bucket); `selftest_embed.py` gains multilingual-default and off-vs-on checks.

### Changed
- Config merge is key-by-key and recursive (`_deep_merge`): an incomplete `relation_priors` / `token_budget` / `status_prior` / `embeddings` overlays the built-in defaults instead of replacing the whole block, so a prior the user did not restate is no longer silently lost.
- Tier mapping: authored `decision`/`adr` → strategic, and their body (rationale) renders inline in any tier (`DOC_BODY_TYPES`).
- Embedding model default is chosen by `working_language`: English → `potion-retrieval-32M` (best static retrieval model); non-English → multilingual `potion-multilingual-128M` / `paraphrase-multilingual-MiniLM-L12-v2`. The seed stays deliberately light (not the ~8B MTEB leaders); any HF id can be set via `retrieval.embeddings.model`. `working_language` is threaded through `retrieve.load_config`.
- `supersedes` edge prior 0.5 → 0.3 (parity with `contradicts`); `exemplifies` is now emitted by `amg-builder`.
- `amg-retrieve` SKILL + `amg-retriever`: a lightweight "verify a code claim before you answer" rule (pointer resolves / grep the symbol / re-read a stale source; the source wins over a stale summary) — a cheap precursor to the Stage 13 verification layer.
- Demo ids use the `doc:` canon; the multi-hop demo (`build_demo_store`) and the embeddings demo are kept in separate graphs so a connected case cannot pull PPR mass from an isolated one.

### Fixed
- `status: superseded` now actually influences retrieval (audit 1.12).
- `inspect_graph.py --bucket` filters by the node's real on-disk directory via `_path`, so `notes`/`_hubs` match too (audit 1.26).
- A broken relative link in `06-retrieval.md`.

## [0.3.0] — 2026-06-13

Stage 1 closed: the node schema is unified to one canon and pre-canon graphs migrate to it.

### Added
- `migrate_schema.py` — one-shot, idempotent schema migration (one transaction under the writer lock): `source_kind: derived` → `synthesized`, `type: derived` hubs → `hub`/`overview`, tree-sitter grammar kinds → canonical, edge `origin` backfilled per owner class; `lineno` is left to the next `bootstrap`, which restores it via pointer drift. `selftest_migrate.py` is the sixth selftest.
- `branch_budget` node field (hub-only node-count budget read by `consolidate.py plan`); planned trust fields `confidence`/`provenance`/`verification` documented as a forward-looking subsection (Stage 13).
- tree-sitter binding adapter in `_treesitter_units` for both generations of `tree-sitter-language-pack` (classic py-tree-sitter API and the alef rewrite ≥ 1.8); without it fresh installs silently degraded non-Python files to a single file unit. `selftest_stage2.py` now exercises a live JavaScript parse.

### Changed
- tree-sitter unit `kind` is canonicalized to `function`/`class` at extraction (`_TS_DEF` map; struct/impl/trait/interface/enum → `class`), so non-Python code gets the same retrieval tiers and `path:line` pointers as Python (audit 1.25).
- reconcile pointer-drift now also reconciles a mirror node's `type` (extraction-owned), converging legacy grammar kinds without re-derivation.
- `amg-synth` emits `type: hub`/`overview` instead of `derived`; provenance lives in `source_kind: synthesized` (audit 1.4).
- Data-model doc brought to canon: new "Node types" section, `branch_budget` and planned fields in the frontmatter table, node-file-path example corrected (single underscore for `::`).

### Fixed
- Hub node types are `hub`/`overview`, so hubs land in the strategic tier and participate in branch compaction (1.4).
- `source_kind` normalization is completed for pre-canon graphs by the migration (1.5).
- Non-Python tree-sitter nodes no longer fall out of tiers and the `path:line` pointer format (1.25).

## [0.2.0] — 2026-06-13

Stage 0 closed: the reconcile core (`bootstrap`/`plan`/`apply`) is correct and self-healing.

### Added
- Move/rename detection: a deleted+added pair with the same content hash migrates earned fields (summary, semantic edges with their `coact`, `derived_from_hash`, multi-membership) onto the new id and redirects inbound references; a pure move costs zero model calls.
- In-project `imports` resolution via a dotted-name → path map; ambiguous suffixes refuse to resolve, external imports stay dangling.
- Edge `origin` field (`structural | semantic | synthesized | consolidation`), stamped at every edge write site.
- Store-root resolution chain (`graph_store.resolve_amg_root`): `--root` → `AMG_AGENT_DIR` → config search upward → engine location → `.claude` default; `--root` flag on the reconcile and consolidate CLIs.
- `selftest_reconcile.py` — 16 regression scenarios for the reconcile core.

### Changed
- Queue items carry `qualname`/`lineno`/`lang` (source language) for the builder; node frontmatter carries `qualname`/`lineno`.
- `plan()` summary gained `moved`, `requeued_stale` and `pointer_refreshed` counters.
- Architecture docs 02/03/04/05/07/08 synced with the implemented core; `amg-builder` prompt input synced with queue fields.

### Fixed
- Under-derived nodes are re-queued on every run; a queue lost to a crash self-heals on the next bootstrap (audit 1.1).
- Code pointers render `path:line` — `lineno`/`qualname` persisted and quietly refreshed when a unit shifts without a content change (1.2).
- Structural edges are rebuilt when a source unit changes; earned semantic edges and weights survive (1.3).
- `source_kind` taxonomy normalized — consolidation-created nodes are `synthesized`, the `derived` value is gone from the code (1.5).
- Multiple derivation items accumulate `part_of` instead of overwriting it; no premature stale→active flips without a new summary (1.6, 1.7).
- In-project `imports` edges resolve instead of being 100% dangling (1.19).
- Markdown headings inside code fences no longer create sections (1.24).

## [0.1.0] — 2026-06-12
### Added
- Baseline of the source repository: engine (skills, agents), entrypoint activation template, config template, Russian documentation (THEORY, GUIDE, architecture 01–11), roadmap with audit items 1.1–1.30 and stages 0–18, development tooling (CLAUDE.md, STATUS.md).
