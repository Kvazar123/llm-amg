#!/usr/bin/env python3
"""
verify_claims.py — lightweight, READ-ONLY verification of AMG facts against live source.

Confidently-wrong memory is worse than none: a summary written three refactors ago and
served as fact makes the model answer convincingly and incorrectly (roadmap §4.3). This
is the programmatic backbone of the "verify a code claim before you answer" rule
(SKILL amg-retrieve, Stage 2), promoted to the full provenance/verification layer of
Stage 13.

For each source-derived node it re-chunks the CURRENT source file (reusing the exact
ingest chunkers, so the content hash is computed identically) and compares:

  * file missing                         -> contradicted  (the source is gone)
  * unit id absent in the re-chunk       -> contradicted  (the symbol/section is gone)
  * unit content_sha != node.source_hash -> stale         (source changed since ingest)
  * unit content_sha == node.source_hash -> verified

By default it is READ-ONLY: it prints/returns verdicts and touches nothing, so the
read-only retriever can run it before answering. With --write it persists each verdict
into the node's `verification` block (status/method/last_verified_at[/commit]) under the
single-writer lock and refreshes the read-index — a maintenance/CI sweep. Authored and
synthesized nodes have no backing file and are skipped (their trust is `kind`, not a
source check).

Explicit ids are read one file at a time (the hot "before answer" path stays cheap on a
large graph — no full scan); a scope sweep (--all/--code with no ids) enumerates nodes.

CLI:
  python verify_claims.py [<id> ...] [--store <amg>] [--project <root>]
                          [--all | --code] [--write] [--json]

  default scope (no ids): --code (every source-derived CODE node). --all adds doc/data.
  --store is the graph root (default: resolve_amg_root); --project is the source root
  (default: cwd, where source_path is relative to).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                                            # retrieve (same skill)
sys.path.insert(0, str(HERE.parents[1] / "amg-bootstrap" / "scripts"))  # gs / rc / es

import graph_store as gs          # noqa: E402
import reconcile as rc            # noqa: E402
import extract_structure as es    # noqa: E402
import retrieve as R              # noqa: E402  (CODE_TYPES only)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

# Verdict statuses verify produces. The stored `verification.status` enum adds
# `unverified` — the never-checked default written at ingest, not a verify outcome.
_CHECKED = ("verified", "stale", "contradicted")
_BUCKETS = ("code", "doc", "data", "notes", "_hubs")


def _method_for(node: Dict[str, Any], unit: Optional[Dict[str, Any]]) -> str:
    """The verification method to record (roadmap enum grep|ast|test|user|doc|none): a
    Python code unit is parsed by ast; other code is matched at symbol level
    (grep-equivalent); a doc/data unit is a source-text match (doc)."""
    if node.get("type") in R.CODE_TYPES:
        return "ast" if (unit or {}).get("lang") == "python" else "grep"
    return "doc"


def _load_targets(store: gs.GraphStore, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Read just the requested nodes by probing their deterministic file path in each
    bucket — O(ids), not O(graph). The filename is the id hash (bucket-independent), so a
    node lives in exactly one bucket; the first that exists is it."""
    out: Dict[str, Dict[str, Any]] = {}
    for nid in ids:
        for bucket in _BUCKETS:
            rel = rc.node_relpath(nid, bucket)
            p = store.root / rel
            if not p.exists():
                continue
            meta = rc.parse_node(p.read_text(encoding="utf-8", errors="replace"))
            if meta and meta.get("id") == nid:
                meta["_path"] = rel
                out[nid] = meta
                break
    return out


def _scope_targets(nodes: Dict[str, Dict[str, Any]], scope: str) -> List[str]:
    """All source-derived nodes (scope 'all') or only the CODE ones (scope 'code')."""
    code_only = scope != "all"
    return [nid for nid, n in nodes.items()
            if n.get("source_kind") == "derived_from_file"
            and (not code_only or n.get("type") in R.CODE_TYPES)]


