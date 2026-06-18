# AMG Consistency & Crash-Safety Model

This document specifies how the Associative Memory Graph stays **consistent** and
loses **nothing** when an operation is interrupted — a crash, a `/clear`, a closed
terminal, a killed process — at the beginning, middle, or end of any ingest,
reconcile, lint, consolidation, or logging step.

Everything here is implemented by `scripts/graph_store.py` (the transactional
store), `scripts/reconcile.py` (the diff engine), and the subagents. The store's
guarantees are exercised by `scripts/selftest_graph_store.py`.

## Contents
1. Mental model: the filesystem is the truth, the graph is a projection
2. Node classes and what is safe to delete
3. Identity and content hashing
4. On-disk layout and invariants
5. Atomic single-file writes
6. Transactions: the write-ahead journal
7. Recovery
8. Reconcile: the diff that converges
9. Locking and concurrency
10. Crash-point analysis
11. Logging
12. What is *not* auto-recoverable, and the mitigations
13. Operator commands

---

## 1. Mental model: the filesystem is the truth, the graph is a projection

`src/`, `doc/`, and `data/` are the source of truth for **what exists**. The graph
under `.claude/amg/` is a **derived projection** of them plus earned, model-made
knowledge. (`.claude` is the Claude Code default agent dir; another environment uses
its configured name, e.g. `.agents` — the store root is resolved, not hard-coded.) This single decision is what makes the system robust: because the graph
is derived, it can always be *reconciled back* to the truth. We never have to trust
the graph's own record of what exists — we recompute the diff against the files. In
the worst case (total graph loss) the structural graph can be rebuilt from source.

## 2. Node classes and what is safe to delete

Every node declares a `source_kind`. This is the rule that prevents reconcile from
deleting things it must not:

- **`derived_from_file`** — derived from a file in a `mirror` source (code, docs).
  Reconcile keeps it equal to its source: changed source updates it, deleted source
  **purges** it. The deliverable is the truth; the node is its shadow.
- **`authored`** — created from chat pasted into context, or from the model's own
  conclusions/decisions/forward plans. It has no backing file. Reconcile **never**
  purges it. Only consolidation may merge or retire it.
- **`synthesized`** — summary/overview/hub nodes the judgment layer derives from
  *other nodes*. Stored in `_hubs/`, rebuilt by consolidation, never purged by a
  source diff (their "source" is the graph itself).

Source `policy` reinforces this. `mirror` sources are projected bidirectionally.
`absorb` sources (e.g. `data/`) are ingested into `derived_from_file` nodes that
carry `policy: absorb` (not "authored notes"): while the source exists its changes
are re-reconciled, but **deleting an `absorb` source changes nothing** — the deletion
pass purges only `derived_from_file` + `policy: mirror` nodes, so survival is decided
by `policy`, not by `source_kind`. That is exactly the requested behavior for the
`data/` folder.

## 3. Identity and content hashing

- **Identity** is stable and path-derived, e.g. `code:src/db/pool.py::get_conn`,
  `doc:doc/db.md::pooling`. The same source unit always maps to the same node id.
- **`source_hash`** = sha256 of the unit's current source text.
- **`derived_from_hash`** = the source hash the node's *summary/edges* were derived
  from.

These three fields define a unit's state precisely:

| condition | meaning | action |
|---|---|---|
| node missing | newly added source | create node, queue for semantic derivation |
| `source_hash != content hash` | source changed | update structural fields, mark `stale`, re-queue |
| `derived_from_hash != source_hash` | summary is behind the code | re-derive (queued) |
| equal hashes | up to date | **do nothing** (no LLM call) |
| source gone, `derived_from_file` | orphan | purge |

The last two rows are why the system is cheap and idempotent: re-running bootstrap
on an unchanged repo does zero model work and produces zero changes.

## 4. On-disk layout and invariants

```
.claude/amg/
  config.yml          activation + sources + policies + tunables
  nodes/              one markdown file per node (YAML frontmatter + body)
    code/ doc/ data/ notes/ _hubs/
  work/queue.json     units awaiting semantic derivation
  work/derived-*.json subagent output awaiting apply
  journal/            write-ahead log; EMPTY when the store is at rest
  archive/            originals evicted by compaction (reversible)
  cache/pack.md       last assembled retrieval context pack (disposable)
  cache/embeddings.json  node embedding cache (disposable)
  log.md              human-readable action audit log (transactional, de-duped by txid, rotated)
  LOCK                single-writer lock (absent when no writer holds it)
```

**Invariant:** when no operation is running, `journal/` is empty and `LOCK` is
absent. Any leftover there after a restart is repaired by §7.

## 5. Atomic single-file writes

A node file is never seen half-written. `atomic_write_bytes` writes to a temp file
in the *same directory*, `fsync`s it, then `os.replace()`s it over the target (an
atomic rename on POSIX and within a Windows volume), then `fsync`s the directory. A
concurrent reader sees either the old bytes or the new bytes — never a torn file.

## 6. Transactions: the write-ahead journal

A logical change usually touches several files at once (a node plus the neighbors
whose inbound edges or memberships it rewrites). We must not let a crash apply only
some of them. The store uses a write-ahead journal with **declarative redo**
(record the desired end state, not a sequence of
deltas):

A `commit()` proceeds in four phases:

1. **Stage.** Write the desired content of every touched file as a content-addressed
   blob under `journal/<txid>/blobs/<sha>`.
2. **Record intent (the durability point).** Write `journal/<txid>/manifest.json`
   listing the ops (`write path sha` / `delete path`). Until this rename lands,
   **nothing** has touched the live store.
3. **Apply.** For each op, atomically place the blob at its target (skipping if the
   target already holds that content) or delete the target. Idempotent by hash.
