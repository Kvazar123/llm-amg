#!/usr/bin/env python3
"""
partition_queue.py — split the semantic work queue into per-subtree batches.

After `reconcile.py bootstrap` writes `work/queue.json` (the units awaiting semantic
derivation), a large queue is best derived as several parallel `amg-builder` subagents,
one per subtree, each in its own isolated context. This is the ready-made helper for
that split — instead of an ad-hoc one-liner (awkward on Windows): it groups the units
by the leading segments of their `source_path` and writes one `work/queue-<part>.json`
per group, each in the same `{generated, units}` shape the builder already reads (plus
a `part` label). Read-only over the graph; it only writes the batch files.

CLI:
  python partition_queue.py [<project_root>] [--root <agent_dir>] [--depth N]

`--depth` (default 2) is how many leading path segments form a batch key: depth 2
groups `src/billing/x.py` and `src/billing/y.py` together under `src/billing`; depth 1
would lump all of `src` into one batch. The graph root is resolved by
graph_store.resolve_amg_root (the same chain as reconcile/consolidate).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import graph_store as gs


def subtree_key(source_path: str, depth: int) -> str:
    """The batch key for a unit: the first `depth` directory segments of its
    source_path (the filename dropped). A file at the source root has no directory,
    so it groups under `_root`."""
    parts = [p for p in (source_path or "").replace("\\", "/").split("/") if p]
    dirs = parts[:-1]                       # drop the filename
    return "/".join(dirs[:depth]) or "_root"


def partition(amg_root: Path, depth: int = 2) -> Dict[str, int]:
    """Group work/queue.json units by subtree and write one work/queue-<part>.json per
    group. Returns {part: unit_count}. Empty (no queue / no units) -> {}."""
    qpath = amg_root / "work" / "queue.json"
    if not qpath.exists():
        return {}
    data = json.loads(qpath.read_text(encoding="utf-8"))
    units: List[Dict[str, Any]] = data.get("units", []) if isinstance(data, dict) else []
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for u in units:
        groups[subtree_key(str(u.get("source_path", "")), depth)].append(u)

    counts: Dict[str, int] = {}
    work = amg_root / "work"
    for key, group_units in groups.items():
        safe = key.replace("/", "_").replace("\\", "_") or "_root"
        gs.atomic_write_text(
            work / f"queue-{safe}.json",
            json.dumps({"generated": data.get("generated"), "part": key,
                        "units": group_units}, ensure_ascii=False, indent=2))
        counts[key] = len(group_units)
    return counts


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    depth = 2
    if "--depth" in args:
        i = args.index("--depth")
        depth = int(args[i + 1])
        del args[i:i + 2]
    project_root = Path(args[0]).resolve() if args else Path.cwd()
    amg_root = gs.resolve_amg_root(cli_root, project_root)
    counts = partition(amg_root, depth)
    print(json.dumps({"batches": counts, "total": sum(counts.values())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
