# Changelog

All notable changes to AMG are documented in this file. Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer in pre-1.0 mode (rules: CLAUDE.md §10, roadmap §5 granularity rule).

## [Unreleased]

## [1.10.0] — 2026-07-04

Stage 19 closed — graph building is correct, connected, and reproducible: the "deterministic edges before the LLM" principle (§4.2) is now fully realized for code, a global semantic linking pass covers cross-domain completeness, apply survives malformed items, build quality is a number (the connectivity gate), and a rebuild restores derivations verbatim from a persistent cache. Second of the three dogfood build-reliability stages (18–20); fixes audits §1.40–§1.46. Additive (MINOR): new edge types (`defines`, `inherits`) and new config keys only; no migration needed — an existing graph heals its edges on one bootstrap. Live dogfood acceptance is deliberately deferred to the joint Stage-20 run (connectivity and cost measured on the same stand, before/after).

### Added
- **Deterministic edge resolver** (audits 1.40/1.41): cross-file symbol tables (`_build_symbols`) resolve `calls` and the new `inherits` edges THROUGH each file's own import bindings (never by name coincidence — a wrong edge is worse than a dangling one); builtins, unknown receivers, and external names emit no edge at all. The chunkers now extract import bindings, dotted call chains, class bases, and `defines` lists (Python fully; tree-sitter best-effort via nesting and heritage-clause probing). New structural rels: `defines` (w 1.0 — the containment backbone module→symbol/class→method; the consolidation downward walk works on real graphs now) and `inherits` (β 0.8 / w 0.8).
- **Target canonicalization** (audit 1.42): a judgment-layer edge target written without its leading directories re-binds to the unique canonical id (same category and qualifier, path-suffix match) — at apply, in the diff branches, and in a whole-graph sweep each bootstrap, so a pre-resolver graph heals in one run while a canonical graph stays a strict no-op (order-insensitive edge comparison; `edges_refreshed` counter).
- **Per-item apply validation** (audit 1.43): `_sanitize_item` + a per-item guard — swapped `confidence`↔`edges` and doubled category prefixes are repaired automatically, malformed edge/membership entries drop one by one, anything else skips with `skipped_invalid` + reasons; one bad item never aborts the batch.
- **Connectivity acceptance gate** (audit 1.44): `reconcile.py metrics` + the `connectivity` block in `/amg status` — components, largest-component share, INTERNAL dangling targets separated from legitimate external `imports`, isolated nodes, doc nodes without `documents` (lazy-aware: stale nodes are never defects); advisory `ok | attention` verdict against the `connectivity_gate` config thresholds. Metrics live in the reconcile layer; `graph_store` stays domain-blind.
- **Global semantic linker** (audit 1.45): `link_candidates.py` nominates, per derived node, its top-K most similar nodes from other domains/files — cosine over the CACHED seed embeddings (numpy block-wise) or a lexical inverted-index fallback with no backend; already-linked pairs are never re-nominated (incrementally re-runnable) — into bounded `work/link-batch-*.json` with the global hub list; the new `amg-linker` subagent (module_summary tier; rendered by the installer like every agent) confirms candidates by meaning. `documents` is mandatory on doc units with a real subject (builder/linker prompts + the gate). Hubs are born by `amg-synth` alone, BEFORE linking, anchored to deterministic directory-derived suggestions (`--hubs` → `work/hub-candidates.json`) — a stable strategic taxonomy across rebuilds (audit 1.46).
- **Persistent derivation cache** (audit 1.46): applied per-unit derivations are stored under `cache/derivations/` keyed by content hash + contract version + working_language; `bootstrap`/`plan` restore hits automatically (`restored_from_cache`; `queued_for_semantic` becomes the real model work left) and `apply-cached` runs it manually — a wipe-and-rebuild restores summaries verbatim at near-zero cost (practical determinism, stronger than temperature=0). New keys: `derivation_cache`, `linker.{top_k,min_sim,batch_nodes}`, `connectivity_gate.{min_largest_share,max_dangling_internal}`, `inherits` in `relation_priors`.
- New theory: THEORY §4.1 (deterministic before the model; canonicalization), §4.2 (global linking over the summary layer; hubs precede linking), §4.3 (build reproducibility via the derivation cache).
- Regression: `selftest_build.py` — the full pipeline on a mini project with a deterministic stub builder (skeleton → malformed-items apply → gate metrics → idempotent re-run → link candidates → cache restore; 7 checks); `selftest_reconcile` cases 17–18. 22 engine selftests + `selftest_install` (6 agents) + mypy (18 modules, +`link_candidates`) green.

### Changed
- `amg-builder` prompt: `documents` mandatory with a real subject, most-specific-path guidance (the resolver re-binds prefixes), deterministic edges are not restated; `amg-synth`: anchors the hub taxonomy, runs before the linker, owns the strategic layer only; `amg-bootstrap` SKILL: an 8-step workflow (cache restore → builders → hub anchors → synth → parallel linking → gate).
- Shipped artifacts carry no roadmap references (config template cleaned; a stage-21 item now also mandates tightening the script-comment convention).

Commits `8141ac2` / `081847a` / `4b9f6eb` / `e25bf4e` plus the docs sync and the close.

## [1.9.0] — 2026-07-03

Stage 18 closed — deployment and store resolution are reliable: operations always hit the right store, an install never clutters the project, the install flow asks the right questions, and a global config layer is inherited by the local one. First of the three dogfood build-reliability stages (18–20); fixes audits §1.37/§1.38/§1.39/§1.49/§1.50 and two of the three §1.52 doc drifts. Additive (MINOR): the on-disk schema, journal, and skill/agent interfaces are unchanged; resolution semantics only get STRICTER (a source checkout / the home defaults layer no longer resolve as a store).

### Added
- **Global config layer** (audit 1.37, variant A): a global install (and `--project-only`) writes machine-wide PERSONAL defaults to `~/<agent_dir>/amg/config.yml` — the `models` tiering and the `retrieval.embeddings` block, from the template plus `--set-global` answers — and the project's local config omits those blocks so they actually inherit; the three loaders (`retrieve` / `consolidate` / `extract_structure` `.load_config`) deep-merge global → local, local wins per key. Inheritance is gated on the installer-written `agent_dir` key in the local config (it both names the environment's home layer and marks the config installer-made — hand-made minimal configs and test fixtures stay hermetic). `_read_models` renders subagents from the same merged view; uninstall keeps the layer (user preference data). Project keys (active, sources, working_language, budgets) always stay local — the project's git canon.
- **Installer answers** (audit 1.49): `--set` accepts dotted paths (`_set_nested` lands `retrieval.embeddings.enabled=auto` on the nested key, preserving comments; the shipped template stays `off`); new `--absorb-once` (fills `absorb_once_path`) and `--set-global`; a kept existing config prints its `in force:` values so a reinstall confirms instead of silently keeping unknown state.
- **"AMG is not installed" diagnostic** (audit 1.50): `extract_structure.load_config` (and `reconcile bootstrap` through it) exits with a message NAMING the resolved store root and the `--root` / `AMG_AGENT_DIR` / `install.py` remedies instead of an unhandled `FileNotFoundError` — deliberately not a silent `{}`: extraction without a config would masquerade as an empty "added: 0" run, exactly the masked-failure class this stage removes.
- Regression: `selftest_reconcile` cases 15–16 (checkout veto incl. an "initialized" checkout, preset priority, the home defaults layer, the `_default_store` mirror, the missing-config diagnostic); `selftest_install` grew the defaults-layer checks and `test_nested_set_and_absorb_once` (12 checks).

