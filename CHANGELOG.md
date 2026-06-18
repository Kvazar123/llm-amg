# Changelog

All notable changes to AMG are documented in this file. Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer in pre-1.0 mode (rules: CLAUDE.md §10, roadmap §5 granularity rule).

## [Unreleased]

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
