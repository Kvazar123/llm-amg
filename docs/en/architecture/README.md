# AMG — Architecture

This set of documents describes the **implementation** of AMG: modules, data formats, algorithms, configuration parameters, and control flow. The theoretical foundations (the memory model, the rationale behind the mechanisms, the connection to research) live separately — see the [theory](../THEORY.md). The documents complement each other: the theory answers "why it is this way", the architecture answers "how exactly it is built".

The architecture describes **AMG (Associative Memory Graph)** — persistent consolidated memory for language models, shaped as a graph of linked nodes over the filesystem. The system is implemented as a set of Python scripts, skills, and subagents for Claude Code; the graph engine is domain-blind, and the data-type-specific layer sits at the input only.

> **On path names.** Throughout these documents, the agent directory `.claude` and the entry-point file `CLAUDE.md` are the **Claude Code defaults**. The engine itself does not depend on the environment: in another agent environment (for example, OpenAI Codex) the configured names are substituted — the agent directory, say `.agents`, and its entry point, say `AGENTS.md`. Paths in the examples keep `.claude`/`CLAUDE.md` as an illustration of the default.

## Documentation map

The documents are numbered in general-to-specific reading order. Each is devoted to one layer or module.

- [01-overview.md](./01-overview.md) — the big picture: the deterministic layer and the judgment layer, the module map, the data flows (structure extraction, retrieval from the graph, consolidation).
- [02-data-model.md](./02-data-model.md) — the data model: the node format (frontmatter), identifiers, edge types, physical buckets, directory layout.
- [03-storage.md](./03-storage.md) — the `graph_store.py` store: atomic writes, the write-ahead journal, the writer lock, recovery and verification; a link to the formal consistency model.
- [04-ingest.md](./04-ingest.md) — extraction by `extract_structure.py`: the type classifier, the chunker registry (Python `ast`, tree-sitter, markdown, RST, plain text, logs, JSON with recursion, NDJSON, CSV, external chat, PDF, DOCX, XLSX, PPTX), the ignore rules.
- [05-reconcile.md](./05-reconcile.md) — reconciliation by `reconcile.py`: the `bootstrap` / `plan` / `apply` modes, the semantic-derivation queue, the `mirror` / `absorb` / `absorb_once` policies, the `derived-*.json` files.
- [06-retrieval.md](./06-retrieval.md) — retrieval by `retrieve.py` and `embed.py`: lexical seeding (BM25), optional semantic seeding (embeddings), Personalized PageRank, tiered pack assembly, configuration keys.
- [07-consolidation.md](./07-consolidation.md) — consolidation by `consolidate.py`: weight folding (Hebbian rule + decay + pruning), the salience rubric, staged branch compaction.
- [08-agents-skills.md](./08-agents-skills.md) — subagents and skills; the activation loop in `CLAUDE.md`; how deterministic and model work is split across the roles.
- [09-config.md](./09-config.md) — the complete `config.yml` reference: every key, its default, and its meaning.
- [10-eval-tools.md](./10-eval-tools.md) — measurement tools: `eval_retrieval.py`, `inspect_graph.py`; the recall / precision / hop-recall metrics.
- [11-roadmap.md](./11-roadmap.md) — the development plan and what is not yet implemented: the lifecycle layer (hooks and commands), packaging and installation, more input formats, indexing and scaling, fact provenance and verification, contradiction arbitration, the 3D graph viewer, the team mode, and other deferred work.
- [12-install.md](./12-install.md) — the architecture of the `install.py` installer: placing the engine, rendering templates and prompts per environment (Claude Code / Codex / generic), injecting the activation block, merging `settings.json`, templating `config.yml`, model tiering, reinstall and uninstall. A reference document (like 09/10); the main working document is the roadmap above.

## Implementation status

Documents 03–07, 09, 10, and 12 describe **implemented and tested** modules. The [roadmap](./11-roadmap.md) collects what has been designed but not yet built; inside the other documents, unimplemented elements are marked explicitly as **(planned)** with a short pointer to the corresponding roadmap section, so the boundary between what is ready and what is planned stays visible as you read.

## Diagram conventions

Diagrams are `mermaid` blocks embedded directly in markdown (GitHub renders them natively), with no decorative fills. Pipelines and step sequences are drawn left to right (`flowchart LR`); hierarchies, trees, and dependency maps top-down (`flowchart TD`). Long processes are split into several diagrams by phase rather than stretched into one.

## Reading order

For a general understanding, the [Big picture](./01-overview.md) is enough. From there, follow the layer you care about; the documents are self-contained but rely on the data model from [Data model](./02-data-model.md), so it is worth reading second. Parameters from the [Configuration reference](./09-config.md) are mentioned in place in the per-module documents, with a link to the full reference.