### Changed
- **Store resolution** (audit 1.39): `resolve_amg_root` (mirrored by `retrieve._default_store`) now vetoes any `amg/`-named candidate carrying the engine signature (`skills/` / `agents/` / `install.py` inside = the source checkout, whose root `config.yml` is the shipped template with example sources) — even when a stray run already created `nodes/`+`journal/` in it (the unconditional `plan()`→`init()` "initializes" a hijacked checkout, so an initialized-store test alone lies); the agent-dir presets (`{.claude,.agents}/amg`) are probed before an ancestor's bare `amg/`, which now must be an INITIALIZED store; the HOME level is skipped by the upward walk (its agent dir holds the global defaults config, not a project store) and the engine-location step requires an initialized store — two decisions beyond the task letter, without which the new global config would have reproduced §1.39 at the home level.
- **Install location** (audit 1.49): install runs FROM the AMG source folder kept OUTSIDE the project (`install.py --target <project>`; the model-driven flow takes the path to `INSTALL.md`; the folder is disposable after install) — README/README_RU/INSTALL/GUIDE rewritten accordingly (no code change needed: the installer already worked from `Path(__file__)`).
- Docs synced by layer: `03-storage` (the full resolution chain + veto — the primary home), `12-install` («Слои конфигурации», the nested setter, new flags, install-from-outside), `09-config` (a new «Два слоя» section; `schema_version` 2→3 — §1.52), `INSTALL.md` (the question flow incl. absorb_once and the reinstall confirmation; `--env codex` documented — §1.52), `01-overview` / `05-reconcile` / `06-retrieval` (resolution summaries), GUIDE («Откуда ставить», the config layers, and the team-work note per §1.38: prefer a global install; a local engine is either gitignored or committed uniformly), both READMEs. Stale "Stage 19" (environments) references in the shipped docs and the codex block template became implicit wording — an early slice of the stage-21 sweep; a new stage-21 item fixes the policy for roadmap references in the shipped control plane (none in executable prompts; past-stage/audit attribution only in script comments). No new theory: store resolution and config layering are deployment mechanics, not a memory "why".

Commits `25c3af2` / `299983b` / `99d4277` / `d4a204a` plus the close.

## [1.8.0] — 2026-07-02

Stage 17 closed — the single-project semantic layer matured without risk to base retrieval: project-local pattern nodes, optional lazy/on-demand derivation, and three deferred decisions taken by measurement. Additive (MINOR); defaults unchanged (`derivation: eager`, `apply_hebbian: false`). A real-project dogfood also surfaced systemic build-reliability defects, recorded as new roadmap stages 18–20 (translation / env-testing renumbered → 21 / 22).

### Added
- **Pattern nodes** — project-local generalization of recurring experience: types `architectural_pattern` / `recurring_fix` / `anti_pattern` / `migration_recipe` (synthesized, strategic tier, `retrieve.PATTERN_TYPES`); `amg-synth` emits them as a create-item plus `exemplifies` edges on the instances (via the type-agnostic `apply_derivation` path — no new engine code); eval guard `eval_retrieval.pattern_metrics` / `build_pattern_demo` / `--pattern-demo` (transfer_recall / false_analogy_rate / stale_pattern_rate); `selftest_pattern`. Theory — THEORY §13 (within-project analogy transfer, the neocortical-generalization analog).
- **Lazy / on-demand derivation** (opt-in, off by default): `config.yml derivation: eager|lazy`; `partition_queue.priority_split` + `PRIORITY_KINDS` + CLI `--priority` / `--usage` (derive the structural map first, defer leaf detail); `retrieve.stale_in_pack` (first touch is synchronous); SKILL orchestration in `amg-bootstrap` / `amg-retrieve`. The engine does not branch on the flag.
- **Hypervector / VSA research note** (THEORY §16): a distributed substrate would only ever be a derived index over the symbolic canon, not a replacement — not warranted at AMG's scale; no code.

### Changed
- Three deferred decisions taken by MEASUREMENT on `../amg-bigtest`: (1) **lazy off/on** (`_measure_lazy.py`) — bare deferral severs multi-hop reach (hop → 0), the synchronous first touch recovers it (hop 0.18 BM25 / 0.42 model2vec) while touching only ~16–23 of 140 deferred nodes per query → default stays `eager`, the §4.10 first-touch safeguard is numerically confirmed; (2) **Hebbian** re-measured (`_measure_big.py`, a regression matching Stage 14: recall 0.60→0.85, hop 0.25→0.72 over folds) → default `apply_hebbian` stays `false` (no real usage.log yet; GUIDE/READMEs now carry the numbers + an enable recipe); (3) **semantic-drift segmenter** measured NOT needed — structural chunkers keep units bounded (~60–3200 tokens); the only bloat is break-less prose, better served by a deterministic size split (04-ingest / roadmap §4.6).
- Docs synced with the implemented layer: THEORY §13/§16, `02-data-model` (pattern types), `06-retrieval` (pattern tier + lazy first touch), `10-eval-tools` (pattern metrics + `--pattern-demo`), `08-agents-skills` (pattern emission + lazy orchestration), `09-config` (`derivation` moved from planned to implemented), `04-ingest` (semantic-drift decision), GUIDE (pattern nodes, lazy derivation, Hebbian when-to-enable numbers), both READMEs.
- Roadmap: dogfood build-reliability audit §1.39–1.49 and new stages **18** (deployment & store resolution), **19** (correct & connected build — deterministic ref-resolution pass, backbone, global semantic linker, connectivity gate, derivation cache), **20** (economical build, ×20–30 without quality loss) — a real ~1 MB project built a fragmented graph (322 components, 1838 dangling edges) over ~30 h / ~70 M tokens; the mechanical layer is sound, the defects are in the semantic pipeline, the half-done §4.2 (calls/backbone/inheritance), and missing quality/reproducibility/cost controls. Translation → stage 21, env-testing → stage 22; §1.37/§1.38 folded into stage 18.

Commits `a5767d5` / `6947063` / `cd49eb0` (groups A–C) plus the close.

## [1.7.0] — 2026-06-26

Stage 16 closed — team work over a shared folder and an optional graph-in-git, without complicating the single-user mode. The graph is plain markdown, so a team shares it two ways and AMG only READS the project's existing git (it keeps no version control of its own). Additive and mostly hardening — a host-aware lock, resilience to a merge-conflicted node, best-effort git awareness — with no data-contract break (MINOR); single-user behavior is unchanged.

