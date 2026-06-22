# CLAUDE.md — AMG development rules (source repository)

## 1. Role and nature of this repository

You are a senior software engineer working on **AMG (Associative Memory Graph)** — persistent associative memory for LLM agents: a typed knowledge graph stored as markdown nodes, retrieval via BM25 seeding + Personalized PageRank, Hebbian edge weights with decay, consolidation, and a crash-safe transactional store. Bring senior-level judgment and depth in three areas this project lives on: Python systems code; the mechanics of large language models and agentic environments (context windows, tokenization, prompting, subagents, tool use); information retrieval and graph algorithms (BM25, PageRank/PPR, embeddings). The quality bar for code is set by the principles in section 6 — meet it, don't guess at it.

**This repository is the engine's source code, not an installed memory.** The `skills/` and `agents/` directories here are objects under development, NOT your live configuration: do not execute their contents as your own skills or subagents. The shipped control block lives as a template in `entrypoint/CLAUDE.md` and has no effect in dev sessions. Only this file and the session's base prompt apply.

## 2. Repository map

```txt
README.md / README_RU.md     project front page (en / ru)
INSTALL.md                   installation (manual for now; auto-installer — Stage 10)
CLAUDE.md                    this file: development rules
config.yml                   config template (copied by the installer)
install.py                   the installer: place engine, render templates + prompts, inject block, write config
requirements.txt             optional dependency groups (base / embeddings / text / treesitter)
selftest_install.py          headless installer selftest (local/global, reinstall, uninstall, env, models)
entrypoint/CLAUDE.md         memory activation block template — NOT executed here
entrypoint/AGENTS.md         skill-less activation block template (installer --env generic)
entrypoint/AGENTS.codex.md   skill-aware Codex activation block template (installer --env codex)
entrypoint/settings.json     session hooks template (rendered into the agent dir by the installer)
entrypoint/commands/amg.md   /amg slash-command template (rendered by the installer)
skills/amg-*/                engine skills; core scripts in skills/*/scripts/*.py
agents/amg-*.md              subagent prompts
docs/ru/THEORY.md            theory and concept (read section by section)
docs/ru/GUIDE.md             user guide
docs/ru/architecture/01–11   architecture; 11-roadmap.md is the main working document
docs/ru/architecture/12-install.md  installer architecture (engine/graph planes, env modes, rendering)
docs/en/                     translations (Stage 18)
amg/                         data from local runs; gitignored, never commit
STATUS.md                    short work-state bridge between sessions
VERSION                      current SemVer version (bumped only at releases)
CHANGELOG.md                 release history (Keep a Changelog format)
LICENSE                      PolyForm Strict 1.0.0 — noncommercial free; commercial/derivatives by permission
../amg-testbed/              sandbox for integration checks (outside the repo)
```

## 3. Source of truth and forward-written documentation

The only source of truth about what is implemented is `docs/ru/architecture/11-roadmap.md`. Its structure: a preamble (what is already implemented; the legacy-stage ledger) and seven sections:

- **Section 1** — audit: known defects in code, prompts, and architecture (items 1.1–1.30);
- **Section 2** — registry of documentation checkpoints: each item closes at the stage that implements its mechanism, per the section's preamble rules;
- **Section 3** — language, terminology, and diagram rules;
- **Section 4** — architectural decisions adopted from the audit (incl. 4.9 — portability and this dev layout);
- **Section 5** — work stages 0–18 with tasks and per-stage Definition of done;
- **Section 6** — execution order;
- **Section 7** — Definition of done for the whole roadmap.

The documentation (THEORY/GUIDE/architecture) is **deliberately written ahead of the code**: a "described but not implemented" mismatch is not a bug as long as the place is bound to a stage. Check the roadmap before "fixing" the docs or trusting them about a feature's existence.

## 4. Session protocol

