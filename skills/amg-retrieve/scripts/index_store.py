#!/usr/bin/env python3
"""
index_store.py — a DISPOSABLE SQLite read-index for retrieve.load_nodes.

Markdown under nodes/ is the canon (roadmap §4.1); this index is a generated,
fully-rebuildable cache that exists only to make the per-query load fast on a large
graph. Today load_nodes scans nodes/*.md and yaml.safe_loads every file — fine for a
few thousand nodes, slow for tens of thousands. The index stores the already-parsed,
already-assembled per-node fields so a query reads one SQLite table instead of N
files. If the index is ever missing, stale, or corrupt, callers fall back to the scan
— it never returns a wrong result, only a slower path. "Broke? delete it" works: the
next read (or the next writer) rebuilds it.

Freshness is a CHEAP stat-walk signature of nodes/ (relpath, mtime, size) stored in
the index; a query compares it to a fresh walk and trusts the index only on a match.
The signature lives INSIDE the SQLite file (a meta row), so the data and its freshness
tag are written atomically together — no separate sidecar to desync.

Lifecycle:
  * read_if_fresh(amg_root)            -> nodes dict, or None when stale/absent/broken
  * build(amg_root, nodes, sig)        -> (re)write the whole index from a scanned set
  * refresh_after_commit(amg_root, w, d) -> incrementally upsert/delete the nodes a
                                          writer just changed (cheap; keeps it warm)

The node-dict SHAPE is identical to retrieve.load_nodes (it reuses retrieve's
_node_from_meta / _parse), so BM25 / build_adjacency / assemble_pack never know the
source. This module is import-safe with retrieve: retrieve imports index_store only
lazily (inside load_nodes), so this top-level `import retrieve` makes no cycle.
"""
from __future__ import annotations

import json
import sqlite3
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import retrieve as R          # safe: retrieve imports index_store lazily, not at top

INDEX_REL = "cache/index.sqlite"
_BUSY_MS = 2000               # let a brief concurrent sqlite lock retry, then give up
_COLS = ("id", "type", "status", "summary", "source_path", "lineno", "line_end",
         "confidence", "text", "edges", "part_of", "verification", "body", "relpath")


def _index_path(amg_root: Path) -> Path:
    return amg_root / INDEX_REL


def signature(amg_root: Path) -> str:
    """A cheap freshness fingerprint of nodes/: a hash over (relpath, mtime_ns, size)
    for every nodes/*.md, sorted. Detects any add / change / delete WITHOUT reading or
    parsing a file — 10-100x cheaper than the load it guards. `empty` when nodes/ is
    absent (a graph with no nodes still has a stable signature)."""
    nodes_dir = amg_root / "nodes"
    if not nodes_dir.exists():
        return "empty"
    h = hashlib.sha256()
    for p in sorted(nodes_dir.rglob("*.md")):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(p.relative_to(nodes_dir).as_posix().encode("utf-8"))
        h.update(f"|{st.st_mtime_ns}|{st.st_size}|".encode("ascii"))
    return h.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
    return con


def _node_to_row(node: Dict[str, Any]) -> List[Any]:
    """Serialize a node dict to a row in `_COLS` order. edges/part_of are JSON; the
    bag-of-words `text` is stored verbatim (tokens are re-derived cheaply on read)."""
    return [
        node["id"], node.get("type"), node.get("status"), node.get("summary", ""),
        node.get("source_path"), node.get("lineno"), node.get("line_end"),
        node.get("confidence"), node.get("text", ""),
        json.dumps(node.get("edges") or [], ensure_ascii=False),
        json.dumps(node.get("part_of") or [], ensure_ascii=False),
        json.dumps(node.get("verification") or {}, ensure_ascii=False),
        node.get("body", ""), node.get("_path"),
    ]


def _row_to_node(row: Any) -> Dict[str, Any]:
    """Reconstruct the load_nodes-shaped dict from an index row, re-tokenizing `text`
    with retrieve's own WORD_RE so the BM25 bag matches the scan byte-for-byte."""
    (nid, typ, status, summary, source_path, lineno, line_end, confidence,
     text, edges_j, part_of_j, verification_j, body, relpath) = row
    return {
        "id": nid, "type": typ or "node", "source_path": source_path, "lineno": lineno,
        "line_end": line_end, "confidence": confidence,
        "summary": summary or "", "status": status,
        "edges": json.loads(edges_j) if edges_j else [],
        "part_of": json.loads(part_of_j) if part_of_j else [],
        "verification": json.loads(verification_j) if verification_j else {},
        "body": body or "", "text": text or "",
        "tokens": [w.lower() for w in R.WORD_RE.findall(text or "")],
        "_path": relpath,
    }