### Added
- Source-freshness-by-commit: `verify_claims.py --by-commit` (and `verify_by_commit`) — a cheap git-history triage that flags nodes whose source changed between their ingest `provenance.commit` and HEAD, one `git diff --name-only --relative` per DISTINCT commit (no content re-chunk), so it scales. It COMPLEMENTS the authoritative content-hash check; run it after a `git pull` to scope what to re-verify or bootstrap. Best-effort: no git / an unresolvable commit → reported, not flagged. New git helpers `reconcile._git_branch` / `_git_changed_since` beside `_git_commit`.
- Git-aware status: `lifecycle.status` (and `/amg status`) shows the current branch and commit — in every environment, since it is a plain script call, not a hook.
- Conflict detection: `reconcile.find_conflict_markers` scans `nodes/*.md` for the unambiguous git markers (`<<<<<<<` / `|||||||` / `>>>>>>>`; the `=======` middle is skipped — it collides with a setext heading underline) and surfaces conflicted files through `reconcile.plan`, `lifecycle.status` (a `conflicts` count + report line), and `/amg repair` / `session-start` (a note to resolve and re-bootstrap).
- A "Командная работа" / "Team work" section in the GUIDE (the how-to: shared folder, the recommended `.gitignore`, merge + conflict resolution, `--by-commit`, branch compare, the honest limits, and a hook-less/Codex note) and in both READMEs (in the body, moved from "planned" to "implemented"). Tests: `case_lock_cross_host`, `case_shared_folder_contention`, `case_merge_conflict_resilience`, `test_conflict_skip`, `case_merge_conflict_surfaced`, `case_freshness_by_commit`, and an extended `case_status`.

### Changed
- The single-writer lock is now HOST-AWARE for shared folders (`graph_store._lock_is_stale`): a pid-liveness probe is only meaningful on the same host, so a lock held by ANOTHER machine is reclaimed solely by the age threshold, never by a local pid probe — a teammate's live lock on a shared disk is no longer falsely stolen. The single-host path is byte-identical (single-user unchanged). When a live foreign writer holds the lock, automatic upkeep degrades gracefully (`lifecycle._heal` / `session_end` / `repair` and the `graph_store` CLI `recover` / `verify --repair` skip with a clean "locked" report instead of crashing); explicit writers still fail with an owner-identifying message.
- Read-path resilience to a merge-conflicted node: the three node parsers (`reconcile.parse_node`, `retrieve._parse`, `consolidate._parse`) now guard `yaml.safe_load` against a `YAMLError` or a non-mapping result → the node is skipped, so a single conflict-marked node no longer crashes every `load_nodes` (it previously took down retrieve / reconcile / consolidate / status; `export_graph` was already guarded). The branch stays at the STORE level (status), not in each node's provenance — `_provenance` is untouched, so frontmatter does not bloat and `migrate_schema` is unaffected.
- Docs synced by layer: `consistency-model.md` §9 (host-aware single-writer on a shared folder, graceful degradation) and §12 (a git merge conflict: detect, isolate, repair); `03-storage` (host-aware staleness rules + a "Командный режим" section); `02-data-model` (which files are the git canon vs disposable); `06-retrieval` (`--by-commit`, named "актуальность источника" in prose). No new theory — git over the markdown canon (§4.1) is instrumental. Branch comparison is a documented git pattern (`git diff` of the nodes, or `export_graph --json` on two branches), no new code. The Stage 16 body folded.

### Found (deferred)
- §1.36 — the action log `log.md` is written as per-line `## ` headings in a `.md` file, but it is a flat append log, not a document (no real markdown is logged; the `## ` prefix carries no code meaning — de-dup keys on `txid`, rotation counts lines, the status filter matches `source |`). Decision: make it a plain `.log` without the heading prefix. Out of Stage 16 scope (recorded per CLAUDE.md §4.3); home is a standalone fix or the Stage 18 doc pass.

## [1.6.0] — 2026-06-24

Stage 15 closed — a 3D graph viewer. The memory's structure can be opened as a self-contained, OFFLINE HTML page: rotate/zoom/pan, click a node for its full frontmatter and edges, color by bucket with arbitration verdicts highlighted, filters/search, and a large-graph mode that stays readable on big graphs. Additive — a new read-only export tool, vendored viewer assets, an optional config block — with no data-contract break (MINOR).