4. **Commit & clean.** Write a `COMMITTED` marker, then remove the journal entry.

Because the journal records *target content*, replaying it any number of times
converges to the same state. No undo log is needed.

## 7. Recovery

`recover()` is safe to run anytime, repeatedly, and turns a crash into a non-event.
For each journal entry it inspects three durable signals:

- **No `manifest.json`** → intent never became durable, so nothing was applied →
  discard the entry. (Crash during phase 1–2.)
- **`manifest.json` present, no `COMMITTED`** → intent durable but apply may be
  partial → **re-apply** the manifest (idempotent), then mark committed and clean.
  (Crash during phase 3.)
- **`COMMITTED` present** → already applied → just clean up. (Crash during phase 4.)

`verify(repair=True)` runs recovery, clears a stale lock, and reports any dangling
path references it is handed. The whole graph is *derivable from the source files*,
so in the worst case (total graph loss) the structural skeleton is rebuilt by
`reconcile bootstrap` and the summaries/edges are re-derived.

## 8. Reconcile: the diff that converges

Reconcile makes the graph equal to the current `mirror` sources. It computes, per
unit, one of {added, changed, deleted, unchanged} (§3) and applies the minimal
mutation set in a single transaction:

- **added** → create a structural node (`status: stale`, `source_hash` set,
  `derived_from_hash: null`) and queue it for semantic derivation.
- **changed** → update structural fields and `source_hash`, set `status: stale`,
  re-queue — but **keep the existing summary and edges**. The old earned knowledge
  stays valid and visible until the new derivation is durably committed, so a crash
  mid-derivation loses nothing; it just leaves a node marked `stale`.
- **deleted** → purge, *only* if `source_kind == derived_from_file` and
  `policy == mirror`. `authored`/`absorb` notes are untouched.
- **unchanged** → nothing.

Running reconcile after *any* suspected drift — a crash, a manual edit, a new
session — repairs the graph. It is the universal self-heal, and it is idempotent:
the second run reports everything unchanged.

Semantic derivation is non-deterministic (the model may word a summary differently
each time), so it is **only invoked for queued units** (added/changed). Unchanged
content is never re-summarized, which bounds non-determinism to real changes and
keeps re-runs free.

## 9. Locking and concurrency

Two Claude Code sessions, or a subagent and the main agent, must not write at once.
Every write operation takes an exclusive `LOCK` (created with `O_CREAT|O_EXCL`),
holding pid/host/timestamp. A lock is *stolen* only if its owning pid is dead or it
is older than the staleness threshold. **Reads are lock-free** and always safe,
because each write is atomic per file (§5): a reader sees a coherent old or new
node, never a mixture.

## 10. Crash-point analysis

The explicit answer to "what happens if it dies at the beginning / middle / end":

| crash point | live-store state at crash | recovery action | guarantee |
|---|---|---|---|
| while staging blobs / before manifest durable | untouched | discard entry | no change, no loss |
| after manifest, mid-apply | some target files written | re-apply to target | all-or-nothing, no dup |
| after apply, before `COMMITTED` | fully applied | re-apply (no-ops) + commit | no change |
| after `COMMITTED`, before cleanup | fully applied | remove journal entry | no change |
| mid semantic derivation (LLM) | old node intact, `stale` | re-queue & re-derive | old knowledge kept |
| reconcile interrupted | partial txn in journal | recover + reconcile again | converges to source |

The self-test (`selftest_graph_store.py`) reproduces rows 1–4 by injecting a fault
mid-apply and asserting consistency after `recover()`.

## 11. Logging

`log.md` is a human-readable audit trail of completed actions, written through the
store's `append_log(source, msg, txid)` — **transactionally**, not by a raw append.
Each entry is `## [<ts>] <txid> <source> | ...`, staged as a single content-addressed
blob, so a replay during recovery is idempotent and an entry whose `txid` is already
present is a no-op (de-duplication by txid). The log is **bounded**: once it exceeds a
line cap the older lines are rotated into `archive/log-<ts>.md` and only the tail stays
live, so a long-lived graph never rewrites an unbounded file per write. Both writers use
it — `consolidate.py` and `reconcile.py` (the latter only when a diff actually changed
the graph, so an unchanged re-run logs nothing and stays idempotent). Writing is still
best-effort (any failure is swallowed): graph integrity rests on the journal, not on
`log.md`, so a missing audit line remains harmless. This implements the model "append as
part of a committed transaction with de-dup by txid" (audit 1.15, done at Stage 12).

## 12. What is *not* auto-recoverable, and the mitigations

Structural state (skeleton, hashes, import/`part_of` edges) is cheap to recompute
from source. **Earned state** — model-written summaries, semantic edges, and
learned weights — is expensive and cannot be regenerated identically. It is
protected by: the journal (every write is transactional), the keep-old-until-new
rule (§8), and periodic snapshots (the whole store is a git repo, so a tagged
commit after each consolidation is a restore point). If the graph is ever lost,
structural nodes and edges are rebuilt by `reconcile bootstrap`; the earned
summaries and semantic edges are restored from the latest snapshot or re-derived.

## 13. Operator commands

```
python scripts/graph_store.py recover          # replay any unfinished transaction
python scripts/graph_store.py verify --repair    # recover + clear stale lock + report
python scripts/reconcile.py  bootstrap .         # build/reconcile from any state
python scripts/reconcile.py  plan .              # diff + structural writes + work queue
python scripts/reconcile.py  apply derivation.json .   # apply semantic derivation
```

On any suspicion of inconsistency, the safe sequence is: `recover`, then
`verify --repair`, then `reconcile bootstrap` to re-establish equality with source.
