---
description: AMG memory — one front door for every operation (status, on/off, repair, sync, retrieve, consolidate, view).
argument-hint: status | on | off | repair | sync | retrieve <query> | consolidate | view
disable-model-invocation: true
allowed-tools: Bash(python *)
---

# /amg — AMG memory control

Read the first word of `$ARGUMENTS` (empty → `status`) and act on it. Match intent and
close synonyms, not the exact word.

**Control verbs** — run the script and report its result:

    python .claude/skills/amg-bootstrap/scripts/lifecycle.py <verb>

- `status` (state, info) — the one-screen report: active, automation, graph root, git
  branch/commit, node and stale counts, pending transactions, stale lock, merge
  conflicts, queue size, last pack, last consolidation and last judged pass (with an
  overdue note when the judgment half has lagged), connectivity verdict, eval
  summary. Present it as-is.
- `on` (enable, activate, start) / `off` (disable, stop) — flip `active` in config.yml;
  confirm the new state. Note: `on` only enables AMG — building the graph is `sync`.
- `repair` (fix, heal) — recover + verify --repair; confirm what was healed.

**Work verbs** — use the matching skill (it orchestrates the scripts and subagents; a
hook or a deterministic script cannot, because these need model judgment):

- `sync` (build, bootstrap, index, reconcile, update) — use the **amg-bootstrap** skill
  to build or sync the graph from the configured source folders.
- `retrieve` (recall, context, pull) — use the **amg-retrieve** skill with the rest of
  `$ARGUMENTS` as the query, to assemble a context pack.
- `consolidate` (maintain, compact, wrap up, save memory) — use the **amg-consolidate**
  skill to fold weights, file the session's conclusions, and compact over-budget branches.

**View verb** — a deterministic read-only script (no subagent, no judgment), so run it
directly:

    python .claude/skills/amg-retrieve/scripts/export_graph.py --store .claude/amg --open

- `view` (graph, show graph, visualize, open graph) — export the graph to ONE
  self-contained, offline HTML and open it in the browser. Read-only; it writes only
  `cache/graph.html`. Omit `--open` to just write the file; `--json` writes the raw
  `{nodes, links, meta}` for external tooling. Also available via the **amg-retrieve** skill.

Each work verb is also directly available as its own skill — `/amg-bootstrap`,
`/amg-retrieve`, `/amg-consolidate`. `.claude` is the Claude Code default agent dir; the
installer adjusts these paths for other environments.
