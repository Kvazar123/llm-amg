#!/usr/bin/env python3
"""
migrate_schema.py — one-shot, idempotent schema migration to the data-model canon.

Brings graphs built before the data-model canon to
the current schema. Transforms, all schema-only (files stay in their buckets):

  * source_kind: derived  -> synthesized   (the normalized taxonomy)
  * type: derived         -> hub, or overview when the id tail contains
                             "overview" (both land in the strategic tier; the
                             distinction is reported so the user can adjust)
  * tree-sitter grammar kinds (function_definition, class_declaration, ...)
                          -> canonical function/class via extract_structure._TS_DEF
  * edges without origin  -> imports/calls -> structural (the only rels
                             extraction ever produced); edges owned by a
                             synthesized node -> synthesized; else semantic
  * missing provenance    -> {kind} inferred from source_kind/id prefix
  * missing verification  -> {status: unverified, method: none} (conservative: a
                             migration must not fabricate a verification event)

`lineno`/`qualname`/`line_end` are NOT restored here: run `reconcile.py bootstrap .`
right after — its pointer-drift branch refreshes lineno/qualname/line_end from the
sources for free (no re-derivation) wherever the source unit still exists.

All writes go through one graph_store transaction under the writer lock, so
the migration is crash-safe and re-running it is a no-op.

Usage:
  python migrate_schema.py [<project_root>] [--root <agent_dir>]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import graph_store as gs
from extract_structure import _TS_DEF
from reconcile import load_nodes, serialize_node, _now, _refresh_index


def _infer_kind(node: Dict[str, Any]) -> str:
    """Provenance kind for an old node lacking one: authored decisions/ADRs are the
    human's (user), other authored and all synthesized nodes are the model's inference;
    a file-projected node takes its id-prefix domain (code/doc/data)."""
    sk = node.get("source_kind")
    if sk == "authored":
        return "user" if (node.get("type") or "").lower() in ("decision", "adr") else "model_inference"
    if sk == "synthesized":
        return "model_inference"
    prefix = str(node.get("id", "")).split(":", 1)[0]
    return prefix if prefix in ("code", "doc", "data") else "doc"


def migrate(project_root: Path, amg_root: Optional[Path] = None) -> Dict[str, Any]:
    amg_root = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg_root)
    store.init()
    counts: Dict[str, Any] = {"source_kind_normalized": 0, "hub_types_fixed": 0,
              "kinds_canonicalized": 0, "edges_origin_backfilled": 0,
              "provenance_backfilled": 0, "verification_backfilled": 0,
              "nodes_updated": 0}
    overviews: List[str] = []

    with store.lock():
        store.recover()
        nodes = load_nodes(store)
        tx = store.transaction()
        for nid, node in nodes.items():
            changed = False

            if node.get("source_kind") == "derived":
                node["source_kind"] = "synthesized"
                counts["source_kind_normalized"] += 1
                changed = True

            if node.get("type") == "derived":
                tail = nid.split(":", 1)[-1].lower()
                node["type"] = "overview" if "overview" in tail else "hub"
                if node["type"] == "overview":
                    overviews.append(nid)
                counts["hub_types_fixed"] += 1
                changed = True
            elif node.get("type") in _TS_DEF:
                node["type"] = _TS_DEF[node["type"]]
                counts["kinds_canonicalized"] += 1
                changed = True

            synthesized = node.get("source_kind") == "synthesized"
            for e in node.get("edges") or []:
                if not isinstance(e, dict) or e.get("origin"):
                    continue
                if e.get("rel") in ("imports", "calls"):
                    e["origin"] = "structural"
                elif synthesized:
                    e["origin"] = "synthesized"
                else:
                    e["origin"] = "semantic"
                counts["edges_origin_backfilled"] += 1
                changed = True

            if "provenance" not in node:
                node["provenance"] = {"kind": _infer_kind(node)}
                counts["provenance_backfilled"] += 1
                changed = True
            if "verification" not in node:
                node["verification"] = {"status": "unverified", "method": "none"}
                counts["verification_backfilled"] += 1
                changed = True

            if changed:
                node["updated"] = _now()
                meta = {k: v for k, v in node.items() if not k.startswith("_")}
                tx.write(node["_path"], serialize_node(meta, node.get("_body", "")))
                counts["nodes_updated"] += 1
        txid = tx.commit()
        if txid:
            _refresh_index(amg_root, tx)       # warm the read-index under the lock

    counts["overview_ids"] = overviews
    return counts


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    project_root = Path(args[0]).resolve() if args else Path.cwd()
    amg_root = gs.resolve_amg_root(cli_root, project_root)
    print(json.dumps(migrate(project_root, amg_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
