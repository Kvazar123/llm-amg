# Installing AMG

AMG is installed **by the model** (you give it the path to this file; it asks the questions and sets everything up — section 1) or **manually** with the installer command (section 2). Either way one script does the work — `install.py`: it copies the engine, renders the activation block and hooks for your environment, writes `config.yml`, and verifies the store. Python 3 is required.

Русское зеркало этой инструкции — [INSTALL_RU.md](INSTALL_RU.md); исполняемый оригинал, который читает модель, — этот файл.

**Install from outside the project: keep the unpacked AMG folder out of the target project.** The installer runs from the unpacked AMG folder and points at the project with `--target`, so the folder itself may live anywhere — the home directory, a shared tools folder, wherever convenient. Do not put it **inside** the project being installed: a scatter of engine files clutters the project root for no benefit (the engine is copied into the agent directory anyway). Store resolution can never mistake the source folder for the memory store — it recognizes the folder by its content and never treats an AMG checkout as a graph. After the install the source folder is no longer needed and may be deleted.

**The folder's own `CLAUDE.md` is not part of the install.** The unpacked AMG folder carries a `CLAUDE.md` with development rules for AMG's own source code. If it ends up in your context (say, the folder was unpacked inside the project against the advice above), ignore it entirely: during an install, this file — INSTALL.md — is the only instruction that applies.

**The graph is always local.** It lives in `<project>/.claude/amg/` even when the engine is installed globally, because memory belongs to a specific project; there is no shared graph across projects.

**What goes where.** The *control plane* (the engine) is the `skills/` and `agents/` directories, the activation block in the entry point, the `settings.json` hooks, and the `commands/amg.md` command. The *content plane* (the graph itself) is created in `<project>/.claude/amg/`. The names `.claude` (agent directory) and `CLAUDE.md` (entry point) are the **Claude Code** defaults; another environment substitutes its own names, say `.agents` / `AGENTS.md` (see "Other environments" below).

## Section 1. Model-driven install

Unpack AMG into any folder **outside the project** and, in a session inside your project, tell the model: **"install AMG per `<path-to-AMG>/INSTALL.md`"** (for example, `~/tools/amg/INSTALL.md`). The model reads this file, surveys the project, asks the questions below (each with a short note), then runs `install.py` from that same folder with your answers.

**Survey the project before asking about sources.** List the project's top-level folders (and the notable file kinds in them) and **propose a classification**: which folders look like sources to mirror (code, documentation the user maintains), which look like one-shot material to absorb (chat logs, data dumps, third-party documents), and which deserve extra excludes (generated code, vendored assets — beyond the built-in ignore list). Present the proposal together with questions 3–5, so the user confirms or corrects a concrete list instead of recalling paths from memory.

**Ask every question, one by one.** Do not skip questions and do not collapse them into a single message. An empty answer takes the stated default — but the question must still be asked: silently defaulting the KEY items is how a graph gets built in the wrong language or without embeddings. Questions 6 and 7 are marked **KEY**: their answers are expensive to change after the graph is built (see "Cheap to change later vs decide before building").

**If memory is already installed** (the project has `<agent dir>/amg/config.yml`), the model first shows the values in force and asks what to keep and what to change: the installer **never overwrites** an existing config (and prints its values itself on the `in force:` line), so changes are made by editing `config.yml`, not by re-answering. Fresh-install questions:

