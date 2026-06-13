# Changelog

All notable changes to AMG are documented in this file. Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer in pre-1.0 mode (rules: CLAUDE.md §10, roadmap §5 granularity rule).

## [Unreleased]

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
