# 11 — Roadmap

> **This is an English snapshot, taken at the v1.13.0 release (the close of Stage 22).**
> The roadmap is a **living working document**, and its source of truth is maintained in
> Russian: [`docs/ru/architecture/11-roadmap.md`](../../ru/architecture/11-roadmap.md).
> Translating it in full would duplicate every per-session edit, so this English page is a
> stable orientation to the document's structure and the stage ledger, not a line-by-line
> mirror. For the exact, current audit items, documentation checkpoints, and stage
> Definitions of done, read the Russian original — it is where new defects and progress are
> recorded first.

The roadmap records what is already implemented in AMG, which mismatches an audit has surfaced, and in what order the project is brought to a coherent state. It serves two functions:

1. **Stabilizing the current implementation** — fixing mismatches between code, prompts, and documentation, and real engine bugs.
2. **The development plan** — adding the planned lifecycle, installation, visualization, quality-check, memory-verification, and scaling features.

**The roadmap-keeping rule.** The project's documentation is written **ahead of the code** — it deliberately describes the system's target state, including features not yet implemented, so the same thing is not written twice. The single source of truth about what is implemented and what is not is the roadmap: its Section 2 binds every forward-written place in the documentation to the stage at which it is reconciled against finished code. Until its stage, a forward-written description is neither deleted nor edited; anything partly implemented is recorded in the roadmap with a concrete list of remaining work.

## Document structure

The Russian roadmap is organized as a preamble (what is already implemented; the legacy-stage ledger) plus eight sections:

- **Section 1** — the audit: known defects in code, prompts, and architecture (items 1.1–1.76). As of this snapshot, **no open items remain** — each has been fixed at its stage and collapsed to a one-line "fixed at Stage N; rationale → <doc section / code>".
- **Section 2** — the registry of documentation checkpoints: each item closes at the stage that implements its mechanism, when the forward-written place is reconciled against the finished code.
- **Section 3** — language, terminology, and diagram rules (the anglicism/calque rules, the term table, the "write for humans" readability rule §3.4). These apply mirrored in both translation directions.
- **Section 4** — the architectural decisions adopted from the audit; the ones referenced from the documentation include §4.1 (markdown canon vs. the generated SQLite index), §4.2 (deterministic edges before the LLM), §4.6 (semantic-drift segmentation, measured and declined), §4.9 (engine portability: the agent directory and entry point as parameters), and §4.10 (lazy / on-demand semantic derivation).
- **Section 5** — the work stages, each with its tasks and Definition of done.
- **Section 6** — the execution order.
- **Section 7** — the Definition of done for the whole roadmap.
- **Section 8** — possible future directions (ideas, deliberately not stages) with the rationale for rejected ones — for example, cross-repository shared memory (§8.1) and "smart" scalar pack cutoffs (§8.5), both considered and rejected with their rationale.

## The stages

The stabilization stages run before the new features: first bring the current pipeline to a correct state, then extend. The order:

| Stage | Theme | Status |
|---|---|---|
| 0 | Core reconciliation stabilization | done (v0.2.0) |
| 1 | Data-model stabilization | done (v0.3.0) |
| 2 | Retrieval stabilization | done (v0.4.0) |
| 3 | Consolidation stabilization | done (v0.5.0) |
| 4 | Automatic eval gate for consolidation | done (v0.6.0) |
| 5 | Safe note-capture API | done (v0.7.0) |
| 6 | `amg-classifier` integration | done (v0.8.0) |
| 7 | Baseline documentation sync | done (v0.9.0) |
| 8 | Lifecycle and control commands | done (v0.10.0) |
| 9 | Session saving | done (v0.11.0) |
| 10 | Packaging and installation | done (v1.0.0) |
| 11 | Ingest extension | done |
| 12 | Performance and scaling | done |
| 13 | Provenance, confidence, and verification | done |
| 14 | Epistemic contradiction arbitration | done |
| 15 | The 3D graph viewer | done |
| 16 | Team work and the git graph | done |
| 17 | The advanced semantic layer | done |
| 18 | Deployment and store resolution | done (v1.9.0) |
| 19 | Correct and connected graph building | done (v1.10.0) |
| 20 | Economical building (tokens and time) | done (v1.11.0) |
| 21 | Documentation translation into English | done (v1.12.0) |
| **22** | **Field reliability: the residual build cost and the memory-usage loop** | **this snapshot (v1.13.0)** |
| 23 | Testing in non-Claude-Code environments | ahead |

Stages 18–20 and the field Stage 22 are a prerequisite for environment verification: without a reliable, cheap build and a working usage loop there is neither a clean real graph nor a real `usage.log`. Stage 22 closed on four field runs (two installs, an incremental rebuild, fifteen working sessions on a real ~1 MB project): batched application and prepared global-pass inputs, linker checkpoint parts with a judged-pairs memory (the linking pass converges), the scale-free relative pack threshold, the imperative usage loop with the start-of-session diff question and the wrap-up consolidation trigger, two-level retrieval (the direct call is the default), the compact pointer profile (`--compact`), and the gated mid-session reminder (`prompt-hint`). Next is Stage 23, where the memory's operation is confirmed in environments other than Claude Code (Codex with skills and TOML subagents; other AGENTS.md agents via the portable skill-less block).

## Definition of done for the whole roadmap

AMG is considered brought to a coherent state when, in brief: code, prompts, and documentation describe one and the same system; every Section 2 item is closed at its stage with no forward-written descriptions left unconfirmed by code; `bootstrap` self-heals the stale queue after a crash; retrieval yields correct `path:line` pointers; structural edges update on source changes; hubs have `type: hub`/`overview` and join the strategic tier; `source_kind` is normalized; `compaction.enabled` works; protected types are enforced in code; `shorten` never loses the original on a repeated apply; `superseded` and `stale` are honored at retrieval; `amg-classifier` really affects ingest; `models` works; `absorb` is described identically everywhere; the eval is used before/after retrieval tuning and compaction; the documentation and its translation pass the checklist (complete, consistent, general-to-specific, terms explained, no misplaced anglicisms or calques, details from code, readable diagrams); a renamed or moved source file does not erase earned summaries and edges; `imports` edges resolve to in-project modules; branches are reachable from hubs so budgets are counted and compaction fires on overflow; the engine is parameterized by agent directory and entry point and works outside Claude Code; Hebbian weight learning is enabled only after a measured eval uplift; and a light verification of code claims runs before answering, with `stale` nodes flagged in the pack.

## Next

- [Documentation map](./README.md) — the architecture table of contents.
- The Russian living document — [`docs/ru/architecture/11-roadmap.md`](../../ru/architecture/11-roadmap.md) — for the full, current audit, checkpoints, and stage detail.