1. **Local or global?** *(default: local.)* Local — the engine inside one project (`<project>/.claude/`); global — one engine for all projects (`~/.claude/`), while **each project still keeps its own graph**. A global install additionally sets up the **global personal-defaults config** — see "Local and global" below.
2. **Environment, agent directory, entry point?** *(default: Claude Code, `.claude` / `CLAUDE.md`.)* For OpenAI Codex — `--env codex` (skills + TOML subagents, preset `.agents` / `AGENTS.md`); for other environments that read `AGENTS.md` (Qwen Coder and the like) — `--env generic` (a portable skill-less block). Details — "Other environments" below.
3. **Mirrors (`mirror_path`)?** — offer the survey's proposal. What you edit (code, maintained documentation): the graph is kept equal to it — a change updates the node, a deletion purges it.
4. **Absorb (`absorb_path` / `absorb_once_path`)?** — offer the survey's proposal. One-shot material (logs, dumps, exported dialogues): deleting the source does not erase the knowledge. If a snapshot must never re-sync even when the original changes, name it frozen — it goes to `absorb_once_path` (flag `--absorb-once`). Absorb is optional — mirrors alone are fine; but **at least one source (`mirror_path` or `absorb_path`) is required** (if everything is empty, ask again).
5. **What to ignore?** — offer the survey's proposal. Glob patterns on top of the built-in defaults and `.gitignore` (written to `exclude`; for finer control there are `mirror_exclude` / `absorb_exclude` and the `respect_gitignore` switch — see [09-config](docs/en/architecture/09-config.md)).
6. **Working language (`working_language`)? — KEY.** *(default: by the project.)* The language of summaries and notes. Summaries and the derivation cache are **keyed by this language**: changing it later re-summarizes nothing by itself (only new and changed units get the new language — the graph drifts bilingual), and a clean switch means re-deriving the whole semantic layer at full model cost. Decide it now. For non-English projects the installer recommends a multilingual embedding model.
7. **Embeddings? — KEY.** Light `model2vec` (default), `sentence-transformers`, a custom model, or off. On consent the flow installs the backend and **enables seeding** — `--set retrieval.embeddings.enabled=auto` (the config template conservatively ships `off`; `auto` turns semantic seeding on as soon as a backend is installed and is harmless without one). Seeding itself heals later (vectors are re-encoded on demand), but the **build-time linking pass nominates cross-domain candidates by these vectors**: building without embeddings and enabling them afterwards means re-running `/amg sync` to gain the links it missed. Decide before the first build. For Cyrillic and other non-English languages a multilingual model is picked automatically (see "Optional dependencies").
8. **Automation (`automation`)?** *(default: `true`.)* With `false` the system acts only on `/amg …` commands or an explicit request.
9. **Session policy (`session_policy`)?** *(default: `absorb`.)* For valuable dialogues — `mirror` (every detail stays retrievable).
10. **Tier budgets?** *(default: 4000 / 10000 / 24000 tokens.)* The per-query output ceiling.
11. **Optional dependencies?** Which groups to install (see "Optional dependencies").
12. **Activate memory after the install?** The installer **never activates silently**. On consent it writes `active: true`. Separately you may **build the graph right away** (`--build`) — otherwise it is built before the first task of a new session or via `/amg sync` (see "After the install").

**Empty answers take the defaults**, except the sources (at least one `mirror_path`/`absorb_path` is needed) — those the installer re-asks. After the answers the model runs `install.py`, which places the engine, renders the block and hooks, writes `config.yml`, installs the chosen dependencies, and runs `verify --repair`. The graph is **not built by itself** — that is the activation step's decision. The AMG source folder may be deleted after the install.

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

Key flags: `--scope local|global`; `--env claude-code|codex|generic` (the environment — see "Other environments"); `--agent-dir` / `--entrypoint` (defaults `.claude` / `CLAUDE.md`; for Codex and generic — `.agents` / `AGENTS.md`); `--mirror` / `--absorb` / `--absorb-once` / `--exclude` (comma-separated lists); `--set key=value` (repeatable: `active`, `working_language`, `automation`, `session_policy`, `respect_gitignore`; a dotted key sets a nested value — `retrieval.embeddings.enabled=auto`); `--set-global key=value` (the same, but into the **global** personal-defaults config — effective with `--scope global` / `--project-only`); `--deps` (dependency groups); `--build` (build the structural graph immediately); `--no-verify` (skip the store check). The full list — `python install.py --help`.

**Fully by hand (no scripts).** In an environment where scripts cannot run, the engine is installed by copying: copy `skills/` and `agents/` into `<agent dir>/`, append the contents of `entrypoint/CLAUDE.md` into the entry point between the `<!-- AMG:BEGIN -->` / `<!-- AMG:END -->` markers, create `<agent dir>/amg/config.yml` (at minimum — `active`, `working_language`, `mirror_path`/`absorb_path`), and, if the environment has hooks, copy `entrypoint/settings.json` and `entrypoint/commands/amg.md`. That is exactly what `install.py` does for you.

## Local and global

| | Local | Global |
|---|---|---|
| Engine (`skills/`, `agents/`) | `<project>/.claude/` | `~/.claude/` (one for all projects) |
| Activation block | `<project>/CLAUDE.md` | `~/.claude/CLAUDE.md`, with an **absolute** engine path |
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

**`/amg on` ≠ building the graph.** Activation only raises the `active` flag; **the graph is built by `sync`** or by the activation loop. So after an install with activation:

- either **start a new session** — the loop reconciles the graph before the first task (with `automation: true`);
- or say **`/amg sync`** in the current session (in words: "build / sync the memory graph");
- or install with `--build` in the first place (the structural skeleton is ready the moment the install finishes).

From here just work: glance at **`/amg status`** (one screen of state, and a tour of the `/amg` commands at the same time), then **ask the model any question about the project** — it assembles context from the graph itself. Manual build/retrieve commands from older instructions are no longer needed: the activation loop drives everything (see the [guide](docs/en/GUIDE.md)).

## Cheap to change later vs decide before building

