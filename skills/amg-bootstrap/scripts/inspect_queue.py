#!/usr/bin/env python3
"""
inspect_queue.py — a read-only summary of the semantic work queue + build progress.

After `reconcile.py bootstrap` writes `work/queue.json`, this shows its shape at a
glance so you can decide how to derive it: how many units in total, the spread by
category (code / doc / data), by unit kind (module / function / section / record / …),
by top-level subtree, and how many units carry their `text` inline (nearly all —
the builder summarizes from the queue without re-opening sources; only oversized
units fall back to the pointer). The `progress` block reports how far the build has
come over the GRAPH — derived vs still-stale nodes as a percentage — so the
orchestrator and the user see the traveled part of the path after every apply round
(stage 20, audit 1.48). It is the counterpart to partition_queue.py (which does the
actual split) and a sibling of inspect_graph.py (which browses the built graph).
Nothing is written.

CLI:
  python inspect_queue.py [<project_root>] [--root <agent_dir>]

The graph root is resolved by graph_store.resolve_amg_root (the same chain as
reconcile/consolidate).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import graph_store as gs
import reconcile as rc                        # node loader for the progress block
from partition_queue import subtree_key       # reuse the same subtree grouping


def _progress(amg_root: Path) -> Dict[str, Any]:
    """Derivation progress over the graph: nodes whose semantic layer is done vs
    still stale. A scan, not the read-index — a progress ping between apply rounds
    is rare enough that correctness-simplicity wins."""
    total = stale = 0
    if (amg_root / "nodes").exists():
        nodes = rc.load_nodes(gs.GraphStore(amg_root))
        total = len(nodes)
        stale = sum(1 for n in nodes.values() if n.get("status") == "stale")
    pct = round(100.0 * (total - stale) / total, 1) if total else 0.0
    return {"nodes_total": total, "derived": total - stale, "stale": stale,
            "derived_percent": pct}


def summarize(amg_root: Path) -> Dict[str, Any]:
    """Counts over work/queue.json plus the graph progress block. `queue: None`
    (with progress still present) when there is no queue file."""
    qpath = amg_root / "work" / "queue.json"
    if not qpath.exists():
        return {"queue": None, "progress": _progress(amg_root)}
    data = json.loads(qpath.read_text(encoding="utf-8"))
    units: List[Dict[str, Any]] = data.get("units", []) if isinstance(data, dict) else []
    by_category: Dict[str, int] = defaultdict(int)
    by_kind: Dict[str, int] = defaultdict(int)
    by_subtree: Dict[str, int] = defaultdict(int)
    with_text = 0
    for u in units:
        by_category[str(u.get("category", "?"))] += 1
        by_kind[str(u.get("kind", "?"))] += 1
        by_subtree[subtree_key(str(u.get("source_path", "")), 1)] += 1
        if u.get("text"):
            with_text += 1
    return {"total": len(units), "generated": data.get("generated") if isinstance(data, dict) else None,
            "by_category": dict(by_category), "by_kind": dict(by_kind),
            "by_subtree": dict(by_subtree), "with_text": with_text,
            "progress": _progress(amg_root)}


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    project_root = Path(args[0]).resolve() if args else Path.cwd()
    amg_root = gs.resolve_amg_root(cli_root, project_root)
    print(json.dumps(summarize(amg_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
