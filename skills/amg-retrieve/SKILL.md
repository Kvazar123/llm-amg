---
name: amg-retrieve
description: >-
  Assemble a focused context pack from the AMG graph BEFORE doing a coding or docs
  task, so the model sees the strategic surround (purpose, related code, prior
  decisions) plus the operational detail — without loading the whole project. USE
  THIS whenever AMG is active and the user gives a task scoped to part of the
  project: "work on / fix / extend / refactor X", "how does X work", "what touches
  X", "continue on the Y feature". Run it first, then work from the pack. It is
  read-only and safe. Also exposes an eval harness to measure retrieval recall and
  tune it. Triggers: any task naming a function/module/subsystem/feature; "pull
  context for", "what's relevant to", "retrieve before we start". Also renders a
  read-only 3D graph viewer (export_graph.py) on request: "open / show / visualize the
  memory graph".
---

# AMG Retrieve

Turn a task into a small, high-signal context pack by spreading activation over the
graph from the task as a query. Retrieval is **query-biased Personalized PageRank**:
a BM25 lexical pass builds the teleport vector (seeds + relevance), structural edges
carry the spread (so multi-hop neighbors that share no words with the query are still
reached), and the result is assembled greedily under per-tier token budgets. The
math and rationale are in `scripts/retrieve.py`.

Retrieval is **read-only** with respect to the graph: it never edits nodes or edges.
Its only side effects are writing the pack to `.claude/amg/cache/pack.md` and
appending a co-activation signal for the consolidation pass to fold in later.

## When to run
When AMG is active (`.claude/amg/config.yml` → `active: true`) and the user gives a
task scoped to part of the project. Do this **before** touching code or docs. Do not
dump the codebase into context; let activation pull the relevant subgraph.

## Workflow

Keep the main conversation clean by delegating the run to the read-only retriever
subagent, which works in its own context and returns the pack location plus a short
summary.

1. **Frame the query.** Use the user's task plus any concrete identifiers it names
   (function/module/feature). A query like `"extend charge retries in the billing
   card-charge flow"` seeds well. If embedding seeding is enabled (see below), close
   paraphrases also work; without it, seeds are purely lexical, so include the words
   that actually appear in the code/docs.