def read_if_fresh(amg_root: Path) -> Optional[Dict[str, Dict[str, Any]]]:
    """Return the full node set from the index iff it exists and its stored signature
    matches nodes/ right now; otherwise None (caller scans). Any sqlite error -> None
    (a corrupt index degrades to the scan, never to a wrong result)."""
    ipath = _index_path(amg_root)
    if not ipath.exists():
        return None
    sig = signature(amg_root)
    con: Optional[sqlite3.Connection] = None
    try:
        con = _connect(ipath)
        cur = con.execute("SELECT value FROM meta WHERE key='signature'")
        got = cur.fetchone()
        if not got or got[0] != sig:
            return None
        cols = ", ".join(_COLS)
        rows = con.execute(f"SELECT {cols} FROM nodes").fetchall()
        return {r[0]: _row_to_node(r) for r in rows}
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            con.close()


def build(amg_root: Path, nodes: Dict[str, Dict[str, Any]], sig: str) -> bool:
    """(Re)write the whole index from an already-scanned node set, tagged with `sig`.
    In-place under one transaction (DROP+CREATE+INSERT+sig): a reader sees the old
    snapshot until commit, never a half-built one, and there is no cross-volume file
    rename to fail on Windows. Best-effort: returns False on any error (the caller
    keeps the scanned nodes; the next read just rebuilds)."""
    ipath = _index_path(amg_root)
    con: Optional[sqlite3.Connection] = None
    try:
        ipath.parent.mkdir(parents=True, exist_ok=True)
        con = _connect(ipath)
        con.execute("DROP TABLE IF EXISTS nodes")
        con.execute("DROP TABLE IF EXISTS meta")
        con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, status TEXT, "
                    "summary TEXT, source_path TEXT, lineno INTEGER, line_end INTEGER, "
                    "confidence REAL, text TEXT, edges TEXT, part_of TEXT, "
                    "verification TEXT, body TEXT, relpath TEXT)")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        ph = ", ".join("?" * len(_COLS))
        con.executemany(f"INSERT INTO nodes ({', '.join(_COLS)}) VALUES ({ph})",
                        [_node_to_row(n) for n in nodes.values()])
        con.execute("INSERT INTO meta (key, value) VALUES ('signature', ?)", (sig,))
        con.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        if con is not None:
            con.close()


def refresh_after_commit(amg_root: Path, written: List[str], deleted: List[str]) -> bool:
    """Incrementally fold a writer's just-committed node changes into an EXISTING index
    and re-stamp the signature — all in one transaction (atomic: either the rows and
    the new signature land together, or nothing does and the old signature makes the
    next read rebuild). Cheap: it parses only the changed files, not the whole graph
    (the point for frequent single-node writes like notes.add_note). No index yet ->
    no-op (the reader builds it lazily). Call under the writer's lock so the signature
    matches disk. Best-effort: any error -> False, leaving the old (consistent) index."""
    ipath = _index_path(amg_root)
    if not ipath.exists():
        return False                          # nothing to upsert; reader will build
    con: Optional[sqlite3.Connection] = None
    try:
        con = _connect(ipath)
        for rel in deleted:
            con.execute("DELETE FROM nodes WHERE relpath = ?", (rel,))
        ph = ", ".join("?" * len(_COLS))
        for rel in written:
            p = amg_root / rel
            if not p.exists():
                continue
            parsed = R._parse(p.read_text(encoding="utf-8", errors="replace"))
            if not parsed:
                continue
            meta, body = parsed
            node = R._node_from_meta(meta, body, rel)
            if node is None:
                continue
            con.execute(f"INSERT OR REPLACE INTO nodes ({', '.join(_COLS)}) "
                        f"VALUES ({ph})", _node_to_row(node))
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('signature', ?)",
                    (signature(amg_root),))
        con.commit()
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        if con is not None:
            con.close()
