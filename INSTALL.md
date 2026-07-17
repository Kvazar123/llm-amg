# Installing AMG

AMG is installed **by the model** (you give it the path to this file; it asks the questions and sets everything up — section 1) or **manually** with the installer command (section 2). Either way one script does the work — `install.py`: it copies the engine, renders the activation block and hooks for your environment, writes `config.yml`, and verifies the store. Python 3 is required.

Русское зеркало этой инструкции — [INSTALL_RU.md](INSTALL_RU.md); исполняемый оригинал, который читает модель, — этот файл.

**Install from outside the project: keep the unpacked AMG folder out of the target project.** The installer runs from the unpacked AMG folder and points at the project with `--target`, so the folder itself may live anywhere — the home directory, a shared tools folder, wherever convenient. Do not put it **inside** the project being installed: a scatter of engine files clutters the project root for no benefit (the engine is copied into the agent directory anyway). Store resolution can never mistake the source folder for the memory store — it recognizes the folder by its content and never treats an AMG checkout as a graph. After the install the source folder is no longer needed and may be deleted.

**The folder's own `CLAUDE.md` is not part of the install.** The unpacked AMG folder carries a `CLAUDE.md` with development rules for AMG's own source code. If it ends up in your context (say, the folder was unpacked inside the project against the advice above), ignore it entirely: during an install, this file — INSTALL.md — is the only instruction that applies.

**The graph is always local.** It lives in `<project>/.claude/amg/` even when the engine is installed globally, because memory belongs to a specific project; there is no shared graph across projects.

**What goes where.** The *control plane* (the engine) is the `skills/` and `agents/` directories, the activation block in the entry point, the session hooks (or, for OpenCode, the event plugin), and the `/amg` command in the environment's native command surface. The *content plane* (the graph itself) is created in `<project>/.claude/amg/`. The names `.claude` (agent directory) and `CLAUDE.md` (entry point) are the **Claude Code** defaults; another environment substitutes its own names, say `.agents` / `AGENTS.md` (see "Other environments" below).

## Section 1. Model-driven install

Unpack AMG into any folder **outside the project** and, in a session inside your project, tell the model: **"install AMG per `<path-to-AMG>/INSTALL.md`"** (for example, `~/tools/amg/INSTALL.md`). The model reads this file, determines the mode (step 0), surveys the project, asks the questions below (each with a short note), then runs `install.py` from that same folder with your answers.

**Step 0 — determine the mode by STATE, never by the user's wording.** Before anything else, check whether the project already has `<agent dir>/amg/config.yml`. **It exists → this is a REINSTALL**, whatever the request said — "install", "reinstall", and "update" all land here: show the values in force, ask ONE question — what to change (or ask precisely about whatever the user themselves named) — and do **not** run the fresh-install questionnaire. The installer never rewrites an existing config wholesale, but the keys **explicitly passed on this run** (`--set`, `--mirror`, `--absorb`, `--absorb-once`, `--exclude`) are applied to it as surgical line edits and echoed as `updated keys:` — so pass each changed answer as a flag, and an answer the user gave is never silently dropped; everything not passed stays as it was (shown on the `in force:` line). A reinstall with nothing to change is the normal engine-update path — run the installer with no config flags at all. **No config → a FRESH install**: survey the project and ask the full questionnaire below. Keying the mode to the config's existence is what makes the flow repeatable across models and phrasings.

**Survey the project before asking about sources** (fresh install). List the project's top-level folders (and the notable file kinds in them) and **propose a classification**: which folders look like sources to mirror (code, documentation the user maintains), which look like one-shot material to absorb (chat logs, data dumps, third-party documents), and which deserve extra excludes (generated code, vendored assets — beyond the built-in ignore list). Present the proposal together with questions 3–5, so the user confirms or corrects a concrete list instead of recalling paths from memory.

