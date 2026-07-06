# 09 — Configuration reference

All of the system's configuration lives in the `config.yml` file (the control plane, kept in English). Its presence with `active: true` turns AMG on for a project; it sits at `.claude/amg/config.yml` (`.claude` is the Claude Code default for the agent directory; in another environment it is the configured name, e.g. `.agents`, see the `agent_dir` key). This section lists every key literally, with its default and its meaning; the mechanics they configure are described in the per-module sections (links along the way).

## Two layers: the global and the local config

Configuration is read in two layers. The **local** project `config.yml` is the primary and mandatory one: it turns the memory on and carries the project settings. The **global** `~/<agent dir>/amg/config.yml` is an optional layer of **one machine's personal defaults**, created by a global install: the loaders (`retrieve` / `consolidate` / `extract_structure`) read it first, then the local one, and the local **overrides key by key** (nested blocks merge recursively; whatever is missing is inherited).

The layers are split by what a setting belongs to. The global one holds the personal — what must not be imposed on a team through the project's git canon: the `models` block (model tiering) and the `retrieval.embeddings` block (the backend and seeding switch — a property of the machine where the dependencies are installed). Everything project-level — `active`, the sources, `working_language`, `automation`, the budgets, the weights, compaction — stays local, and the team gets it identically from git. Under a global install the local config is written **without** the personal blocks (a full template would shadow the global layer with its every key); under a local install it is self-sufficient, and no global layer is created.

Inheritance is keyed on the **local** config's `agent_dir` (the installer always writes it): it names the environment's home directory — `.claude`, `.agents`, and a custom name each have their own layer — and simultaneously marks the config as installed; a minimal hand-written config without `agent_dir` does not read the global layer. The `~/<agent dir>/amg/` directory carries one file and is **not a store**: store resolution never picks it (see [Storage](./03-storage.md)). How the installer writes the layers — [12-install](./12-install.md).

## Location and overriding

The scripts carry their own defaults (`DEFAULTS`), and `config.yml` (merged from the two layers, above) **overrides** them — block-wise. The merge nuances matter:

