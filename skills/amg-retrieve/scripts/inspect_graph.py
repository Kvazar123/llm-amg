#!/usr/bin/env python3
"""
inspect_graph.py - browse the AMG graph to see what's in it and pick gold_ids for
eval cases. Read-only; prints each node's id, type, and summary.

  python inspect_graph.py                          # all nodes, summaries truncated
  python inspect_graph.py --grep роутинг           # only nodes whose id/summary matches
  python inspect_graph.py --bucket doc             # only nodes/doc
  python inspect_graph.py --grep controller --full # full summaries

The id printed for each node is exactly what goes in a case's "gold_ids".
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retrieve as R                       # reuse the same node loader

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass


def _arg(flag: str, default: Optional[str] = None) -> Optional[str]:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def _in_bucket(node: Dict[str, Any], bucket: str) -> bool:
    """True if the node's file lives in nodes/<bucket>/. Filters by the REAL on-disk
    bucket directory (code / doc / data / notes / _hubs) taken from the node's path,
    not a guessed id prefix — so notes/_hubs (which have no id-prefix) also match."""
    parts = (node.get("_path") or "").split("/")     # nodes/<bucket>/<file>.md
    return len(parts) > 1 and parts[1] == bucket


def main() -> int:
    store = Path(_arg("--store") or str(R._default_store()))
    grep = (_arg("--grep") or "").lower()
    bucket = _arg("--bucket")
    full = "--full" in sys.argv

    nodes = R.load_nodes(store)
    if not nodes:
        print(f"(no nodes under {store}/nodes)"); return 0

    rows = []
    for nid in sorted(nodes):
        n = nodes[nid]
        if bucket and not _in_bucket(n, bucket):
            continue
        summary = (n.get("summary") or "").replace("\n", " ").strip()
        if grep and grep not in nid.lower() and grep not in summary.lower():
            continue
        rows.append((nid, n.get("type", "node"), summary))

    for nid, typ, summary in rows:
        shown = summary if full else (summary[:110] + ("…" if len(summary) > 110 else ""))
        print(f"{nid}\n    [{typ}] {shown or '(no summary yet — node is stale)'}")
    print(f"\n{len(rows)} node(s)"
          + (f' matching \"{grep}\"' if grep else "")
          + (f" in {bucket}" if bucket else "")
          + f"; {len(nodes)} total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