**Ask every question, in fixed series of at most four.** In Claude Code the question tool accepts at most four questions per call; keep the same series in any other environment too, even where more per message is possible — the thematic batches below exist to prevent the observed failure of collapsing the whole flow into one message and silently defaulting whatever did not fit. An empty answer takes the stated default — but the question must still be asked: silently defaulting the KEY items is how a graph gets built in the wrong language or without embeddings. **The option lists inside the questions are part of the contract: present them as written** — all five environments in question 2, both embedding backends in question 7, every remaining dependency group in question 11 — never re-derive a list from memory or shorten it (the observed failure: the environment list sometimes missing an entry, the dependency list varying run to run). The one legitimate adaptation is what an earlier answer already settled — e.g. the backend chosen in question 7 drops out of question 11. The series:

- **Series I — placement:** questions 1–2 (scope; environment).
- **Series II — sources, asked AFTER the survey:** questions 3–5 (mirrors; absorb / absorb_once; excludes). **The absorb question is mandatory even when the survey proposes nothing to absorb** — the user may hold one-shot material the survey cannot see (exported chats, dumps kept elsewhere).
- **Series III — KEY decisions:** questions 6–7 (working language; embeddings). Their answers are expensive to change after the graph is built (see "Cheap to change later vs decide before building").
- **Series IV — behavior:** questions 8–11 (automation; session policy; tier budgets; remaining dependencies).
- **Series V — models and performance:** questions 12–14 (model tiering; compaction; the synthesis-sheet ceiling).
- **Closing question:** question 15 — activation, with the immediate build (`--build`) folded into its options (activate and build now / activate only / not yet).

Fresh-install questions:

