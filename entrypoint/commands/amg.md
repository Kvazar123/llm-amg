---
description: AMG memory — one front door for every operation (status, version, on/off, repair, sync, retrieve, consult, consolidate, relink, view, help).
argument-hint: status | version | on | off | repair | sync | retrieve <query> | consult <query> | consolidate | relink | view | help
disable-model-invocation: true
allowed-tools: Bash(python *)
---

# /amg — AMG memory control

Read the first word of `$ARGUMENTS` (empty → `status`) and act on it. Match intent and
close synonyms, not the exact word.

**Control verbs** — run the script and report its result:

    python .claude/skills/amg-bootstrap/scripts/lifecycle.py <verb>

- `status` (state, info) — the one-screen report: engine version, active, automation,
  graph root, git branch/commit, node and stale counts, pending transactions, stale
  lock, merge conflicts, the semantic queue (units awaiting enrichment) and any
  recorded sync deferral, last sync, last pack, last consolidation and last judged
  pass (with an overdue note when the judgment half has lagged), connectivity
  verdict, eval summary. **Present the report verbatim, exactly as printed** — its
  layout is the contract; do not paraphrase or reorder it.
- `version` — the installed engine version.
- `on` (enable, activate, start) / `off` (disable, stop) — flip `active` in config.yml;
  confirm the new state. Note: `on` only enables AMG — building the graph is `sync`.
- `repair` (fix, heal, check) — recover + verify --repair + the store invariant audit
  (duplicate ids, path/id mismatches, a lying queue — report-only); confirm what was
  healed and relay the audit note if it flags anything.
- `help` — print the verb list (`lifecycle.py help`), or summarize this file.

**Work verbs** — use the matching skill (it orchestrates the scripts and subagents; a
hook or a deterministic script cannot, because these need model judgment):

- `sync` (build, bootstrap, index, reconcile, update) — use the **amg-bootstrap** skill
  to build or sync the graph from the configured source folders.
- `retrieve` (recall, context, pull) — use the **amg-retrieve** skill with the rest of
  `$ARGUMENTS` as the query, to assemble a context pack.
- `consult` (session-aware check, "check against memory") — spawn the
  **amg-retriever-fork** subagent with the rest of `$ARGUMENTS` as the ask (add the
  wanted judgment form when clear: a briefing / a delta / a contradiction check / a
  revision). It inherits this whole conversation, retrieves in its own window, and
  returns the informed distillate — what the memory adds / confirms / contradicts —
  without importing the pack. Rides on Claude Code's fork mechanism; no analog
  elsewhere.
- `consolidate` (maintain, compact, wrap up, save memory) — use the **amg-consolidate**
  skill to fold weights, file the session's conclusions, and compact over-budget branches.
- `relink` (re-link, link the isolated/unlinked nodes) — re-check exactly the stray
  nodes: run the **amg-bootstrap** skill's linking pass (step 6) starting from
  `link_candidates.py --isolated .` — it nominates candidates ONLY for nodes with no
  resolved relation and deliberately re-opens their past rejections; then the cycle
  as usual — judge every batch (amg-linker), apply with ONE
  `reconcile.py apply-derived .`, repeat until zero batches. Nomination alone
  applies nothing. Use when `status`/the viewer shows isolated nodes after a
  completed build; a plain `sync` will honestly report "nothing new" there, because
  its convergence memory (`work/judged/`) already ruled on those pairs.

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