### Added
- `export_graph.py` (new, read-only, in `skills/amg-retrieve/scripts`): scans `nodes/*.md` and assembles a `{meta, nodes, links}` document, sharing one core (`build_graph_data`) between a JSON export (`--json`, for external graph tooling) and the HTML viewer (`--open` writes + opens `cache/graph.html`). It carries the FULL frontmatter of every node (not retrieve's BM25/PPR projection — the side panel shows it); links come from typed `edges` (rel/w/coact/origin) plus `part_of` memberships resolving to a real hub node; dangling edges are dropped, exactly as retrieval does. Read-only w.r.t. the graph (the only write is the disposable `cache/graph.html`). `selftest_export.py` (8 checks). Documented in 10-eval-tools / 01-overview / 02-data-model.
- A self-contained, OFFLINE viewer: the graph data, the vendored `3d-force-graph` library (1.80.0, MIT — `viewer/3d-force-graph.min.js`), and the glue (`viewer/viewer.template.html` + `viewer.js`) are inlined into one HTML, so it opens by double-click with no server and nothing fetched (the data rides in a `<script type="application/json">` because a `file://` page cannot fetch a sibling `.json` under CORS). Node color by bucket with Stage 14 `disputed`/`rejected`/`superseded`/`stale` highlighted; size by degree (hubs large); edge width by `w` (the Hebbian-tuned strength), `contradicts`/`supersedes` flagged; click → side panel with the full frontmatter and edges; filters (type/status/bucket), search (id/summary), cluster coloring, a light/dark toggle (GitHub palette, OS default), and a large-graph mode (hubs-first + expand-on-click + hide-weak-edges) so a big graph is not a hairball and does not hang. The idle WebGL render loop pauses while the tab is hidden; cross-browser prefixes for backdrop-filter and scrollbars.
- A `viewer` config block (a thin layer over the library): `quality` (auto|high|medium|low — node/edge smoothness, auto by on-screen count), `large_graph_mode` (auto|on|off — the startup large-graph view, baked into the export), `large_graph_nodes` (the auto threshold), `min_edge_weight` (the hide-weak-edges slider default), and `options` — a raw pass-through applied verbatim to 3d-force-graph, so config.yml need not enumerate the library's option surface. Documented in 09-config ("Просмотр графа").
- Launch surfaces (task 6): a `/amg view` verb (a deterministic read-only script, run directly), the `amg-retrieve` skill, a verbal request, and the CLI — wired into the `/amg` command, the activation block, and the AGENTS.md / AGENTS.codex.md portable blocks.

### Changed
- Docs synced with the implemented viewer, by layer: GUIDE "3D-просмотр графа" (forward → implemented, with what cluster coloring shows), architecture 10-eval-tools (the export/viewer section) + 01-overview + 02-data-model + 09-config, and a dedicated "3D graph viewer" section in both READMEs (moved from "planned" to "implemented"). No new theory — the viewer is instrumental over the existing model. Roadmap checkpoint 2.12 (GUIDE "3D") closed; the Stage 15 body folded.

### Found (deferred to Stage 18)
- §1.34 — hardcoded non-English (Russian) verbal-intent examples in `entrypoint/CLAUDE.md`. The engine is English-base and matches intent in any language semantically, so hardcoded words violate that invariant; the Stage 15 additions were fixed, the pre-existing rest is scheduled for the Stage 18 prompt pass.
- §1.35 — verbal memory commands are not anchored to an "AMG"/memory keyword, so a generic phrase ("show the graph", "wrap up") could false-fire an `/amg` operation; anchoring the triggers (across the activation blocks and the docs) is scheduled for Stage 18.

## [1.5.0] — 2026-06-22

Stage 14 closed — epistemic contradiction arbitration and an improved Hebbian weight rule. Memory now keeps itself current under conflicting claims (resolved by provenance, freshness and source rank — not query frequency) and can learn edge weights from the real task outcome instead of blind co-activation. Additive — new statuses, new consolidator actions, a new audit file, a retrieval intent flag — with no data-contract break (MINOR); the `apply_hebbian` default stays `false`.

### Added
- Epistemic arbitration in consolidation (mechanics deterministic, judgment by `amg-consolidator`): `make_plan` detects candidates — `contradictions` (node-vs-node pairs linked by a `contradicts`/`supersedes` edge, each side with its comparison inputs: `source_rank` per the source hierarchy code>doc>ADR>session>legacy>guess, `confidence`, freshness, `verification`, `provenance.kind`) and `source_contradicted` (nodes whose live-source check failed). `apply_actions` enacts five NON-destructive verdicts — `supersede` / `dispute` / `reject` / `keep_both_with_context` / `ask_user` — as a status change plus a linking edge (nothing archived or deleted, so no compaction gate, protection, or eval gate applies). New lifecycle statuses `disputed` / `rejected` with `status_prior` 0.5 / 0.1. THEORY §15.7, 07-consolidation ("Арбитраж противоречий"). `test_contradiction_plan` / `test_arbitration`.
- A durable arbitration audit trail `<amg>/arbitration.md` — each verdict (action, nodes, reason, the sources compared) is appended within the same transaction as the status change, so the basis of every memory verdict is visible (DoD: conflicts are not resolved silently).
- Retrieval conflict surfacing: a history/conflict query LIFTS the downrank on retired statuses (`superseded`/`disputed`/`rejected` — the user asked to see them; audit 1.12 closed), and a conflict query additionally SEEDS the conflict subgraph (`_conflict_nodes` — disputed/contradicted nodes + contradicts/supersedes endpoints) into the teleport, still shaped by the query. Intent is recognized by the MODEL — the retriever subagent reads the query in ANY language and passes `--intent history|conflict`; no language-specific keywords live in the code (audit 1.33). `disputed`/`rejected` pack trust marks. `test_intent_and_conflict`.

### Changed
- Improved Hebbian weight rule (`consolidate.fold_weights`), replacing the blind co-activation rule that measurably HURT recall on a sparse graph. It is OUTCOME-GATED — reinforce an edge only when both endpoints were USED in an accepted session (`work/usage.log`, the non-circular signal) by the discriminative headroom `hebbian_rate·(1−w)` — and EXPOSURE-GATED for decay (an edge merely surfaced in a pack but not used fades, demoting the "highways"); a reverted outcome weakens. Measured on `../amg-bigtest`: recall/hop rise monotonically over 1→2→4→8 folds across all three seeds — the inverse of the blind rule's monotonic drop; a dense dogfood (`../amg-testbed`) shows no harm. Because both measurements use a SYNTHETIC outcome signal (gold-as-used) and weight folding has no eval gate, the default `apply_hebbian` stays `false`; the real-usage measurement and the default decision are scheduled at Stage 17 (task 8). `lifecycle.session_end` reordered (dump → record_usage → fold) so the session's own outcome feeds the fold. THEORY §8.1/§8.2 updated with the measurement. `test_weights` / `test_hebbian_demo` rewritten for the new semantics.
- Prompts: `amg-consolidator` (an Arbitrate-contradictions section — source hierarchy → `verify_claims` → verdicts, "when unsure, dispute"), `amg-synth` (its `contradicts`/`supersedes` edges feed arbitration), `amg-retriever` (recognize intent + pass `--intent` + surface disputed/rejected), and the consolidate / retrieve SKILLs.
- `CLAUDE.md` §7 gains "Document layers" — a universal, role-based documentation principle (no file names): the theory layer ("why", scientific, timeless, no stage refs), the architecture-and-roadmap layer ("how / what / when built", cites stages), and the front-page-and-guide layer (practical, for a reader who never opens the theory). Docs synced across THEORY §8/§15.7, 02/06/07/08/09, GUIDE, READMEs. Stage 14 body folded; checkpoint 2.1.1 and audit 1.12 closed; audit 1.33 (hardcoded working-language) opened with a sweep scheduled at Stage 18.

## [1.4.0] — 2026-06-22

Stage 13 closed — provenance, confidence, and verification (the trust layer). Confidently-wrong memory is worse than none, so every fact now knows its origin and confidence, a code claim is checked against the live source before it is answered, and untrusted nodes are flagged (never silently downranked). Additive — new optional node fields, a new script, a config block — with no data-contract break (MINOR); old graphs are backfilled by `migrate_schema.py`.

### Added
- Trust fields on every node: `line_end`, `confidence`, `provenance{kind, commit?, derived_from?}`, `verification{status, method, last_verified_at?}` (`schema_version` → 3). Design decision — NO duplication: a file-projected node's origin already IS its flat `source_path`/`source_hash`/`lineno`/`line_end`, so `provenance` adds only `kind` + optional `commit`, plus `derived_from` (the "source ids") for synthesized/authored nodes. `confidence` is a DISPLAYED signal, not an activation multiplier (a just-changed node is often the most relevant — flag, don't bury). Documented in 02-data-model ("Слой доверия") and THEORY §15.
- `verify_claims.py` (new, in `skills/amg-retrieve/scripts`): lightweight verification of a code claim against the live source — re-chunk the current file with the same chunkers and compare → `verified` / `stale` / `contradicted` / `skipped` (file or symbol gone → contradicted; hash differs → stale, caught before reconcile even sees it). Read-only by default (a read-only retriever can run it before answering); `--write` stamps the `verification` block under the lock and refreshes the index. Targeted ids are read one file at a time (no full scan on the hot "before answer" path). `selftest_verify.py`.
- Ingest fills provenance/verification deterministically: `extract_structure` computes `line_end` where source lines are real (code, markdown/RST sections, log/session windows); `reconcile` stamps `provenance.kind` + best-effort git `commit` + `verification: unverified` on added/changed/move and RESETS verification on `changed` (the same hash machinery that flags `stale`); `apply_derivation` applies the builder's `confidence` (default `default_confidence` 0.7) and `derived_from` on synthesized nodes; `notes.py` records `kind: user|model_inference` (`user` → `verified/user`) with `--kind`/`--confidence`. `migrate_schema.py` backfills old graphs idempotently.
- Pack trust marking: `retrieve._trust_marks` flags `stale` / `unverified` / `contradicted` / low-confidence nodes in the pack (a flag never downranks — it prompts the model to confirm), and code pointers render the line RANGE (`path:start-end`). The trust fields are projected into the read-index (`confidence`/`verification`/`line_end`).
- Usage provenance (task 9): `retrieve._log_pack` writes the pack composition to `work/pack-log.jsonl`; `lifecycle.session-end` crosses it with the files the session's edit tools touched → `work/usage.log` (nodes actually USED + a coarse outcome). Kept SEPARATE from the blind `coactivation.log` and NOT read by consolidation — the non-circular substrate for Stage 14's Hebbian rule (usage comes from outside the ranking loop, §8.1). `selftest_usage.py`.
- Config: a top-level `verification` block (`enabled`, `verify_code_claims`, `warn_on_unverified`, `min_confidence_warn`, `default_confidence`), surfaced in `retrieve.load_config`.