1. **Local or global?** *(default: local.)* Local — the engine inside one project (`<project>/.claude/`); global — one engine for all projects (`~/.claude/`), while **each project still keeps its own graph**. A global install additionally sets up the **global personal-defaults config** — see "Local and global" below.
2. **Environment, agent directory, entry point?** *(default: Claude Code, `.claude` / `CLAUDE.md`; you usually know which environment you are running in — propose it.)* Five modes: **Claude Code** (skills + subagents + hooks + the `/amg` command); **OpenAI Codex** — `--env codex` (skills + TOML subagents in `.codex/agents` + hooks in `.codex/hooks.json`, which run only after the user trusts them via `/hooks`; preset `.agents` / `AGENTS.md`); **OpenCode** — `--env opencode` (it discovers the skills in `.agents/skills` natively; subagents rendered to `.opencode/agent`, the AMG event plugin to `.opencode/plugin`, the `/amg` command to `.opencode/command`; preset `.agents` / `AGENTS.md`); **Qwen Code** — `--env qwen` (skills + markdown subagents + session hooks + the `/amg` command in `.qwen/commands`, preset `.qwen` / `QWEN.md`); **any other / unknown environment** — `--env generic` (a portable skill-less block; the skills still land in `.agents/skills`, the cross-tool location, and the block tells the model to prefer them if the environment discovers them). The rule: when the environment's entry point is known, name it explicitly; unknown → `.agents` / `AGENTS.md`. Details — "Other environments" below.
3. **Mirrors (`mirror_path`)?** — offer the survey's proposal. What you edit (code, maintained documentation): the graph is kept equal to it — a change updates the node, a deletion purges it.
4. **Absorb (`absorb_path` / `absorb_once_path`)?** — offer the survey's proposal. One-shot material (logs, dumps, exported dialogues): deleting the source does not erase the knowledge. If a snapshot must never re-sync even when the original changes, name it frozen — it goes to `absorb_once_path` (flag `--absorb-once`). Absorb is optional — mirrors alone are fine; but **at least one source (`mirror_path` or `absorb_path`) is required** (if everything is empty, ask again).
5. **What to ignore?** — offer the survey's proposal. Glob patterns on top of the built-in defaults and `.gitignore` (written to `exclude`; for finer control there are `mirror_exclude` / `absorb_exclude` and the `respect_gitignore` switch — see [09-config](docs/en/architecture/09-config.md)).
6. **Working language (`working_language`)? — KEY.** *(default: by the project.)* The language of summaries and notes. Summaries and the derivation cache are **keyed by this language**: changing it later re-summarizes nothing by itself (only new and changed units get the new language — the graph drifts bilingual), and a clean switch means re-deriving the whole semantic layer at full model cost. Decide it now. For non-English projects the installer recommends a multilingual embedding model.
7. **Embeddings? — KEY.** Semantic seeding for retrieval, and the vectors the build-time linking pass nominates candidates by. Present the choice with its honest trade-off: **`model2vec`** (the default) — static vectors, no torch, near-instant on every call, the lighter quality; **`sentence-transformers`** — a full transformer, noticeably stronger on paraphrase and cross-language queries, at the price of a heavier install (torch) and seconds-to-tens-of-seconds of cold start per retrieval process; **a custom model** — any HF id (`--set retrieval.embeddings.model=…`); **off**. The default model per backend follows the working language on its own — non-English gets a multilingual one (`potion-multilingual-128M` / `paraphrase-multilingual-MiniLM-L12-v2`), English the retrieval-tuned one (`potion-retrieval-32M` / `all-MiniLM-L6-v2`) — so naming a model is not required. **A chosen backend must be PINNED and installed in this same step**: `--set retrieval.embeddings.enabled=auto --set retrieval.embeddings.backend=model2vec --deps embeddings` (or `…backend=sentence-transformers --deps embeddings-st`). Writing `enabled=auto` alone is the observed failure: `auto` picks whichever backend loads first (model2vec leads the order), so an unpinned "sentence-transformers" answer silently runs on model2vec. Seeding itself heals later (vectors re-encode on demand), but linking nominates by these vectors at build time: building without embeddings and enabling them afterwards means re-running `/amg sync` to gain the links it missed. Decide before the first build.
8. **Automation (`automation`)?** *(default: `true`.)* With `false` the system acts only on `/amg …` commands or an explicit request.
9. **Session policy (`session_policy`)?** *(default: `absorb`.)* For valuable dialogues — `mirror` (every detail stays retrievable).
10. **Tier budgets?** *(default: 4000 / 10000 / 24000 tokens.)* The ceiling on ONE memory answer: how many tokens a single retrieval may hand into the model's window, split by abstraction tier — strategic (hubs and overviews) / tactical (the relevant modules) / operational (the leaf detail in focus). A ceiling, not a mandatory load — a simple query pulls in only its activated neighborhood. Raise it for large-window models that should see more per query; lower it to keep every answer lean.
11. **Remaining optional dependencies?** The embeddings backend was settled by question 7; what is left is `text` (PDF/DOCX/XLSX extraction) and `treesitter` (function-level units and call edges for non-Python code) — see "Optional dependencies".
12. **Model tiering (`models`)?** *(default: `discovery: {haiku, low}` / `module_summary: sonnet` / `synthesis: {opus, high}`.)* Three roles, rendered into the subagents at install: `discovery` — cheap read-only classification and pack assembly (amg-classifier, amg-retriever); `module_summary` — the bulk per-unit summaries and link confirmation that feed retrieval (amg-builder, amg-linker); `synthesis` — the hubs, the overview, the gap report, and consolidation judgment (amg-synth, amg-consolidator). A value is any string the environment accepts, in the environment's own format: the Claude aliases (`opus`/`sonnet`/`haiku`) mean something only to Claude Code; OpenCode expects `provider/model` (e.g. `anthropic/claude-sonnet-4-5`), Qwen Code its native ids (`qwen3-coder-plus`) or a foreign provider in the colon form (`openai:gpt-4o`) — an alias in those environments is dropped and their default model applies. Pass answers as `--set models.<role>=<id>` or, with a reasoning effort, `--set "models.synthesis={model: opus, reasoning_effort: high}"`. Changing the tiering later takes a reinstall (the block is rendered into the agent definitions).
13. **Branch compaction (`compaction.enabled`)?** *(default: on — and idle: nothing is compressed while every branch stays within its budget.)* When a branch outgrows its budget, it is compressed stepwise from the bottom, with the originals archived (reversible) and an automatic recall check guarding the result. Answer "off" (`--set compaction.enabled=false`) if the memory must never compress itself; on "on", confirm the branch budgets — the defaults are 400 nodes / 200000 tokens per branch (`--set compaction.default_branch_budget_nodes=…` / `--set compaction.default_branch_budget_tokens=…`).
14. **Synthesis sheet ceiling (`linker.synth_input_max_chars`)?** *(default: 800000 characters.)* The global synthesis pass receives the whole summary layer as one prepared sheet; a model with a smaller context window (around 256k tokens) loses rows on a sheet that big. For a weak or small-window environment model set 300000 (`--set linker.synth_input_max_chars=300000`) — past the ceiling the engine splits the sheet into whole parts and synthesizes part by part, losing nothing. Keep the default for large-window models.
15. **Activate memory after the install?** The installer **never activates silently**. On consent it writes `active: true`. Separately you may **build the structural skeleton right away** (`--build`) — say plainly what it is: a deterministic, model-free pass (`reconcile bootstrap`) that runs fine in the installing session; the **semantic layer — summaries and links — still needs the model and a NEW session** (`/amg sync` or the first task under automation). Without `--build`, everything is built in the new session.