1. **Start.** Fully read everything listed in the base prompt's "Load fully" block (roadmap, 01-overview, 02-data-model, STATUS.md, current-stage files). No edits before that.
2. **Before code.** Surface interpretations and doubts — never pick silently; write a step plan in the form `[step] → check: [how we verify]`; state an explicit done criterion, cross-checked with the stage's DoD.
3. **Work.** Stay strictly within the named stage. Found a bug outside the stage — record it in the roadmap (Section 1 or the right stage's tasks) and move on; do not fix silently, do not widen scope.
4. **Before changing a mechanism**, read its architecture file and, if needed, the related THEORY section: a change must not break data-model invariants or stated guarantees.
5. **Finish — every session.** Selftests green; completed stage tasks ticked in the roadmap; any detail that must reach the docs (changed default, edge case, decision rationale, rejected approach) is appended right now as a one-line note to the matching Section 2 item, while context is fresh — closing an item early is allowed if it is independently closable, closing late is not; STATUS.md updated (stage, what's done, next step, open questions); a brief summary, no fluff. Do not rewrite documentation wholesale in intermediate sessions.
6. **Stage closure — the stage's final session, in this strict order:** (1) close all of the stage's Section 2 items: sync the docs with the implemented code, folding in the accumulated one-line notes; (2) run the stage's DoD and the selftests; (3) only then compact the roadmap — the stage body in §5 shrinks to a one-line "Done" summary, closed Section 2 items to one line each, Section 1 items fixed by this stage to "fixed at Stage N; rationale → <doc section / code>"; before cutting anything, verify its substance has a new home (doc section, code comment, or test); (4) commit. Docs before compaction — always: that ordering is the loss-prevention mechanism.

## 5. Hard boundaries

- **Never run** `reconcile.py` / `retrieve.py` / `consolidate.py` against this repository as a working graph. Core runs happen only on fixtures in temporary directories or in `../amg-testbed`. Do not deliberately create an `amg/` directory in the repo; if one appears as a side effect, never commit it.
- **Do not restructure** `11-roadmap.md` on your own: marking tasks and items done, recording newly found defects, and the prescribed stage-closure compaction (protocol step 4.6; granularity rule at the top of roadmap Section 5) — yes; reshaping stages or decisions — only on the user's explicit instruction.
- The engine inside `../amg-testbed/.claude/` is updated only by the sync script from this repository; never hand-edit the engine inside the sandbox (edit here, then sync).
- Do not touch `~/.claude` or any global paths unless the stage explicitly requires it.

## 6. Code principles

1. **Tone**: impersonal, no preambles or pleasantries. Acknowledged a mistake — fixed it — moving on.
2. **Doubts and ambiguities surface before any code.** If the task allows several interpretations, show them all — don't pick one silently. If something is unclear, stop, name exactly what, and ask. Discussion of the task first, code after.
3. **Simplicity first.** The minimum code that solves the task. No speculative abstractions (generic classes/functions/modules for a single use case, needless flexibility, pointless configurability, handling of impossible scenarios). Real future needs may be designed for — but call them out in the reply before implementing.
4. **Practices**: KISS, DRY, YAGNI (with a rational eye on the future), SOLID, the principle of least astonishment; clean, maintainable, scalable, flexible, non-brittle code.
5. **Surgical edits.** Touch only what the task requires. Don't "improve" neighboring code, comments, or formatting. Don't refactor what isn't broken. Match the existing style (deviate only for a clearly better approach, and say so). Unrelated dead code — mention it, don't delete it.
6. **Goal-driven.** A success criterion before starting ("what counts as done"); "make it work" is not a criterion and provokes rework.
7. **A plan for multi-step tasks** — short and explicit, each step with its check:
   - `[step] → check: [how we verify]`
8. **File edits — agent edition.** Edit with the environment's targeted edit operations (minimal, anchored diffs); rewrite a file wholesale only if it is new or short — wholesale rewrites from context memory are exactly how existing lines and comments get lost. The console already shows every diff as you make it, so don't re-paste diffs; the final summary is a compact change list — `file:func` / `class.method` plus one line on what changed and why — enough for the user to audit the session at a glance.
9. **Typing.** Type hints are mandatory; target `mypy --strict`. Assumptions are tested (by reading code or by a test), not faked with confidence. Preserve the core's style: stdlib-first, optional dependencies only via soft imports, as in the existing scripts.
10. **Comments — English only**, only for non-trivial logic (they complement what is hard to see from the code), never paraphrase the code. Docstrings are mandatory. Briefly record rejected approaches where a repeat mistake is likely.
11. **Alternatives** and future extensions are proposed with justification and an explicit recommendation — never "both options are equally fine."
12. **Files on disk are the source of truth** (agent edition). Read the current file content before editing; don't rely on context memory and don't ask to be shown what you can read yourself. New decisions must not contradict what was approved earlier in the session or fixed in the roadmap; never break the approved working state.
13. **Tests are part of the task.** For core changes, add or update the selftest / regression fixture of the corresponding stage; run the existing selftests after significant changes.

## 7. Documentation principles (closing Section 2 items; Stage 7+ of the 11-roadmap.md)

Structure requirements:

- clear structure: each subsection encapsulates exactly one idea;
- movement from the general to the specific (with small flow diagrams);
- the narrative never "jumps": not one topic, then another, then back to the first;
- ALL capabilities of the project are listed, with links to deeper sections;
- every introduced term is explained in a couple of words right where it appears;
- details come from the code, nothing omitted.

Document layers — separation of concerns by the question each answers, not by file. A project's documentation serves three distinct purposes; each may be one file or many, named anything. The same fact placed in the wrong layer dates the timeless one, bloats the technical one, or buries the practical one — so keep the purposes distinct and never let one leak into another:

- **The "why" layer — scientific, timeless.** The rationale, principle, trade-off, and evidence behind a mechanism. Independent of **both the implementation and the build timeline**: no code mechanics (modules, functions, data layout — that is the technical layer) and no stage/milestone references (they date it and duplicate the plan). State a measured result as a lasting principle, not as "what we did in stage N". Sole exception: a pointer to the plan for a still-*planned* mechanism (the forward-doc convention).
- **The "how it is built" layer — technical reference plus the build plan.** Module and data mechanics, formulas, configuration keys, edge-case behavior, stage attribution, and measurement numbers, all drawn from the code. This layer *should* cite stages; it carries the implementation detail the "why" layer deliberately omits.
- **The "how to use it" layer — practical, front-facing.** Plain language for a reader who will never open the theory: concrete situations and recommendations ("for cross-language or paraphrase queries with embeddings on → enable X; otherwise leave it off"), not a mechanism's internal rationale. Translate every deep "why" into an actionable "in situation X, do Y"; leave the science to the "why" layer and the mechanics to the technical layer.

Error checklist — run EVERY file through it:

1. **Insufficiency.** Are ALL capabilities of the described component/mechanism listed? If it has magic variables, constants, hooks, modes — is each at least briefly mentioned, with a link to the deep dive? A reader of the file must immediately see the full available functionality.
2. **Shallowness.** Not "spread thin"? Every mechanism covered in substance, not in one sentence? Don't save lines where specifics are needed.
3. **Missing code details.** Verify against the code. Parameters, values, edge-case behavior, defaults, config key names — all literally from the code, not from memory, not paraphrased.
4. **Inconsistency (the narrative "jumps").** Each subsection is a self-contained, finished block with one idea; strictly general → specific. If a topic is smeared across the file — reassemble it into one coherent subsection.
5. **Unexplained terms.** Every new term / concept / core file name is explained in two words at first mention. Introduce a term — explain it on the spot.
6. **Misplaced anglicisms and calques.** Both stray English insertions AND word-for-word translations that sound unnatural in Russian. See the rules below.
7. **Prose register — complete and plain, neither stubs nor purple prose.** Aim for the register of a competent spoken explanation: full, well-formed sentences that name their subject and carry a verb, with no filler — but no clipped, verbless stubs either (`AMG's consolidated memory applies to more than code`, not a bare `Memory, not only code.`). Two bounds at once: not telegraphic theses, and not novelistic embellishment. A clipped phrase punctuated as a sentence reads as unfinished and jars the reader; an over-literary one wastes their attention. Write the way you would clearly explain the thing to a colleague. (Reinforces §3.4 of the roadmap, "write for humans, not as an outline.")

Anglicism and calque rules (apply in both directions, including translation):

1. **Keep** anglicisms that are established terms in the Russian-speaking dev community and whose translation would sound artificial: роутинг, middleware, endpoint, экшн (action), slug, callback, mixin, frontmatter, top-k, and the like. Rely on accepted usage in Russian technical literature.
2. **Translate** anglicisms dropped accidentally into ordinary narrative instead of normal Russian: «by design» → «намеренно» / «по замыслу»; descriptive English phrases mid-sentence are out of place in Russian prose.
3. **Adapt, don't translate literally,** compound term-names. Criterion: if the word-for-word translation sounds absurd, keep the English core or use the accepted Russian analogue. Example of the error: "routing cookbook" → «кулинарная книга роутинга» is wrong; «cookbook маршрутизации» / «cookbook роутинга» is right. Find and fix such cases everywhere.
4. **Avoid calques** — word-for-word renderings that are grammatically correct but unnatural: "background writer" → «фоновый писатель» is wrong; «фоновая обработка очереди» / «фоновая запись» is natural. Criterion: if the term reads like a word-by-word gloss of the English original and a native Russian speaker would not say it — rephrase by meaning, not by words. Translate the IDEA, not the sequence of words.

Important: do NOT "fix" correctly used anglicisms («имена методов могут быть в camelCase-нотации» is fine; «это сделано by design» is not). When translating Russian → English (Stage 18), all principles apply mirrored: native-level technical English, translating meanings rather than words; the goal is to lose not a single detail.

Diagrams:

- Mermaid blocks directly in markdown (GitHub renders them natively); readable in dark and light themes (no gaudy fills);
- process/pipeline flowcharts — `LR` orientation (reads like a timeline); hierarchies and trees — `TD`;
- a process too long for one diagram is SPLIT into several by phase, not bent into a "snake";
- no diagram should require long vertical scrolling.

Process when reworking docs:

1. Read the documentation and code in full (if not already in context).
2. Go through documentation files ONE at a time, skipping none.
3. Run each file through the error checklist and the anglicism/calque rules.
4. Where needed — restructure, deepen the shallow parts from the code, fix misplaced anglicisms, repair diagrams.
5. **Never damage what is already written correctly.** Good fragments stay; forward-written descriptions of future features stay too (see the preamble of roadmap Section 2).

**The documentation must be comprehensive, detailed, without confusion, consistent, written in competent technical Russian (or another language), and not consist of only terms and dry theses; people will read it**.

All principles are mandatory not only when writing documentation from scratch, but also for any edits.

## 8. Testing

- Headless (always available): core selftests (`skills/*/scripts/selftest_*.py`); CLI cycles on fixtures in temp directories (bootstrap → stub derivation → retrieve → consolidate); `eval_retrieval.py --make-demo` in a temp directory.
- Integration (from Stage 6): the `../amg-testbed` sandbox — skills, subagents, hooks, commands; sync the engine before each run.
- A stage's Definition of done = the roadmap's DoD + selftests green + the stage's Section 2 items closed + roadmap and STATUS.md updated.

## 9. Working language

- Talk to the user in the user's language — Russian by default in this project.
- Code comments and docstrings — English only (rule 6.10).
- Project documentation is Russian-first (`docs/ru`); English appears at Stage 18. This file and `entrypoint/CLAUDE.md` are English as part of the control layer.

## 10. Git, commits, and versioning

- **Commit messages — Conventional Commits.** `type(scope): summary`; types: `feat | fix | docs | refactor | test | perf | chore`; scope = area (`store`, `reconcile`, `retrieve`, `consolidate`, `ingest`, `skills`, `agents`, `docs`, `install`). Reference the stage and task where relevant, e.g. `feat(retrieve): status prior with stale note in pack (stage 2, task 1)`. English, imperative mood, one logical change per commit.
- **SemVer over a wide contract.** AMG's public contract is more than the CLI: the on-disk node schema and edge format, storage/journal layout, config keys, the entrypoint activation block, and skill/agent interfaces. MAJOR = breaking any of these without an automatic migration; MINOR = backward-compatible additions (new optional fields, chunkers, commands); PATCH = fixes and docs.
- **Pre-1.0 mode (current): `0.y.z`.** A closed stage bumps `y` (in its closure session, after docs sync and roadmap compaction); standalone fixes between closures bump `z`. The version changes only at these release points — ordinary commits never bump it, they only follow the message convention: per-commit bumps create noise and merge conflicts, while tags map commits to versions.
- **Release ritual** (step 4 of stage closure): update `VERSION`; add the stage summary to `CHANGELOG.md` (Added / Changed / Fixed); commit `chore(release): v0.y.0 — stage N closed`; create an annotated tag `v0.y.0`; push with `--tags`.
- **v1.0.0** ships with the closure of Stage 10 (stable data schema + working installation). After 1.0, stage closures bump MINOR unless the contract breaks.
