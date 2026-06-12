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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retrieve as R                       # reuse the same node loader

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def _arg(flag: str, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main() -> int:
    store = Path(_arg("--store", R._default_store()))
    grep = (_arg("--grep") or "").lower()
    bucket = _arg("--bucket")
    full = "--full" in sys.argv

    nodes = R.load_nodes(store)
    if not nodes:
        print(f"(no nodes under {store}/nodes)"); return 0

    rows = []
    for nid in sorted(nodes):
        n = nodes[nid]
        if bucket and not nid.startswith(bucket.rstrip("s") + ":") \
                and f"/{bucket}/" not in (n.get("source_path") or ""):
            # match by id prefix (code:/doc:/data:) or, for notes/_hubs, by type
            if not (bucket in ("notes", "_hubs") and n.get("type") in ("hub", "overview", "note")):
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