**Empty answers take the defaults**, except the sources (at least one `mirror_path`/`absorb_path` is needed) — those the installer re-asks. After the answers the model runs `install.py`, which places the engine, renders the block and hooks, writes `config.yml`, installs the chosen dependencies, and runs `verify --repair`. The graph is **not built by itself** — that is the activation step's decision. The AMG source folder may be deleted after the install.

**Close with the restart instruction.** The final message must tell the user to **restart the session** (start a new one): an agent environment registers skills and the `/amg` command at session start, so in the installing session they are not live yet. For the same reason, **do not start the first build here**: without the `amg-bootstrap` skill the model improvises the pipeline from scratch and loses its orchestration discipline (batching, checkpoints, batched application). The first build belongs to a fresh session — see "After the install". **For `--env codex`, add one more closing step: open `/hooks` in Codex and TRUST the AMG hooks** — Codex reviews unmanaged hooks and does not run them until the user approves each (trust is a hash of the definition, so a reinstall that changes a hook re-requires the review); until then the memory still works, with the start check run by the model per the activation block.

## Section 2. Manual install (the installer CLI)

The same `install.py` runs directly — no model needed. From the AMG source folder (kept anywhere outside the project; disposable after the install):

```bash
# local, active, with the graph built right away:
python install.py --target /path/to/project --scope local \
    --mirror src,doc --absorb logs --set active=true --set working_language=ru \
    --set retrieval.embeddings.enabled=auto \
    --deps base,embeddings --build

# global (the engine in ~/.claude, each project's graph stays local):
python install.py --target /path/to/project --scope global --mirror src
```

Key flags: `--scope local|global`; `--env claude-code|codex|opencode|qwen|generic` (the environment — see "Other environments"); `--agent-dir` / `--entrypoint` (presets: `.claude` / `CLAUDE.md`; Codex, OpenCode, generic — `.agents` / `AGENTS.md`; Qwen Code — `.qwen` / `QWEN.md`); `--mirror` / `--absorb` / `--absorb-once` / `--exclude` (comma-separated lists); `--set key=value` (repeatable: `active`, `working_language`, `automation`, `session_policy`, `respect_gitignore`; a dotted key sets a nested value — `retrieval.embeddings.enabled=auto`, `retrieval.embeddings.backend=sentence-transformers`, `compaction.enabled=false`, `linker.synth_input_max_chars=300000`, `models.module_summary=sonnet`, and a flow mapping fits a role with effort: `--set "models.synthesis={model: opus, reasoning_effort: high}"`); `--set-global key=value` (the same, but into the **global** personal-defaults config — effective with `--scope global` / `--project-only`); `--deps` (dependency groups); `--build` (build the structural graph immediately); `--no-verify` (skip the store check). The full list — `python install.py --help`.