- **Retrieval** (`retrieve.py`): the `retrieval` block overlays the defaults **key by key and recursively** (`_deep_merge`): the nested blocks `relation_priors`, `token_budget`, `status_prior`, `embeddings` merge by individual keys, so a partial block **loses no defaults** — an edge type not named in `config.yml` keeps its built-in prior instead of dropping to `relation_prior_default`. The built-in code defaults differ from the template in places (`token_budget` in code is 1200/2500/6000/40), but the `config.yml` values **win** — what is in force is what is in the file.
- **Consolidation** (`consolidate.py`): the given keys are taken from the `weights` block, and the `compaction` block merges by individual keys. The same "code ≠ template" difference: the built-in branch budgets in code are `default_branch_budget_nodes` 150 / `default_branch_budget_tokens` 60000, while the `config.yml` template sets 400 / 200000 (the template's values are the ones in force). The `near_duplicate_sim`, `episodic_types`, and `stale_age_days` parameters are **read from the config** (top-level keys) and present in the template; absent, the script defaults apply (0.82, `[section, note, open_question, plan]`, 30).
- **Reconciliation** (`reconcile.py`): the `trivial_unit_max_lines` key is the third "code ≠ template" case: the built-in code default is `0` (the auto-summary of trivial units is off — a minimal hand-written config behaves as before), while the shipped template enables `3` (see "The queue and build batches" below).

**Disposable caches are not configurable.** The generated read-index `cache/index.sqlite` (speeds up `load_nodes` on large graphs), the embedding cache `cache/embeddings.json`, and the last pack `cache/pack.md` have no config keys: they are automatic and disposable, rebuilt as needed ("broken — delete it"). The exception is the **derivation cache** `cache/derivations/`: it changes rebuild behavior (restoring summaries instead of regenerating), so it has the `derivation_cache` switch (below). More — [Retrieval](./06-retrieval.md) and [Reconciliation](./05-reconcile.md).

## Activation and basics

| Key | Value | Meaning |
|---|---|---|
| `active` | `true` | the master switch: `true` turns AMG on for the project; a missing file or `false` turns it off (see [Subagents and skills](./08-agents-skills.md), the activation loop). Toggled by `/amg on`/`off` |
| `automation` | `true` | lifecycle automation (boolean, like `active`): `true` — the `SessionStart`/`SessionEnd` hooks heal the graph, fold the weights, and refresh the digest, and the model runs the loop itself; `false` — nothing happens automatically, only the `/amg …` commands, skills, and explicit requests work (see [Subagents and skills](./08-agents-skills.md), the lifecycle layer). The hooks and `/amg` live in the agent directory's `settings.json`/`commands/`, not in this file |
| `schema_version` | `3` | the configuration schema version |
| `working_language` | `ru` | the language of document summaries and notes (ISO 639-1); structural metadata, edge type names, and code identifiers stay English (see [The big picture](./01-overview.md), the two planes) |
| `agent_dir` | `.claude` | the engine and graph directory; `.claude` is the Claude Code default, other environments have their own name (e.g. `.agents`). Written by the installer; it records the choice declaratively (the graph root is found by the location of `amg/config.yml` via the `resolve_amg_root` chain — see [Storage](./03-storage.md)) and **enables global-layer inheritance** (see "Two layers" above): without this key the global config is not read. For other environments the installer substitutes the name per the `--env` flag: `codex` (skill-aware — skills + TOML subagents in `.codex/agents`) or `generic` (the portable skill-less block); see [Subagents and skills](./08-agents-skills.md), "Portability". Modes other than Claude Code are not yet tested — environment verification is ahead on the roadmap |
| `entrypoint` | `CLAUDE.md` | the entry-point file into which the installer injects the activation block between the `<!-- AMG:BEGIN/END -->` markers; `CLAUDE.md` is the Claude Code default, other environments have their own name (e.g. `AGENTS.md`) |

## Sources

| Key | Value | Meaning |
|---|---|---|
| `mirror_path` | a string or a list | the folders the graph is a **live projection** of: file added → a node, changed → updated, deleted → purged. For what you edit (code, maintained guides) |
| `absorb_path` | a string or a list | the folders **ingested once** into independent nodes: deleting the source later does not purge the node, but while the source is on disk its changes are re-reconciled. For one-off material (chat logs, data dumps) |
| `absorb_once_path` | a string or a list | the folders **ingested once and frozen**: like `absorb` (deleting the source does not purge the node), but subsequent source **changes** are ignored too — the node is never rebuilt. For a one-off snapshot that must not re-sync even when the original is edited. On an overlap with `mirror`/`absorb` it wins as the most preserving policy |
| `exclude` | a list of glob patterns | extra exclusions **on top of** the built-in defaults (the agent directory, `.git`, `node_modules`, `.venv`, `dist`, `build`, binaries, etc.) and `.gitignore`; applied to **all** sources. Usually empty |
| `mirror_exclude` | a list of glob patterns | the same, but only for mirrors (`mirror_path`); **stacks** with the global `exclude` |
| `absorb_exclude` | a list of glob patterns | the same, but only for absorbed sources (`absorb_path`); stacks with `exclude` |
| `respect_gitignore` | `true` | whether to read the root `.gitignore` (as a plain file — no git needed; the rules apply in line order, the last match wins, `!` re-include rules work). With `false`, `.gitignore` is ignored and the graph's composition is set by the config alone |
| `session_policy` | `absorb` \| `mirror` | the policy for dialogue auto-dumps: `absorb` (default) — the dump may be deleted, the distillate stays; `mirror` — the nodes live as long as the dump file does |
| `sessions` | a path (opt.) | the session auto-dump directory; by default **computed** as `<store>/sessions` and correct under any agent directory — the key exists only to override |

Each of the paths is a string or a YAML list; any may be omitted. What the `mirror`/`absorb` policies mean — the [theory](../THEORY.md) (section 12); reconciliation behavior on change and deletion — [Reconciliation and semantic derivation](./05-reconcile.md). PDF/DOCX/XLSX extraction needs the pure-Python libraries installed (`pip install pypdf python-docx openpyxl`); without them such files are skipped (see [Structure extraction](./04-ingest.md)).

For backward compatibility the legacy source form is also supported — the dictionary `sources: {name: {path, policy}}` (`resolve_sources` reads it only when none of the `mirror_path`/`absorb_path`/`absorb_once_path` keys is set). Use the three keys above in new configurations; the template does not carry the legacy form.

Ignoring stacks from three layers (the built-in directory list → `.gitignore` → these config keys) and never requires git. An important guarantee against silent loss: **an explicitly named source beats `.gitignore`** — if the root of a `mirror_path`/`absorb_path` is itself listed in `.gitignore` (typical for `absorb_path: logs`), it is indexed anyway, while `.gitignore` still trims the junk inside broad sources. The mechanics of all the layers — [Structure extraction](./04-ingest.md), "Ignoring".

If one file falls under **two roots at once** (a `mirror_path`/`absorb_path`/`absorb_once_path` overlap), the most preserving policy wins — `absorb_once` outranks `absorb`, which outranks `mirror` — and the conflict itself is visible in the `plan` output (the `policy_conflicts` field) and in `--stats` (`overlapping_sources` with a hint), i.e. it is not resolved silently.

Saved sessions (the dialogue dump on `SessionEnd`, [Subagents and skills](./08-agents-skills.md)) are a separate internal source inside the store; it is ingested by the same path as the mirrors but walked without the prefix ignore (see [Structure extraction](./04-ingest.md), "Sessions"). In an environment without the `SessionEnd` hook there is no auto-dump — the portable "the dialogue is not lost" insurance is note capture along the way (`notes.py`).

## Input chunking

The numeric limits of the structured- and unstructured-data chunkers (the mechanics — [Structure extraction](./04-ingest.md)). All keys are optional; absent, the shown defaults apply.

| Key | Value | Meaning |
|---|---|---|
| `json_max_depth` | 4 | how deep the recursive JSON chunker descends into nested containers |
| `json_recurse_min_chars` | 2048 | descend into a container only if its serialized JSON is longer than this threshold; small and flat values stay one record (with the old ids and hashes) |
| `json_max_nodes` | 500 | the cap on record units from one JSON or NDJSON file |
| `log_group_lines` | 50 | how many log (`.log`) lines are grouped into one episode block |

## The queue and build batches

The economical-build keys: how much text travels into the work queue, which units are derived without a model, and how big one builder batch is (the mechanics — [Reconciliation](./05-reconcile.md), "The derivation queue" and "Auto-summary of trivial units"; the orchestration — [Subagents and skills](./08-agents-skills.md)).

| Key | Value | Meaning |
|---|---|---|
| `queue_text_max_chars` | 20000 | the ceiling (in characters) on a unit's text embedded into a `work/queue.json` item: the builder writes the summary straight from the queue and never re-reads sources; text over the threshold is not embedded — such an item carries only the pointer. `0` — a fully text-less queue (as before the key existed) |
| `trivial_unit_max_lines` | 3 (template) | a code function whose definition fits within this many lines is derived by the **deterministic auto-summary** (its own code as one line) with no model call and no queue; protocol dunders (`__call__`, `__enter__`, `__getitem__`, …) always go to the model, and a unit with a derivation-cache hit restores its earned summary (the cache beats the template). Another "code ≠ template" case: the built-in code default is `0` (off) — the shipped template is what enables the savings |
| `builder.batch_units` | 40 | units per builder batch (`partition_queue.py` splits a subtree group into `queue-<batch>-NN.json` parts) |
| `builder.batch_max_chars` | 120000 | the estimated input volume of one batch — the sum of the embedded texts (a unit without text counts as a 2000 nominal); it bounds one builder's context and, indirectly, its output — symmetric to the linker's `linker.batch_nodes` |

## Subagent models

The `models` block distributes model strength across the roles — the strongest only on the foundational synthesis. It is **operational**: the installer renders each role's choice into the corresponding subagent definitions at install and reinstall. `config.yml` is the single source of truth, and the subagent definitions carry only the rendered result.

| Key | Value | Role → subagents |
|---|---|---|
| `models.structural_extraction` | `none` | structure extraction is deterministic, no model (there is no subagent) |
| `models.discovery` | `{haiku, low}` | light read-only tasks → `amg-classifier`, `amg-retriever` |
| `models.module_summary` | `sonnet` (the environment's default effort) | bulk work with a bounded context: per-unit summaries → `amg-builder`; confirming linking candidates → `amg-linker` (the linking's strength is in the global candidates, not the model size) |
| `models.synthesis` | `{opus, high}` | the architecture overview, cross-layer edges, gaps → `amg-synth`, `amg-consolidator` |

**The structured form.** A role's value is either a flat model string or a `{model, reasoning_effort}` mapping (backward-compatible: a flat string keeps working):

```
models:
  synthesis: {model: opus, reasoning_effort: high}
```

- **`model`** — an opaque string, passed through as is: a Claude family alias (`opus`/`sonnet`/`haiku`/`fable`), an exact id (`claude-opus-4-8`), or another provider's id (e.g. `gpt-5.5`) when Claude Code is pointed at a gateway/Bedrock/Vertex. AMG only **passes the string along**; routing to a provider is the deployment's job (the `ANTHROPIC_BASE_URL`/Bedrock level), not the model name's — no custom version micro-format is introduced.
- **`reasoning_effort`** — the environment-neutral reasoning effort: `minimal | low | medium | high | xhigh | max`. **Clamped per environment**: in Claude Code it renders into the `effort` field (`low|medium|high|xhigh|max`; `minimal` maps to `low`), in Codex into `model_reasoning_effort` (`minimal|low|medium|high|xhigh`; `max` maps to `xhigh`). If unset, the field is **omitted** (the environment's default applies; Claude Code's is `high`). The model and the effort are separate parameters; the effort is never baked into the model name.

**How it is applied.** The installer (`install.py`) reads `models` and writes the result into the subagent definitions: for Claude Code — the `model`/`effort` fields in the `agents/amg-*.md` frontmatter; for Codex — `model`/`model_reasoning_effort` in the `.codex/agents/amg-*.toml` TOML (see [Install](../../INSTALL.md)). In skill-less environments all roles are played by one model instance, so per-role tiering is inert there.

> **A known Claude Code caveat.** The `model:` field in subagent frontmatter is currently ignored unless the model is passed explicitly at launch ([issue #44385](https://github.com/anthropics/claude-code/issues/44385)); the `effort` field does apply. AMG renders `model:` anyway, as the documented future-proof surface; to force a model today — pass it when invoking the subagent, or set the `CLAUDE_CODE_SUBAGENT_MODEL` variable.

**The default tiering and the measurement protocol.** The template sets a reasoning-effort gradient: `low` on the simple read-only roles (`discovery` — file-type classification, pack assembly), full effort on the foundational synthesis (`synthesis` — an explicit `high`). `module_summary` is deliberately left **flat** (the environment's default effort): its per-unit summaries feed retrieval (BM25 + embeddings), so the builder's effort is not cut without a measurement. Tiering is tuned **by a number, not by eye** — the same "quality first" principle as with the Hebbian rule (§8.1 of the theory) and compaction:

1. take a baseline — build the graph and run `eval_retrieval.py --cases …` on the current tier;
2. lower one tier (the model and/or `reasoning_effort`), **rebuild** the affected summaries/hubs (re-derivation via `reconcile.py bootstrap` + the builders), and run the same eval;
3. keep the cheap tier where `recall`/`hop_recall` hold; restore the old one where they dropped — "a cheap model only where the eval does not sag; never cut reasoning effort where quality falls".

The measurement requires paid model calls (rebuilding the graph on another tier), so the **live measurement is deferred** (like the productization of the Hebbian rule and the non-Claude-Code environment checks): the template ships sensible defaults and the method, and the optimal tier for a specific graph is found by measurement. `discovery: low` is safe by construction (mechanical tasks); the under-tiering risk sits with `module_summary`, which is why it stays at full effort.

The `models` block is a typical resident of the **global** configuration layer (see "Two layers" above): tiering is a machine's personal preference, not the project's, and under a global install it lives in `~/<agent dir>/amg/config.yml`, inherited by every project; a local `models` block, if set, overrides it per role. The installer renders the subagents from the merged view of both layers.

The role-to-subagent mapping — in [Subagents and skills](./08-agents-skills.md).

## The derivation strategy (`derivation`)

The top-level `derivation` key (`eager` | `lazy`, default **`eager`**) sets how much of the semantic layer is built at `bootstrap`:

| Value | Behavior |
|---|---|
| `eager` (default) | derive the summary + semantic edges for **every** queued unit right at build time |
| `lazy` | derive only the structural **map** at once (module/class/package/file + the synthesis hubs), deferring the leaf detail (functions, document sections, records) until a query first touches it; an activated deferred node is derived **synchronously before the answer** (the first touch is never empty), and a background pass fills the rest by `usage.log` |

`lazy` trades the upfront cost for a quality dip on not-yet-derived nodes (the [roadmap, §4.10](./11-roadmap.md)) and pays off **only** on a graph much larger than what is actually queried; on a small graph keep `eager`. The default stays `eager` until a measurement on a large graph confirms the trade (an off/on protocol) — an explicit opt-in, not an automatic size-based mode. **Safeguards:** the structural skeleton and strategic synthesis are never deferred; the first touch of an activated node is synchronous (`retrieve.stale_in_pack` → the builder before the answer); deferral loses nothing (the source and the structural node are in place). The off/on measurement (first-touch keeps multi-hop reach; bare laziness tears it) — §4.10 and `../amg-bigtest/README.md`. The mode's branching lives at the level of the `amg-bootstrap`/`amg-retrieve` skills and the `partition_queue.py --priority` helper; the engine itself does not branch on the flag.

## The derivation cache (`derivation_cache`)

The top-level `derivation_cache` key (boolean, default **`true`**) enables the persistent cache of applied derivations (`cache/derivations/`, keyed by the unit content hash + the contract version + `working_language`): a rebuild restores summaries and edges verbatim and nearly for free, and `bootstrap` does it automatically (the `restored_from_cache` field in the summary). `false` — derive afresh (e.g. after switching models); deleting the directory has the same effect. The mechanics — [Reconciliation](./05-reconcile.md), "The derivation cache"; the rationale — [theory, §4.3](../THEORY.md).

## Global linking (`linker`)

The settings of the deterministic preparation of the global semantic pass (`link_candidates.py`; the mechanics — [Reconciliation](./05-reconcile.md) and [Subagents and skills](./08-agents-skills.md)):

| Key | Value | Meaning |
|---|---|---|
| `linker.top_k` | 5 | how many candidate neighbors are nominated per node |
| `linker.min_sim` | 0.35 | the lower cosine-similarity bound for nomination (the embeddings mode; the lexical fallback uses its own threshold — ≥2 shared informative tokens) |
| `linker.batch_nodes` | 40 | nodes per `work/link-batch-*.json` batch — bounds one `amg-linker` instance's context |

## The connectivity acceptance gate (`connectivity_gate`)

The thresholds of the `reconcile.py metrics` verdict (also the `connectivity` block in `/amg status`; the mechanics and metric set — [Reconciliation](./05-reconcile.md), "Connectivity metrics"). The verdict is advisory (`ok` / `attention`) — it never blocks the build, only suggests the next step (usually rerunning the linking):

| Key | Value | Meaning |
|---|---|---|
| `connectivity_gate.min_largest_share` | 0.9 | the largest connected component must hold at least this share of the nodes |
| `connectivity_gate.max_dangling_internal` | 0 | the allowed number of unresolved **internal** edge targets (external `imports` to stdlib/third-party are legitimate and not counted) |

## Retrieval (`retrieval`)

The spreading-activation parameters (the mechanics — [Retrieval](./06-retrieval.md)):

| Key | Value | Meaning |
|---|---|---|
| `retrieval.damping` | 0.85 | how far activation spreads |
| `retrieval.max_hops` | 30 | the power-method iteration ceiling |
| `retrieval.activation_threshold` | 0.02 | the cutoff below this final activation |
| `retrieval.token_budget.strategic` | 4000 | the hubs/overviews tier budget |
| `retrieval.token_budget.tactical` | 10000 | the modules tier budget |
| `retrieval.token_budget.operational` | 24000 | the budget of the code-pointer and in-focus document/note-body tier |
| `retrieval.token_budget.periphery_links` | 60 | the cap on periphery links |
| `retrieval.relation_priors` | a dictionary | the conductance prior `β` by edge type: `documents`/`implements`/`specifies` 0.9, `calls`/`depends_on`/`inherits` 0.8, `defines`/`part_of` 0.7, `imports`/`refines`/`exemplifies` 0.6, `relates_to` 0.5, `follows` 0.4 (chat-turn adjacency — weaker than semantic links), `supersedes`/`contradicts` 0.3 |
| `retrieval.relation_prior_default` | 0.5 | the prior for types not listed above |
| `retrieval.status_prior` | a dictionary | the final-activation multiplier by node status: `active`/`stale` 1.0, `superseded` 0.2, `disputed` 0.5, `rejected` 0.1 (arbitration verdicts); `stale` is not penalized but flagged in the pack; a query with `--intent history|conflict` lifts the retired-status demotion (see [Retrieval](./06-retrieval.md)) |
| `retrieval.convergence_tol` | 1e-6 | the power-method convergence threshold (the sum of absolute changes) |
| `retrieval.seed_floor` | 0.0 | base mass for every node (0 = pure relevance) |
| `retrieval.embeddings.enabled` | `auto` | `auto` (enable if a backend is installed) / `on` / `off`. Another "code ≠ template" case: the built-in code default is `auto`, but the `config.yml` template conservatively ships `off`, so seeding is off in a fresh install; the install flow, on consent to embeddings, sets `auto` (`--set retrieval.embeddings.enabled=auto`; under a global install — in the global layer) |
| `retrieval.embeddings.backend` | `auto` | `auto` / `model2vec` / `sentence-transformers` |
| `retrieval.embeddings.model` | `""` | `""` = the backend's default by `working_language`: en → model2vec `potion-retrieval-32M` / st `all-MiniLM-L6-v2`; non-en → `potion-multilingual-128M` / `paraphrase-multilingual-MiniLM-L12-v2`. Any HF id overrides (an upgrade: `Alibaba-NLP/gte-multilingual-base`) |
| `retrieval.embeddings.blend` | 0.5 | 0 = pure BM25 … 1 = pure semantics |

## Weights (`weights`)

The weight-upkeep parameters at consolidation (the mechanics — [Consolidation](./07-consolidation.md)):

| Key | Value | Meaning |
|---|---|---|
| `weights.apply_hebbian` | `false` | enable weight (`w`) updates (Hebbian + decay + pruning); with the default `false` consolidation only accumulates `coact`, conductance stays static (the rationale — the [theory](../THEORY.md) §8.1) |
| `weights.default_edge_weight` | 0.5 | the starting weight of an edge with no explicit `w`; an **operational** key — read by `reconcile._merge_edges` (new semantic edges), `retrieve.build_adjacency` (conductance), and `consolidate.fold_weights` |
| `weights.hebbian_rate` | 0.10 | the Hebbian reinforcement strength (active under `apply_hebbian`) |
| `weights.decay_rate` | 0.02 | passive fading (active under `apply_hebbian` with a co-activation log) |
| `weights.prune_below` | 0.05 | the pruning threshold for weakened edges (active under `apply_hebbian`) |
| `weights.part_of_renormalize` | `true` | keep membership shares summing to ≤ 1 (always, separate from `apply_hebbian`) |

## Compaction (`compaction`)

The branch-compaction parameters (the mechanics — [Consolidation](./07-consolidation.md)):

| Key | Value | Meaning |
|---|---|---|
| `compaction.enabled` | `true` | an **operational** switch: with `false` the plan flags no branches and `apply` skips compaction actions absent a `force`. With `true` compaction still fires only on budget overflow (with generous budgets the graph stays uncompressed) |
| `compaction.default_branch_budget_nodes` | 400 | the branch budget in nodes |
| `compaction.default_branch_budget_tokens` | 200000 | the branch budget in tokens |
| `compaction.protect_types` | `[decision, adr]` | the types never compressed first |
| `compaction.protect_min_centrality` | 0.7 | the centrality threshold for node protection |
| `compaction.archive_dir` | `archive` | the archive directory for originals |
| `compaction.steps` | `[summarize_episodes, merge_near_duplicates, introduce_subhub, lossy_shorten]` | the staged compaction order |

## Consolidation plan settings (top level)

The plan-annotation parameters — top-level configuration keys (read by `consolidate.load_config`, present in the template; absent, the script defaults apply):

| Key | Value | Meaning |
|---|---|---|
| `near_duplicate_sim` | 0.82 | the Jaccard similarity threshold for flagging a pair as merge candidates |
| `episodic_types` | `[section, note, open_question, plan]` | the node types counted as episodic (folding / salience / promotion). `open_question` and `plan` are transient authored states that consolidation must **revisit** (a question answered → promote/retire; a plan done → retire), not keep eternally `active`; `decision`/`adr` are not here — they are protected (`protect_types`) |
| `stale_age_days` | 30 | the base of the recency signal in the salience rubric |

## The automatic recall check (`eval_gate`)

The guard that keeps compaction from silently degrading retrieval: before applying compaction actions, `consolidate.py apply` measures recall on a clone of the graph and commits to the real graph only if recall holds (the mechanics — [Consolidation](./07-consolidation.md), "The automatic recall check"). Read by `consolidate.load_config` (the block merges with the defaults by individual keys).

| Key | Value | Meaning |
|---|---|---|
| `eval_gate.enabled` | `true` | enable the check; with `false` compaction applies unmeasured |
| `eval_gate.cases` | `.claude/skills/amg-retrieve/evals/cases.json` | the labeled-cases file (`{id, query, gold_ids}`); an absolute path or relative to the project root |
| `eval_gate.min_recall_delta` | −0.02 | the allowed `pack_recall` drop (the "after − before" delta; below → fail) |
| `eval_gate.min_hop_recall_delta` | −0.02 | the allowed `hop_recall` drop |
| `eval_gate.on_fail` | `reject` | the failure reaction: `reject` (do not commit), `warn` (commit + record the drop), `revert` (≡ `reject` — the measurement happens before the commit, so there is nothing to roll back) |

**Robust by default.** If the `cases` file is absent, empty, or none of its `gold_ids` exist in the graph, the check is **skipped** (`status: skipped`) — compaction is never falsely blocked. The shipped `evals/cases.json` is a neutral template (its `gold_ids` do not resolve), so in an unlabeled install the guard is safely inactive. To turn it on, label your own cases (ids from `inspect_graph.py`) and point `eval_gate.cases` at them.

## Provenance and verification (`verification`)

The trust layer (the mechanics — [Retrieval](./06-retrieval.md), "The trust layer"; theory — [§15](../THEORY.md)): every fact carries its origin and confidence, and a claim about code is checked against the live source before the answer. A top-level block (lifted in `retrieve.load_config` over the defaults; `default_confidence` is read by `reconcile.apply_derivation`). A mark **does not demote** activation — an unverified/stale/disputed/low-confidence node is flagged in the pack, like `stale`.

| Key | Value | Meaning |
|---|---|---|
| `verification.enabled` | `true` | the master switch of the marking layer; with `false` the pack carries only the `stale` flag |
| `verification.verify_code_claims` | `true` | the loop runs `verify_claims.py` on a code claim before answering (file/symbol/hash); also governs the marking of unverified code nodes |
| `verification.warn_on_unverified` | `true` | mark an unverified (`unverified`) code node in the pack |
| `verification.min_confidence_warn` | `0.5` | mark a node whose `confidence` is below the threshold |
| `verification.default_confidence` | `0.7` | the confidence for a derived summary the builder gave no explicit estimate for (read at derivation `apply`) |

`verify_claims.py` (read-only by default; `--write` stamps the node's `verification` block for an audit pass) and the pack marking are described in [Retrieval](./06-retrieval.md). The node fields `confidence`/`provenance`/`verification` — in [Data model](./02-data-model.md), "The trust layer".

## The graph viewer (`viewer`)

The block configures the 3D graph viewer (`export_graph.py` → a self-contained offline HTML; the mechanics — [Evaluation and tools](./10-eval-tools.md)). It is a **thin layer** over the `3d-force-graph` library: a few keys with clear AMG names plus the `options` pass-through, so the config does not restate the library's whole surface. All keys are optional; the exporter puts the block into the HTML's `meta`, and the viewer reads it as its defaults (the UI can change them on the fly).

| Key | Value | Meaning |
|---|---|---|
| `viewer.quality` | `auto` | node and edge smoothness (the sphere/tube segment counts): `auto` (higher on small graphs, lower on big ones) / `high` / `medium` / `low` |
| `viewer.large_graph_mode` | `auto` | the large-graph mode **at startup**: `auto` (on when the node count exceeds `large_graph_nodes`) / `on` (always — for a graph that would otherwise stall) / `off` (show everything at once). The startup state is "baked" into the HTML, so changing it requires a re-export; the in-viewer button toggles it live |
| `viewer.large_graph_nodes` | `1500` | the node-count threshold above which `large_graph_mode: auto` engages |
| `viewer.min_edge_weight` | `0.0` | the starting position of the "hide weak edges" slider (0 = show all) |
| `viewer.options` | `{}` | the **pass-through**: a "`3d-force-graph` method name → its argument" map applied to the graph as is (a broken key/value is silently ignored) — so the config does not duplicate the library's keys. Their full list — the [`3d-force-graph` API reference](https://github.com/vasturiano/3d-force-graph#api-reference) |

YAML reads bare `on`/`off` as the booleans `true`/`false`; the viewer accepts both a boolean and a string, so `large_graph_mode: on` works as expected.

## Planned (roadmap)

Every key designed so far is in the template (the `derivation` key is implemented — see "The derivation strategy" above). Future keys will appear here as they are planned; the full plan — the [roadmap](./11-roadmap.md).

## Next

- [Documentation map](./README.md) — the architecture table of contents and the way back to the start.
- [01 — The big picture](./01-overview.md) — the two planes and the repository structure.
- [06 — Retrieval](./06-retrieval.md) — what the `retrieval` block keys do.
- [07 — Consolidation](./07-consolidation.md) — what the `weights` and `compaction` block keys do.
- [08 — Subagents and skills](./08-agents-skills.md) — `active`, `automation`, `session_policy`, `models` in the lifecycle-layer context.