Everything in `config.yml` may be edited at any time: nothing watches the file live — every operation reads it afresh, so a change takes effect on the **next run** (the next `/amg sync`, retrieval, or a new session's loop). What differs is the cost of changing your mind:

- **Cheap — applies on the next run:** adding mirror/absorb paths (the new content is simply ingested and derived at the normal incremental cost), excludes, `automation`, `session_policy`, tier budgets and other retrieval knobs, weights and compaction settings. Removing a mirror path purges its nodes on the next reconciliation — by design, the graph mirrors the sources.
- **Embeddings (`retrieval.embeddings`) — better decided before the first build.** Enabling them later is safe for seeding (node vectors are cached and re-encoded on demand), but the build-time linking pass nominates cross-domain candidates by these vectors: after enabling, re-run `/amg sync` so linking re-nominates what the lexical fallback missed.
- **`working_language` — decide before building.** Summaries and the derivation cache are keyed by the language; a later change re-summarizes nothing by itself (the graph drifts bilingual) and a clean switch costs a full re-derivation of the semantic layer.
- **`models` tiering — takes effect via reinstall.** The installer renders the block into the subagent definitions, so after editing it run the install again (a reinstall is safe and idempotent); already-written summaries stay as they are until their source changes.

## Reinstall

A reinstall is safe and idempotent: `install.py` updates **only** the `amg-*` skills and agents (your other skills in a shared `~/.claude` are untouched), replaces the block **only between the markers** (your instructions above it stay), merges hooks into `settings.json` **without clobbering** yours, and **never overwrites** an existing project `config.yml`. Local graphs are untouched by a global reinstall. To reconfigure — edit `config.yml`, or delete it and install afresh.

## Uninstall

```bash
python install.py --target /path/to/project --uninstall                 # local
python install.py --target /path/to/project --uninstall --scope global  # the global engine
python install.py --target /path/to/project --uninstall --purge-graph   # remove the graph too
```

Uninstalling strips the block between the markers (your text above it stays), removes the `amg-*` skills/agents and the AMG hooks from `settings.json` (other hooks stay). **The graph is kept** unless `--purge-graph` is passed — memory is not lost by accident.

## Optional dependencies

The base path runs on the Python standard library (Python code is parsed with `ast`, no dependencies); anything missing is **skipped gracefully**. Groups (the `requirements.txt` file; the installer installs them per `--deps`):

| Group | What it gives | Packages |
|---|---|---|
| `base` | mandatory | `pyyaml` |
| `embeddings` | semantic seeding (light, multilingual) | `model2vec` |
| `text` | PDF / DOCX / XLSX extraction | `pypdf` `python-docx` `openpyxl` |
| `treesitter` | code in other languages (functions + call edges) | `tree-sitter` `tree-sitter-language-pack` |

The heavy embedding transformers — `pip install sentence-transformers` (an optional upgrade, sized with `--compare-embeddings`). **Non-English** projects need a multilingual model — the engine picks one by default; details in the [guide](docs/en/GUIDE.md). Install everything at once: `pip install -r requirements.txt`.

## Other environments (not Claude Code)

Memory is not tied to Claude Code. The engine scripts are plain Python that resolve the graph root themselves, so they work in any environment. Only the control layer differs, and the **`--env`** flag picks it:

- **`--env claude-code`** (default) — the activation block with skills, the `SessionStart`/`SessionEnd` hooks in `settings.json`, and the `/amg` slash command.
- **`--env codex`** — OpenAI Codex, an environment **with** skills and subagents: skills go to `.agents/skills`, subagents are rendered as **TOML in `.codex/agents`** (with `model` / `model_reasoning_effort` from the `models` block), and the skill-aware block from the `entrypoint/AGENTS.codex.md` template is injected; Claude hooks and the `/amg` command are not written — their role falls to the activation loop. The default agent directory and entry point are `.agents` / `AGENTS.md`.
- **`--env generic`** — for other environments that read `AGENTS.md` (Qwen Coder and others, no skills): the installer injects a **portable skill-less block** from the `entrypoint/AGENTS.md` template and writes **no** hooks or command (those are Claude Code mechanisms an environment may not have). The same memory loop runs by **direct script calls**, and the model reads the `agents/*.md` prompts as guidance (not as spawned agents). Typical command:
  ```bash
  python install.py --target /path/to/project --env generic \
      --agent-dir .agents --entrypoint AGENTS.md --mirror src
  ```

The names `.claude` / `CLAUDE.md` are the Claude Code defaults; other environments use their own (preset `.agents` / `AGENTS.md`). The baseline capability of the memory does not depend on the environment; the set of conveniences (hooks, slash commands, isolated subagents) does.

> ⚠️ **TESTED ON CLAUDE CODE ONLY.** The `codex` and `generic` modes are designed for their environments, but their behavior and stability on Codex / Qwen Coder and others are **not yet confirmed** — all testing so far was on Claude Code. Verifying those environments is a separate roadmap stage that follows the build-reliability stages.

## Checking after the install

The installer itself finishes with `verify --repair` (store integrity). Additionally, from the project root, you can confirm everything is in place:

```
python .claude/skills/amg-bootstrap/scripts/extract_structure.py . --stats
```

`--stats` shows what will enter the graph **per source** (a source with `files: 0` or `found: false` is empty or its path was not found), what is still ambiguous by type, and which optional libraries are missing.