2. **Spawn `amg-retriever`** with the query and the store path. It runs:
   ```bash
   python .claude/skills/amg-retrieve/scripts/retrieve.py "<query>" \
       --store .claude/amg
   ```
   which writes `.claude/amg/cache/pack.md` and returns the ranked nodes. The
   subagent returns the pack path and a 3–5 line summary. For a **history/audit** query
   ("what was X before", "why was it changed") or a **contradictions** query ("show the
   conflicts") it adds `--intent history|conflict`, which surfaces retired/contradicted
   nodes that are otherwise pushed down (the subagent reads the intent from the query in
   any language — no keyword list).

3. **Read the pack** (`.claude/amg/cache/pack.md`) and work from it. The pack has
   four tiers: *Strategic* (overview/subsystem hubs), *Tactical* (relevant modules),
   *Operational* (code pointers + the docs/notes text in focus), *Related* (links to
   follow if needed). For code, the pack gives **pointers** (`path:line`) — open the
   real file in `src/` to edit; the graph is not a copy of the code.

4. If the pack misses something you expected, widen the query with more identifiers
   and re-run, or follow a *Related* link. (If misses are systematic, measure and
   tune — see below.)

## Lazy derivation: first touch is synchronous (only if `derivation: lazy`)

When `config.yml → derivation: lazy`, the graph may hold nodes that are not yet summarized
(a structural skeleton awaiting first use). `retrieve.py` reports these as `stale_in_pack`
(printed under `--- stale in pack ---`): the nodes a query just activated that are still
`stale`. Before working from the pack, **derive them**, so the activated node answers with a
real summary instead of an empty one — the lazy mechanism's first-touch guarantee:

1. take the `stale_in_pack` ids from the retriever's output;
2. spawn an `amg-builder` on just those units (their `work/queue.json` / `queue-deferred.json`
   entries) → a `derived-*.json`, and apply it (`reconcile.py apply ...`) — the same steps 3–4
   as bootstrap (see the `amg-bootstrap` skill);
3. re-read the pack (or re-run retrieve) and proceed.

Under the default `eager`, `stale_in_pack` is normally empty, so this is a no-op. This is
the read-side half of lazy derivation; the build-side (priority map + background fill) is in
the `amg-bootstrap` skill.

## Verify a code claim before you answer (cheap, mandatory)

The pack is **memory, not ground truth**: a summary can lag the source it points to
(refactors happen between consolidations). Confidently-wrong memory is worse than no
memory — the model answers convincingly and incorrectly. So before you state anything
about code on the strength of the pack, confirm it against the live source.

The pack already **flags** a node whose trust is in doubt with a `⟨…⟩` suffix: `stale`
(summary may lag), `unverified` (a code claim not yet checked against source),
`contradicted` (a check failed), `low confidence`. Treat any flag as a prompt to verify
before relying on that node.

`verify_claims.py` runs the check for you — read-only: the file exists, the symbol is
still there, and the content hash still matches what was summarized:

```bash
python .claude/skills/amg-retrieve/scripts/verify_claims.py \
    <node-id> [<node-id> ...] --store .claude/amg
```

It prints `verified` / `stale` / `contradicted` per node. On any conflict the **source
wins** over the summary (current code > a stale summary). A maintenance or CI sweep can
stamp the verdict back into the graph with `--write`; the default run touches nothing.

Verify the specific claims you are about to make — only the claims you actually use, not
the whole pack. This is the behavioral half of the trust layer; the schema fields
(`provenance`, `confidence`, `verification`) and the marking above carry the rest.

## Measuring and tuning recall

Retrieval quality is not a matter of taste — measure it. The eval harness compares
the AMG pack against a lexical (RAG-like) top-k baseline and reports recall,
precision, and **hop-recall** (recall on gold nodes that don't lexically match the
query — what spreading activation uniquely adds).

```bash
# Reproducible demo on a built-in labeled graph:
python .claude/skills/amg-retrieve/scripts/eval_retrieval.py --make-demo /tmp/amg-demo

# On your real graph, with your own labeled cases:
python .claude/skills/amg-retrieve/scripts/eval_retrieval.py \
    --store .claude/amg --cases .claude/skills/amg-retrieve/evals/cases.json --out results.json
```

`cases.json` is a list of `{"id", "query", "gold_ids": [...]}`. Label a handful of
real tasks with the node ids that *should* surface, then tune these knobs in
`config.yml → retrieval` against the numbers (never by feel):
`damping` (reach), `activation_threshold` (pack tightness), `token_budget` per tier,
and `relation_priors` (how strongly each edge type conducts).

## Visualize the graph (3D viewer)

To see the memory's *structure* — clusters, hubs, conflicts, what links to what — render
it as a 3D viewer. It is **read-only and offline**: ONE self-contained HTML with the graph
data and the library inlined, so it opens by double-click with no server and nothing
fetched (the graph can hold sensitive project knowledge).

```bash
python .claude/skills/amg-retrieve/scripts/export_graph.py --store .claude/amg --open
```

`--open` writes `.claude/amg/cache/graph.html` and opens it; omit it to just write the
file. `--json [path]` instead writes the raw `{nodes, links, meta}` for external graph
tooling (it is not needed by the viewer). Click a node for its full frontmatter and edges;
color is by bucket (the arbitration verdicts `disputed`/`rejected` highlighted,
`superseded`/`stale` dimmed), size by degree (hubs read large), with filters
(type/status/bucket), search (id/summary), a light/dark toggle, and — on a large graph — a
hubs-first mode that expands on click. Tunables (quality, `large_graph_mode`,
`large_graph_nodes`, raw 3d-force-graph `options`) live in `config.yml → viewer`.
Read-only w.r.t. the graph (writes only `cache/graph.html`). Triggers: "open / show /
visualize the memory graph" (in any language — match the meaning, not the words, but only
when the request explicitly refers to this memory / AMG).

## Reference
- `scripts/retrieve.py` — the retriever (importable `retrieve(...)` + CLI).
- `scripts/export_graph.py` — read-only export to a self-contained 3D HTML viewer (or `--json`).
- `scripts/embed.py` — OPTIONAL semantic seed enrichment (see below).
- `scripts/eval_retrieval.py` — recall/precision/hop-recall vs lexical baseline.
- `scripts/verify_claims.py` — verify a code claim against live source (file/symbol/hash);
  read-only by default, `--write` stamps the verification block.
- `evals/cases.json` — your labeled eval set (template provided).
- Subagent: `../../agents/amg-retriever.md`.

## Optional: embedding seed enrichment
By default seeding is lexical (BM25). If an embedding backend is installed, retrieval
also seeds by *meaning*, so paraphrased queries still light up the right nodes. It
blends into the teleport vector only — the PPR spread and the pack are unchanged —
so it is safe and its effect is measurable (run the eval with `embeddings.enabled`
off vs on). Enable in `config.yml` under `retrieval.embeddings`; install a backend
with `pip install model2vec` (light) or `pip install sentence-transformers`. With no
backend, retrieval silently stays pure-BM25. Node vectors are cached and recomputed
only when a node changes. Self-test: `scripts/selftest_embed.py`.