### Changed
- `extract_structure`: DRY refactor — the per-file chunker dispatch is extracted into `_units_for_path`, reused by a new `units_for_file` (single-file chunk for verification). Behavior-identical.
- Prompts: `amg-builder` / `amg-synth` echo a `confidence` estimate (synth also `derived_from`); `amg-retriever` / SKILL amg-retrieve / the entrypoint base "verify a code claim before you answer" on `verify_claims.py` and the full mark set. `.claude` paths render per `agent_dir` at install (verified a render to `.agents` leaves no `.claude`).
- The engine passes `mypy --strict` over 16 modules (`verify_claims` added to the gate).
- New THEORY §15 "Модель доверия" (the source hierarchy, "confidently-wrong memory is worse than none", flag-don't-downrank, breaking the usage circularity); §17 "Родословная" renamed to "Истоки". Docs synced across 02-data-model / 05-reconcile / 06-retrieval / 08-agents-skills / 09-config / 03-storage / consistency-model §4 / 01-overview / GUIDE / READMEs. Stage 13 body folded; THEORY checkpoint 2.1.1 half-closed (the contradiction-arbitration cycle closes at Stage 14).

### Deferred
- The usage signal accrues but does NOT yet change weights — the improved (outcome-gated) Hebbian rule and contradiction arbitration are Stage 14. `verification.status: contradicted` and `status_prior.disputed` are seeded for it.

## [1.3.0] — 2026-06-21

Stage 12 closed — performance and scaling. Large graphs stay fast without giving up the Markdown canon: a generated SQLite read-index under retrieval (≈15× on the per-query load path), resumable derivation, queue helpers, model-effort tiering, a benchmark, and the transactional action log. Additive, no data-contract change (MINOR).

### Added
- Generated SQLite read-index `cache/index.sqlite` (new `skills/amg-retrieve/scripts/index_store.py`) under `retrieve.load_nodes`, the one per-query hot path. It stores a PROJECTION of the retrieval-relevant node fields (id/type/status/summary/source_path/lineno/searchable-text/edges/part_of/body), not the whole frontmatter or the sources — Markdown stays canon. `load_nodes` reads the index when a cheap stat-walk signature of `nodes/` (stored inside the sqlite, taken before the scan) matches, else falls back UNCONDITIONALLY to the scan and best-effort rebuilds — never a wrong result, only a faster path. All six graph writers (`reconcile.plan`/`apply_derivation`, `consolidate.fold_weights`/`apply_actions`, `notes.add_note`, `migrate_schema`) incrementally upsert it under their lock via a new `graph_store.Transaction.node_paths()`. The node-dict shape is byte-identical to the scan (shared `retrieve._node_from_meta`), so BM25 / build_adjacency / assemble_pack are untouched and `eval --make-demo` hop-recall stays 1.00. Measured ≈15× at 7600 nodes (7.9s → 0.5s). Disposable, no config key, no size threshold. `selftest_index.py`.
- `bench.py` (+ `selftest_bench.py`): a read-only performance ruler over the engine — scan vs index `load_nodes`, `build_adjacency`, `retrieve`, `eval`, and (with `--project`) bootstrap; best-of-N, embeddings off. A self-contained deterministic generator (`--make-bench --nodes N`) or any `--store`.
- Queue helpers `partition_queue.py` (split `work/queue.json` into per-subtree batches for parallel builders) and `inspect_queue.py` (a read-only queue summary), with `selftest_queue.py`.
- Resumable derivation: `amg-builder` echoes each unit's `content_sha`, and `reconcile.apply_derivation` skips an item whose `content_sha` no longer matches the node's current `source_hash` (`skipped_stale`) — a leftover `derived-*.json` from an interrupted run never derives against stale content, and re-derives only what changed. `case_resume_freshness`.
- Transactional action log: `GraphStore.append_log` (de-dup by txid, rotation into `archive/`), written by both `consolidate` and `reconcile` (audit 1.15). `selftest_graph_store.case_action_log`.
- Model-effort tiering: the `config.yml` `models` block ships the structured `{model, reasoning_effort}` form with a gradient — `discovery: {haiku, low}`, `synthesis: {opus, high}`; `module_summary` stays flat (its summaries feed retrieval). A measurement protocol is documented in 09-config; the live (paid) measurement is deferred.

### Changed
- `consolidate.make_plan` restricts the near-duplicate Jaccard scan to episodic, non-source-derived nodes (the ones `merge_near_duplicates` can merge) — O(k²) instead of O(n²) over the whole graph, and it no longer proposes futile mirror merges (audit 1.27). `test_near_dup_scope`.
- The engine passes `mypy --strict` in one pass; the gate is `mypy.ini` (`files=`, run `python -m mypy` with no args) over 15 engine modules; selftests are excluded by decision (audit 1.31).
- The "don't edit the engine mid-task" barrier is sharpened into a named "Boundaries" section across all three activation blocks and 08-agents-skills (registry 2.16 p.6).
- Docs synced: the read-index documented across 06-retrieval (main), 02-data-model / 03-storage / consistency-model §4 (layout, `node_paths`), 09-config (caches are keyless); resumable derivation in 05-reconcile / 08; the `episodic_types` drift fixed and near-dup scope in 07-consolidation; `log.md` described as transactional in 02-data-model + THEORY §9; model tiering + bench in 09-config / 10-eval-tools / GUIDE; READMEs move index + scaling from "planned" to "implemented" (stages 0–12). Roadmap §1 items 1.15 / 1.27 / 1.31 collapsed; the Stage 12 body folded.

### Deferred
- The adjacency cache (Stage 12 task 3) is deferred by measurement: `build_adjacency` is <1% of `load_nodes`' cost and serializing a large adjacency blob would risk costing more than the rebuild; the index already removed the dominant cost. Revisit only if a tens-of-thousands measurement disproves it (then a binary format, not JSON).
- Lazy/on-demand derivation (Stage 12 task 7): the safe building blocks are in place (structural-first, partition scoping, resumable derivation); the speculative `derivation` switch is not introduced — full on-demand/background laziness with an eval-gated default flip stays roadmap §4.10 / Stage 17.

## [1.2.0] — 2026-06-18

Stage 11 closed — broader input formats. AMG now ingests far more than code, Markdown, and JSON: dedicated chunkers for reStructuredText, NDJSON, CSV/TSV, logs, presentations, and external chat exports, plus recursive chunking of deeply nested JSON, a frozen `absorb_once` source policy, and source-overlap / missing-path diagnostics. Additive, no data-contract change (MINOR).

### Added
- Structural chunkers in `extract_structure.py`, all mapping to the canonical node types (`record` / `sheet` / `block` / `section`) so retrieval and consolidation consume them unchanged: RST (underline/overline headings → sections), logs (`.log` grouped into bounded episode windows of `log_group_lines` lines by a leading timestamp → blocks), NDJSON (one record per JSON line), CSV/TSV (one structural sheet unit — headers + sample rows, like XLSX), PPTX (one section per slide via optional `python-pptx`, graceful skip; `.pptx` removed from `BINARY_EXT`).
- Recursive JSON chunking: a large nested container (serialized JSON over `json_recurse_min_chars`, holding nested structure) is split into sub-records by key path (`a.b.c` / `a.b[0]`) under `json_max_depth` / `json_max_nodes`, with stable ids; a small or flat value keeps the original one-record shape and hash (existing data files unchanged), and a large FLAT scalar list is not exploded into a node per element.
- External chat chunker `_chat_units`: a JSON array / a `{messages:[...]}` object / NDJSON of message objects (the OpenAI/Anthropic shape, with tolerant key synonyms) → one episodic section per message, with role/timestamp/thread folded into the text for attribution and a weak `follows` edge to the previous turn in the same thread (conversation adjacency; `build_adjacency` symmetrizes, so one edge reaches both neighbours; relation prior β 0.4). Detection sniffs json/ndjson structure — an ordinary record array is left to the data chunker. A flat role-marker dump (`=== Human ===`) dropped into a normal source routes to the existing session chunker (`_has_role_markers`).
- Source policy `absorb_once` (key `absorb_once_path`): ingest once and freeze — like `absorb` (deleting the source keeps the node), but later changes to the source are ignored too (no re-derivation, no pointer drift). Enforced by a `frozen` gate in `reconcile.plan`.
- Source hygiene: `detect_policy_conflicts` flags a file that falls under more than one policy (audit 1.29) — `policy_conflicts` in `plan`, `overlapping_sources` + a hint in `--stats`; the dedup keeps the most-preserving policy (`absorb_once` > `absorb` > `mirror`). `reconcile.plan` reports `missing_sources` so a typo'd source path is visible, not hidden behind "added: 0" (audit 1.30, remainder).
- Config keys: `json_max_depth` (4), `json_recurse_min_chars` (2048), `json_max_nodes` (500), `log_group_lines` (50), `absorb_once_path`; the `follows` relation prior (0.4). `python-pptx` added to the `text` dependency group (`requirements.txt`, `install.py DEP_GROUPS`).
- Regression: new `selftest_chunkers.py` (the stdlib chunkers + recursive JSON + chat + reconcile buckets); PPTX added to `selftest_stage2.py`, overlap/missing to `selftest_ignore.py`, `absorb_once` to `selftest_reconcile.py`. 13 selftests green.

### Changed
- `reconcile._structural_edges` emits the `follows` edge from a chat unit's `follows` hint (origin structural, w 0.3), like `imports`/`calls`. `resolve_sources` recognizes `absorb_once_path` and orders the policies so the most-preserving one wins an overlap; the gate is fixed for configs that set only `absorb_once_path`.
- Docs synced: `04-ingest.md` (the new chunkers, recursive JSON, the external-chat section, source overlap; the "Развитие" stubs trimmed to semantic-drift only), `05-reconcile.md` (the `frozen` case, the `follows` edge, the `plan` diagnostics), `09-config.md` (the chunking tunables, `absorb_once_path`, the `follows` β; `absorb_once` moved out of "planned"), `02-data-model.md` (the `absorb_once` policy and the `follows` edge), `THEORY.md` §11–§13 (broader input domains, the absorb_once freeze as a third point on the mirror↔absorb axis, conversation adjacency as the episodic skeleton), `GUIDE.md`, README_RU + README. Roadmap §1 items 1.29 / 1.30 collapsed; the Stage 11 body folded.

### Deferred
- Semantic-drift segmentation of long unstructured prose (Stage 11 task 2) is deferred to **Stage 17, task 6**, with the full spec and gating conditions kept in roadmap §4.6: implement last, only under the eval completeness gate, and only if the structural chunkers leave measured pain. The structural chunkers shipped here cover the overwhelming majority of inputs, and accuracy outweighs token economy.

## [1.1.0] — 2026-06-17

Sub-stage between Stage 10 and Stage 11 (control-plane / portability): the `config.yml` `models` block becomes a working setting (audit 1.14), a third installer environment `--env codex` is added (Codex is not skill-less), and the engine prompts are made portable (audit 1.32). Additive, no data-contract change (MINOR).

### Added
- Structured model tiering (audit 1.14): `models.<role>: {model, reasoning_effort}` (backward-compatible with a flat model string) is rendered by the installer into the subagent definitions — Claude Code: `model`/`effort` in `agents/amg-*.md` frontmatter; Codex: `model`/`model_reasoning_effort` in `.codex/agents/*.toml`. `reasoning_effort` (`minimal|low|medium|high|xhigh|max`) is clamped per environment (no `off`; unset → omitted, the tool default applies). `model` is an opaque pass-through string (a Claude alias, a pinned id like `claude-opus-4-8`, or another provider's id); multi-provider routing is a deployment concern (gateway / Bedrock / Vertex), not a model string, so AMG does not implement it. Role→agent map: discovery→{classifier,retriever}, module_summary→{builder}, synthesis→{synth,consolidator}. `install.py`: `render_agent_models` / `render_codex_agents` / `_resolve_role` / `_clamp_effort` / `_set_agent_field` / `_read_models`.
- Installer environment `--env codex` (3-way `_env_kind`: claude-code | codex | generic). Codex is NOT skill-less — it has skills (`.agents/skills`) and subagents (TOML in `.codex/agents`). The codex mode places skills under `.agents/skills`, renders the subagents as TOML in `.codex/agents` (model + model_reasoning_effort from `models`; Claude-alias defaults omitted for Codex), and injects a skill-aware block from the new template `entrypoint/AGENTS.codex.md`; Claude hooks and the `/amg` command are not written. The skill-less `--env generic` block is retained (Qwen Coder and other AGENTS.md envs). `agent_dir`/`entrypoint` default per environment (codex/generic → `.agents`/`AGENTS.md`). Reinstall/uninstall are env-aware (uninstall clears `.codex/agents/amg-*.toml`).
- New architecture doc `docs/ru/architecture/12-install.md` — the installer's internals (engine/graph planes, the install matrix, path rendering, block injection, settings merge, config templating, model tiering, reinstall/uninstall), distinct from the user-facing INSTALL.md; registered in the architecture map.
- `selftest_install.py`: `test_models_render` and `test_codex_env` (new); `test_generic_env` switched to `--env generic`; `test_agents_env` asserts the engine prompts + `eval_gate.cases` are rendered. `selftest_reconcile.py` root test covers the `.agents` config probe. 13 selftests green; generated Codex TOML parses with `tomllib`.

### Changed
- Engine prompts made portable (audit 1.32): `install.py place_engine` now RENDERS each skill's `SKILL.md` and the `agents/amg-*.md` prompts (for Codex, the TOML subagent bodies) via `render_control_text`, like the entry templates — previously they were copied verbatim, leaving wrong `.claude/...` command paths under any other agent dir and relative script paths under a global install.
- `resolve_amg_root` (`graph_store.py`, `retrieve.py`) probes both agent-dir presets `.claude` and `.agents` in its upward config search, so a non-`.claude` project resolves.
- `eval_gate.cases` is portable: `consolidate.load_config` derives the default from the store root; `install.write_config` renders the shipped value to the agent dir for a non-`.claude` install.
- Docs synced to the implemented mechanism: `09-config.md` (the `models` block is now a working setting — structured form, `reasoning_effort`, clamp, the #44385 caveat; the planned `models` entry removed), `08-agents-skills.md` (the 3 env modes + prompt rendering), `GUIDE.md` (model selection → `reasoning_effort`; the installation section reworked into the full guide form; the env note), README_RU + README (the Installation section reworked: deps → install ways → config-as-code → other environments; the portability note moved into Installation; a "what gets ignored" line). Roadmap §1 items 1.14 / 1.16 / 1.28 / 1.32 collapsed; every open §1 item attached to a stage.

### Notes
- Codex (`--env codex`) and the skill-less generic mode remain **untested** on a live non-Claude-Code environment — verification is roadmap Stage 19. The Claude Code path is unchanged in behavior. Upstream Claude Code bug [#44385](https://github.com/anthropics/claude-code/issues/44385): a subagent's frontmatter `model:` is honored only when passed explicitly at spawn (the `effort` field works); this is documented, with the `CLAUDE_CODE_SUBAGENT_MODEL` / per-call workaround.

## [1.0.0] — 2026-06-16

Stage 10 closed — **v1.0.0**: AMG installs locally or globally with no manual file copying, the engine is portable across the agent directory, and what enters the graph is fully controllable per intent. This release fixes a stable data schema and a working install (under SemVer, breaking the data contract without a migration would now be a MAJOR bump).

### Added
- `install.py` — the installer (successor to `sync_testbed.py`, which stays for the dev sandbox). Config-driven: the model conducts the Q&A and calls it with the answers. It copies only the `amg-*` skills/agents (a shared `~/.claude` keeps the user's other skills), renders `entrypoint/CLAUDE.md` / `settings.json` / `commands/amg.md` per `agent_dir`/`entrypoint` (`render_control_text`; global → an absolute engine path, the `@digest` import → a loop note, the graph stays local), injects the block between `<!-- AMG:BEGIN/END -->` (reinstall replaces only the block; the `# Project memory` preamble is stripped), merges `settings.json` (the user's hooks kept), writes `config.yml` from the template (editing real keys, not prose comments; never clobbering an existing config), installs dependency groups, and finishes with `verify --repair` — never an auto-build.
- Installer options: `--build` (build the structural graph during install, ready this session), `--project-only` (add a project to a global install — local config only), `--uninstall [--scope global] [--purge-graph]` (strips the block, removes only `amg-*` and the AMG hooks, keeps the graph unless purged). `requirements.txt` with `base` / `embeddings` / `text` / `treesitter` groups.
- Environment adaptation (`--env`): for Claude Code (the default) the installer lays down the skill/hook/command block; for any other agent environment that reads `AGENTS.md` (Codex, Qwen Coder, ...) `--env generic` injects a separate **skill-less** block (`entrypoint/AGENTS.md`) — the same memory loop via direct script calls, with the `agents/*.md` prompts read as guidance and no hooks or `/amg` command. The default `claude-code` path is byte-identical to before. NOTE: the skill-less path is **untested and not guaranteed stable** on non-Claude-Code environments — verification is roadmap Stage 19.
- Per-intent ignore controls: `mirror_exclude` / `absorb_exclude` config keys (additive over the global `exclude`), `respect_gitignore` (default true; false makes ignoring fully config-driven, no git needed), and an explicitly configured source whose root is gitignored is still ingested (`_gitignore_for_source` — `absorb_path: logs` with `logs/` in `.gitignore` no longer vanishes). `--stats` reports per-source file counts (`by_source`), so a silently-filtered or missing source is visible.
- `selftest_ignore.py` (5 cases) and `selftest_install.py` (9 cases, headless — including the skill-less `--env` path). 13 selftests green.

### Changed
- Environment parameterization (roadmap 4.9 completed): `DEFAULT_IGNORE_DIRS` gains `.agents` and the resolved agent dir (`_effective_ignore_dirs`), so the engine never indexes its own directory under any name. `graph_store.main`, `retrieve._default_store` and `inspect_graph` now resolve the store via `resolve_amg_root` (upward config search), so a global engine heals and reads a project's LOCAL graph from the project cwd; the dead `graph_store._default_root` is removed.
- Docs synced and the 2.15 sweep done: `INSTALL.md` rewritten for the installer; README_RU + README Quick Start reworked (install → `/amg status` → just ask the model; `/amg on` ≠ index); GUIDE, `04-ingest` (the ignore section), `09-config` (new keys + `agent_dir`/`entrypoint` made real), `08-agents-skills` (the control-plane install realized), `03-storage`/`06-retrieval` (CLI root resolution), THEORY §11; a "`.claude`/`CLAUDE.md` are Claude Code defaults" caveat across the docs. Roadmap §5 Stage 10 folded; section-2 items 2.13 / 2.15 closed, 2.16 item 5 closed.
- `config.yml`: `respect_gitignore`, `mirror_exclude`, `absorb_exclude`, and the documented `agent_dir` / `entrypoint` keys.

### Fixed
- A global install would have resolved the wrong (global) graph root from the activation block's direct `graph_store.py` calls — it now resolves the project's local graph via the upward config search (the global-install DoD).
- `place_engine` no longer deletes the whole `skills/` directory — it replaces only `amg-*`, so a shared agent dir keeps the user's other skills across install and reinstall.
- A configured source whose root is gitignored (e.g. `absorb_path: logs`) was silently dropped from the graph; it is now ingested — explicit intent beats the generic ignore convention.

## [0.11.0] — 2026-06-16

Stage 9 closed: valuable dialogues are no longer lost on `/clear`. At session end the transcript is dumped to `<store>/sessions` and ingested like any other source; a hard kill that skips `SessionEnd` is now surfaced (not silent) on the next start.

### Added
- Session capture: `lifecycle.py session-end` reads the `SessionEnd` hook's stdin (`transcript_path`/`reason`) or `--transcript`, parses the Claude Code `.jsonl`, and writes a role-marked markdown dump to `<store>/sessions/YYYY-MM-DD-HHMM.md` — human/assistant text kept, raw `thinking` cut, each tool call/result/image marked individually as `== Attachment N: <kind> ==` (numbered, so several attachments in one message stay distinct), and meta / slash-command / `!`-bash / task-notification wrappers filtered (enumerated from 49 real transcripts). The `.jsonl` parser is isolated as a Claude-Code adapter.
- Session chunker `_session_units` (`extract_structure`, `CHUNKERS`): one `section` unit per turn (`doc:path::m{n}`), sharing the role-marker format with the writer (`session_role_marker` / `session_attachment_marker`); `section` makes turns episodic so consolidation compacts piled-up chat. The sessions dir DERIVES as `<store>/sessions` (portable across any agent dir); `sessions` is an optional override, `session_policy` (`absorb` / `mirror`, default `absorb`).
- Unclean-shutdown note (task 9): `session-start` / `repair` detect a healed crash — replayed transactions or a cleared stale lock, via a read-only probe taken BEFORE the lock — and report it in plain words; `session-start` prints only when something was healed (a clean start stays silent).
- `selftest_sessions.py` (chunk + 1.18 ignore-fix + non-`.claude` portability + dump round-trip); `selftest_lifecycle.py` gains heal-note and unclean-shutdown cases.
- `config.yml`: `session_policy`.

### Changed
- Docs synced to the implemented code: `04-ingest.md` (the session chunker + the 1.18 ignore exemption), `02-data-model.md` (session id-form row + `sessions/`), `05-reconcile.md` (sessions consumed as ordinary mirror/absorb), `08-agents-skills.md` (the SessionEnd dump and the unclean-shutdown note; planned item closed), `09-config.md` (`sessions`/`session_policy` now live), `THEORY.md` §13 (sessions as the broad episodic capture channel; capture-vs-dump by portability — new rationale), `GUIDE.md`/`INSTALL.md`/the activation block, and README_RU + README (a "Saving sessions" section). Roadmap §5 Stage 9 folded; section-2 items 2.9 п.2 / 2.12 / 2.13 п.6 / 2.16 п.4 closed.

### Fixed
- Audit 1.18: a source inside the store (the sessions dir, under the ignored agent dir and often in `.gitignore`) was silently dropped by `iter_source_files`; it is now walked by a dedicated `_iter_session_files` that bypasses the ignore prefix while normal sources keep full ignore semantics (anchored `.gitignore` rules still apply). Eleven selftests green.

## [0.10.0] — 2026-06-15

Stage 8 closed: AMG is driven both automatically and manually with no implicit behavior. A thin `lifecycle.py` orchestrator carries the Claude Code session hooks and the `/amg` commands, and consolidation emits an always-on digest that the entry point imports every session — so the memory surfaces even before any retrieval.

### Added
- `lifecycle.py` (new, in `skills/amg-bootstrap/scripts/`): the control plane. Hook entrypoints `session-start` (recover + verify --repair + refresh digest) and `session-end` (fold weights + refresh digest), self-gated on `active` + `automation`; manual commands `status` / `repair` / `on` / `off`. `status` gathers every field — active, automation, graph root, node/stale counts, pending transactions, stale lock, queue size, last pack, last consolidation, eval summary — without reading files. It carries no graph logic of its own: it calls `graph_store` (healing) and `consolidate` (weights, digest).
- Always-on digest: `consolidate.write_digest` + the `digest` command write `<amg>/digest.md` — the most salient standing decisions and open questions (`decision`/`adr`/`open_question`, active/captured, ranked by salience, up to 6 + 4). The entry point imports it via `@.claude/amg/digest.md` every session (insurance against the loop's main failure: memory that exists but is never retrieved). Refreshed by `weights`/`apply` and by both hooks.
- `entrypoint/settings.json` — the `SessionStart`/`SessionEnd` hooks. `entrypoint/commands/amg.md` — `/amg` as one front door: control verbs (`status`/`on`/`off`/`repair`) run `lifecycle.py`, work verbs (`sync`/`retrieve`/`consolidate`) delegate to the amg-bootstrap/retrieve/consolidate skills; `disable-model-invocation` (user-only), synonym-tolerant.
- `selftest_lifecycle.py` — digest selection/placeholder, automation gate, hook heal + weight fold, status fields, on/off in-place flip.
- `config.yml`: `automation` (boolean, like `active`).

### Changed
- The activation block (`entrypoint/CLAUDE.md`) is now env-aware: the digest import, a full operations table (`/amg` verb + skill + verbal intent, with synonyms), `automation` behavior, SessionStart/SessionEnd hook notes, and a caveat that `.claude`/`CLAUDE.md` are Claude Code defaults and that hooks/commands/import are Claude-Code-specific, with the model loop as the portable substitute.
- `sync_testbed.py` also lays down `settings.json` + `commands/` and seeds an empty `digest.md` so the import resolves before the first consolidation.
- Docs synced to the implemented layer: `08-agents-skills.md` (the "Слой жизненного цикла" section, in depth), `09-config.md` (`automation` live), `02-data-model.md` (`digest.md` in the layout), `GUIDE.md`, `THEORY.md` §13 (the digest as a passive memory channel — new rationale), and README/README_RU (mirrored EN). The roadmap §5 Stage 8 is folded; the portability "deeper point" (hooks/slash-commands/@import are Claude-Code-specific; the portable substrate is the model loop + verbal triggers + direct calls; the Stage 10 installer both renders paths and deploys the env-appropriate mechanism) is recorded across 4.9, Stage 10 task 8, 2.13/2.15/2.16, 08, GUIDE, both READMEs, and the activation block. The Stage 8 control-plane artifacts are registered for the Stage 10 installer.

### Fixed
- Closes section-2 items 2.9 п.6, 2.12 п.1, 2.16 п.3: hooks, `/amg` commands, `automation`, and the digest were forward-written; they are now realized and the docs are synced to the code. Ten selftests green.

## [0.9.0] — 2026-06-15

Stage 7 closed: the documentation is synced once with the implemented stage 0–6 tract; forward descriptions of future features are kept and bound to their stages. Showcase READMEs (RU + EN) are written, and the Russian docs are cleaned of calques per roadmap section 3.

### Added
- `README_RU.md` and the English `README.md` (mirror; links to `docs/en/*` filled at stage 18): what AMG is (best of RAG + Karpathy's llm-wiki), two planes, graph-over-tree, per-request PPR retrieval, tier budgets + branch-compaction/forgetting, capture→consolidate, crash safety, conservative defaults, local/global install + `CLAUDE.md` entry point (with the `.claude`/`.agents` environment caveat), the semi-manual quick-start, mirror/absorb (+optional, +mirror-trick), modes & `/amg` commands, optional deps.
- `selftest_graph_store.case_documented_layout` pins "documented layout == `graph_store.init()`" (the five buckets, no phantom `index.md`/`graph.json`, on-demand dirs absent).

### Changed
- Section-3 language sweep across all docs, grammar-correct (gender/case agreement) with code identifiers preserved: `деривация`→`семантическое обогащение` (05 retitled, English gloss at first mention), `лок`→`блокировка`, `крэш-безопасность`→`устойчивость к сбоям`, `харнесс`→`измерительный стенд`, `хеш-гейт`→`фильтр по хешу`, `сырьё`→`материал`, `фронтматтер`→`frontmatter`, and the rest of the section-3 list.
- Architecture 01–10 synced to verified code: `cache/` + `log.md` in the layout; honest selftest claim; RST/NDJSON fall back to one file unit (parsers → stage 11); precise hashing (function/class/module slices); `--no-pack` also disables the co-activation log (1.28); `models` is declarative (1.14); `--make-demo` creates a demo graph; the eval-gate is implemented (removed from the unimplemented list).
- `consistency-model.md` (read by the model during bootstrap): phantom `index.md`/`graph.json` removed from §4/§6/§7/§12 and the `graph_store.py` docstring; layout `code/ doc/ data/ notes/ _hubs/`; node class `derived`→`synthesized`; absorb survives via `policy`, not `source_kind`; §11 logging is best-effort outside the journal (1.15); id prefix `docs:`→`doc:`.
- THEORY/GUIDE/INSTALL: `absorb` survives source deletion (not "once & frozen"); mirror move/rename keeps the earned trail; weights "not learned by gradient descent"; the decision-log is marked planned; GUIDE gains the `gap-report.md` description and stage-bindings for forward sections (`/amg`+automation→8, sessions→9, 3D→15, structured models→1.14).

### Fixed
- All relative links verified across docs; zero residual calques; nine selftests green.

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