def verify_nodes(nodes_by_id: Dict[str, Dict[str, Any]], target_ids: List[str],
                 project_root: Path, config: Dict[str, Any],
                 amg_root: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Verify each target node against its live source. Groups by source_path so each
    file is re-chunked once. Returns {id: {status, method, reason?}}."""
    results: Dict[str, Dict[str, Any]] = {}
    by_path: Dict[str, List[str]] = defaultdict(list)
    for nid in target_ids:
        n = nodes_by_id.get(nid)
        if not n:
            results[nid] = {"status": "skipped", "method": "none", "reason": "no such node"}
        elif n.get("source_kind") != "derived_from_file" or not n.get("source_path"):
            results[nid] = {"status": "skipped", "method": "none",
                            "reason": "not source-derived (authored/synthesized)"}
        else:
            by_path[str(n["source_path"])].append(nid)
    for rel, ids in by_path.items():
        if not (project_root / rel).is_file():
            for nid in ids:
                results[nid] = {"status": "contradicted",
                                "method": _method_for(nodes_by_id[nid], None),
                                "reason": "source file is gone"}
            continue
        units = {u["id"]: u for u in es.units_for_file(project_root, rel, config, amg_root)}
        for nid in ids:
            n = nodes_by_id[nid]
            u = units.get(nid)
            method = _method_for(n, u)
            if u is None:
                results[nid] = {"status": "contradicted", "method": method,
                                "reason": "symbol/section no longer in the source"}
            elif u.get("content_sha") == n.get("source_hash"):
                results[nid] = {"status": "verified", "method": method}
            else:
                results[nid] = {"status": "stale", "method": method,
                                "reason": "source changed since the summary was derived"}
    return results


def verify(store_root: Path, project_root: Path, ids: Optional[List[str]] = None,
           scope: str = "code", write: bool = False) -> Dict[str, Any]:
    """Verify (and with write=True, persist) verification verdicts. The read path is
    lock-free (the retrieval invariant); the write path takes the single-writer lock and
    refreshes the read-index, like every other writer."""
    store = gs.GraphStore(store_root)
    amg_root = store.root
    config = es.load_config(amg_root)
    if ids:
        nodes = _load_targets(store, ids)               # cheap per-id read (hot path)
        targets = list(ids)
    else:
        nodes = rc.load_nodes(store)                    # full frontmatter scan for a sweep
        targets = _scope_targets(nodes, scope)
    results = verify_nodes(nodes, targets, project_root, config, amg_root)

    written = 0
    if write:
        commit = rc._git_commit(project_root)
        now = rc._now()
        with store.lock():
            store.recover()
            live = _load_targets(store, list(results)) if ids else rc.load_nodes(store)
            tx = store.transaction()
            for nid, verdict in results.items():
                if verdict["status"] not in _CHECKED:
                    continue
                n = live.get(nid)
                if not n:
                    continue
                meta = {k: v for k, v in n.items() if not k.startswith("_")}
                vblock: Dict[str, Any] = {"status": verdict["status"],
                                          "method": verdict["method"], "last_verified_at": now}
                if commit:
                    vblock["last_verified_commit"] = commit
                meta["verification"] = vblock
                tx.write(n["_path"], rc.serialize_node(meta, n.get("_body", "")))
                written += 1
            txid = tx.commit()
            if txid:
                rc._refresh_index(store.root, tx)       # keep the read-index in step
                store.append_log("verify_claims",
                                 f"verification stamped: {written} node(s)", txid)

    summary: Dict[str, int] = defaultdict(int)
    for v in results.values():
        summary[v["status"]] += 1
    return {"results": results, "summary": dict(summary), "written": written}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    args = list(argv[1:])
    write = "--write" in args
    as_json = "--json" in args
    scope = "all" if "--all" in args else "code"
    for flag in ("--write", "--json", "--all", "--code"):
        while flag in args:
            args.remove(flag)
    store_cli: Optional[str] = None
    if "--store" in args:
        i = args.index("--store"); store_cli = args[i + 1]; del args[i:i + 2]
    proj = Path.cwd()
    if "--project" in args:
        i = args.index("--project"); proj = Path(args[i + 1]).resolve(); del args[i:i + 2]
    ids = [a for a in args if not a.startswith("--")]
    store_root = Path(store_cli).resolve() if store_cli else gs.resolve_amg_root(start=proj)

    res = verify(store_root, proj, ids or None, scope, write)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    rank = {"contradicted": 0, "stale": 1, "verified": 2, "skipped": 3}
    for nid in sorted(res["results"], key=lambda n: (rank.get(res["results"][n]["status"], 9), n)):
        v = res["results"][nid]
        reason = f"  ({v['reason']})" if v.get("reason") else ""
        print(f"  {v['status']:<12} {v['method']:<6} {nid}{reason}")
    tail = f"  | written={res['written']}" if write else ""
    print(f"\nsummary: {dict(res['summary'])}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