**Fully by hand (no scripts).** In an environment where scripts cannot run, the engine is installed by copying: copy `skills/` and `agents/` into `<agent dir>/`, append the contents of `entrypoint/CLAUDE.md` into the entry point between the `<!-- AMG:BEGIN -->` / `<!-- AMG:END -->` markers, create `<agent dir>/amg/config.yml` (at minimum — `active`, `working_language`, `mirror_path`/`absorb_path`), and, if the environment has hooks, copy `entrypoint/settings.json` and `entrypoint/commands/amg.md`. That is exactly what `install.py` does for you.

## Local and global

| | Local | Global |
|---|---|---|
| Engine (`skills/`, `agents/`) | `<project>/.claude/` | `~/.claude/` (one for all projects) |
| Activation block | `<project>/CLAUDE.md` | the environment's **user-level entry**, with an **absolute** engine path: `~/.claude/CLAUDE.md`; Codex `~/.codex/AGENTS.md`; OpenCode `~/.config/opencode/AGENTS.md`; Qwen Code `~/.qwen/QWEN.md`; an unknown environment `~/AGENTS.md` |
| Graph, `config.yml` | `<project>/.claude/amg/` | **also** `<project>/.claude/amg/` — always local |

With a global install the engine is placed once, and every new project is connected **by its local config alone** — no engine copying:

```bash
python install.py --target /path/to/new-project --project-only --mirror src
```

**Two configuration layers.** A global install sets up, next to the engine, the **global personal-defaults config** — `~/<agent dir>/amg/config.yml`. The system reads it first, then the project's local config: the local one **overrides per key**, whatever is missing is inherited. The layers are split by what a setting belongs to:

- **the global layer — one machine's personal preferences:** the `models` block (model tiering per role) and the `retrieval.embeddings` block (the backend and whether semantic seeding is on). These should not be forced on a team through git, so with a global install the local config is written **without** them — they are inherited;
- **the local layer — the project's own:** `active`, the sources (`mirror_path` and the rest), `working_language`, `automation`, budgets, weights, compaction. The local `config.yml` is part of the project's canon (with "the graph in git" it is committed), and the team gets identical project settings.

Inheritance is switched on by the `agent_dir` key of the local config (the installer always writes it): it both names the environment's home directory and marks the config as installed; a minimal hand-written config without that key does not read the global layer. The `~/<agent dir>/amg/` directory is a config carrier only, **not a store**: no graphs are created there, and store resolution never picks it.

## After the install: activation and the first request

**`/amg on` ≠ building the graph.** Activation only raises the `active` flag; **the graph is built by `sync`** or by the activation loop.

**Restart the session first.** Skills and the `/amg` command register when a session starts, so the installing session does not see them — and the first build must not run there: a skill-less, improvised build loses the pipeline's orchestration discipline. In the **new** session:

- either the loop reconciles the graph before the first task (with `automation: true`);
- or say **`/amg sync`** (in words: "build / sync the memory graph");
- or, if you installed with `--build`, the structural skeleton is already in place — the new session's loop adds the semantic layer.

From here just work: glance at **`/amg status`** (one screen of state, and a tour of the `/amg` commands at the same time), then **ask the model any question about the project** — it assembles context from the graph itself. Manual build/retrieve commands from older instructions are no longer needed: the activation loop drives everything (see the [guide](docs/en/GUIDE.md)).

## Cheap to change later vs decide before building

Everything in `config.yml` may be edited at any time: nothing watches the file live — every operation reads it afresh, so a change takes effect on the **next run** (the next `/amg sync`, retrieval, or a new session's loop). What differs is the cost of changing your mind:

- **Cheap — applies on the next run:** adding mirror/absorb paths (the new content is simply ingested and derived at the normal incremental cost), excludes, `automation`, `session_policy`, tier budgets and other retrieval knobs, weights and compaction settings. Removing a mirror path purges its nodes on the next reconciliation — by design, the graph mirrors the sources.
- **Embeddings (`retrieval.embeddings`) — better decided before the first build.** Enabling them later is safe for seeding (node vectors are cached and re-encoded on demand), but the build-time linking pass nominates cross-domain candidates by these vectors: after enabling, re-run `/amg sync` so linking re-nominates what the lexical fallback missed.
- **`working_language` — decide before building.** Summaries and the derivation cache are keyed by the language; a later change re-summarizes nothing by itself (the graph drifts bilingual) and a clean switch costs a full re-derivation of the semantic layer.
- **`models` tiering — takes effect via reinstall.** The installer renders the block into the subagent definitions, so after editing it run the install again (a reinstall is safe and idempotent); already-written summaries stay as they are until their source changes.

## Reinstall

The mode is keyed to the config's existence, not to the wording (step 0 of the model-driven flow): a project with `<agent dir>/amg/config.yml` is reinstalled whatever the request said — with the values in force shown and only the changes asked about, never the full questionnaire. A reinstall is safe and idempotent: `install.py` updates **only** the `amg-*` skills and agents (your other skills in a shared `~/.claude` are untouched), replaces the block **only between the markers** (your instructions above it stay), merges hooks into `settings.json` **without clobbering** yours, and never rewrites an existing project `config.yml` wholesale — but keys **explicitly passed on the run** (`--set`, `--mirror`, `--absorb`, `--absorb-once`, `--exclude`) are applied to it surgically and echoed as `updated keys:`, so a changed answer lands instead of vanishing. Local graphs are untouched by a global reinstall. To reconfigure beyond that — edit `config.yml`, or delete it and install afresh.

## Uninstall

```bash
python install.py --target /path/to/project --uninstall                 # local
python install.py --target /path/to/project --uninstall --scope global  # the global engine
python install.py --target /path/to/project --uninstall --purge-graph   # remove the graph too
```

Uninstalling strips the block between the markers (your text above it stays) and removes every AMG artifact of **every** environment mode — the `amg-*` skills/agents, the native subagent renders (`.codex/agents`, `.opencode/agent`), the `/amg` command files, the OpenCode plugin, and the AMG hooks from every hooks carrier (`settings.json`, `.codex/hooks.json`) — foreign hooks and skills stay. **The graph is kept** unless `--purge-graph` is passed — memory is not lost by accident.

## Optional dependencies

The base path runs on the Python standard library (Python code is parsed with `ast`, no dependencies); anything missing is **skipped gracefully**. Groups (the `requirements.txt` file; the installer installs them per `--deps`):

| Group | What it gives | Packages |
|---|---|---|
| `base` | mandatory | `pyyaml` |
| `embeddings` | semantic seeding (light, static, multilingual) | `model2vec` |
| `embeddings-st` | semantic seeding (transformer: stronger on paraphrase, slower cold start) | `sentence-transformers` |
| `text` | PDF / DOCX / XLSX extraction | `pypdf` `python-docx` `openpyxl` |
| `treesitter` | code in other languages (functions + call edges) | `tree-sitter` `tree-sitter-language-pack` |

When a specific embeddings backend is the answer to question 7, pin it in the config too (`--set retrieval.embeddings.backend=…`) — the group only installs the package. The uplift is sized with `eval_retrieval.py --compare-embeddings`; **non-English** projects get a multilingual model by default; details in the [guide](docs/en/GUIDE.md). Install everything at once: `pip install -r requirements.txt`.

## Other environments (not Claude Code)

Memory is not tied to Claude Code. The engine scripts are plain Python that resolve the graph root themselves, so they work in any environment; the SKILL.md skill format itself is a cross-tool standard (Agent Skills), read by Codex, OpenCode, and Qwen Code alike. Only the control layer differs, and the **`--env`** flag picks it:

- **`--env claude-code`** (default) — the activation block with skills, the `SessionStart`/`SessionEnd`/`UserPromptSubmit` hooks in `settings.json`, and the `/amg` slash command.
- **`--env codex`** — OpenAI Codex, an environment **with** skills, subagents, and a core hooks engine: skills go to `.agents/skills`, subagents are rendered as **TOML in `.codex/agents`** (with `model` / `model_reasoning_effort` from the `models` block), the skill-aware block from the `entrypoint/AGENTS.codex.md` template is injected, and two hooks merge into **`.codex/hooks.json`** — SessionStart runs the whole deterministic start check and injects its note, UserPromptSubmit injects the gated "memory unconsulted" reminder. Codex runs unmanaged hooks **only after the user trusts them via `/hooks`** — the install flow ends with that step; a hook is a definition hash, so a reinstall that changes one re-requires the review. Codex has **no SessionEnd event** (the wrap-up signal stays the block's discipline) and **no custom-command surface** — the discoverable entry is the skills popup (`$`-completion). Preset: `.agents` / `AGENTS.md`.
- **`--env opencode`** — OpenCode, an environment **with** skills: it discovers the `amg-*` skills in `.agents/skills` **natively** (one of its standard skill locations), subagents are rendered to **`.opencode/agent/*.md`** (`mode: subagent`; a real model id from the `models` block passes through, a Claude alias is omitted), the portable skill-aware block from `entrypoint/AGENTS.skills.md` is injected, the `/amg` command lands in **`.opencode/command/amg.md`** (so `/amg` autocompletes), and the **AMG event plugin** lands in **`.opencode/plugin/amg.js`** — OpenCode has no hooks, but its JS plugin API covers the same ground event-driven: the start check on session creation, the gated reminder with each prompt, and a throttled **incremental transcript dump** (the dialogue is re-dumped as you work, so even a hard kill loses at most the last couple of minutes — richer than Claude Code's end-of-session dump). Preset: `.agents` / `AGENTS.md`.
- **`--env qwen`** — Qwen Code, an environment **with** skills, subagents, commands, **and hooks**: skills go to `.qwen/skills`, the worker prompts land in **`.qwen/agents/*.md`** — Qwen's native markdown subagents — with Claude-only frontmatter sanitized (the `tools`/`effort` fields dropped, a Claude-alias model dropped; a real id passes through — a native `qwen3-coder-plus` or a foreign provider in the colon form `openai:gpt-4o`), the same skill-aware block is injected into **`QWEN.md`**, the `/amg` command lands in **`.qwen/commands/amg.md`** (markdown with `{{args}}` — Qwen's recommended command format; its TOML commands are deprecated upstream), and the session hooks merge into `.qwen/settings.json` (Qwen reads a Claude-shaped `hooks` block, so healing, weight folding, and the transcript-dump attempt run automatically). Preset: `.qwen` / `QWEN.md`.
- **`--env generic`** — for an **unknown** environment that reads `AGENTS.md`: the installer injects a **portable skill-less block** from the `entrypoint/AGENTS.md` template and writes **no** hooks or command. The same memory loop runs by **direct script calls**, and the model reads the `agents/*.md` prompts as guidance (not as spawned agents). The skills still land in `.agents/skills` — the cross-tool location — and the block tells the model to prefer them when the environment turns out to discover them. Typical command:
  ```bash
  python install.py --target /path/to/project --env generic \
      --agent-dir .agents --entrypoint AGENTS.md --mirror src
  ```

The names `.claude` / `CLAUDE.md` are the Claude Code defaults; other environments use their own presets (above), and `--agent-dir` / `--entrypoint` override any of them. The baseline capability of the memory does not depend on the environment; the set of conveniences (hooks, slash commands, isolated subagents) does.

> ⚠️ **FULLY TESTED ON CLAUDE CODE; OPENCODE CONFIRMED ON LIVE RUNS.** The `opencode` mode has carried the full memory cycle on live field runs (its freshly added plugin and command await theirs); the `codex`, `qwen`, and `generic` modes are designed from each environment's verified surface, but their behavior and stability are **not yet confirmed live**. Finishing that verification is a roadmap stage in progress.

## Checking after the install

The installer itself finishes with `verify --repair` (store integrity). Additionally, from the project root, you can confirm everything is in place:

```
python .claude/skills/amg-bootstrap/scripts/extract_structure.py . --stats
```

`--stats` shows what will enter the graph **per source** (a source with `files: 0` or `found: false` is empty or its path was not found), what is still ambiguous by type, and which optional libraries are missing.
