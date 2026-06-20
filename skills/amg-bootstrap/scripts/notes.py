#!/usr/bin/env python3
"""
notes.py — safe capture of AUTHORED nodes into the AMG graph. Crash-safe & idempotent.

This is the cheap, broad "hippocampal" capture of the Complementary Learning Systems
model (THEORY §2, §13): during a working session the model records what it concluded —
a decision, a conclusion, an open question, a forward plan — as an authored node,
WITHOUT hand-editing files under nodes/. Selection, weighting and promotion happen
later, at consolidation, when the full context is available.

Why this is safe across a later bootstrap: an authored node is written with
source_kind=authored + policy=authored. The reconcile deletion and move passes only
ever touch derived_from_file + mirror nodes, so a note is NEVER purged by a source
diff — deleting the data/ folder must not lose a conclusion the model reasoned out.
Every write goes through a graph_store transaction (recover() first, atomic commit),
so an interruption mid-capture heals on the next `graph_store.py recover`.

Supported types (the node's SHAPE; origin is always authored):
  note          a working note / conclusion          (episodic)
  decision      a settled decision                    (protected at consolidation)
  adr           an architecture decision record       (protected at consolidation)
  open_question an unresolved question to revisit      (episodic)
  plan          a forward-looking plan                 (episodic)

Identity: `note:<slug>-<hash8>` — slug from the summary (or first tag), hash content-
addressed over (type|summary|body|tags). Capturing the same content twice updates the
one node instead of spawning a duplicate. The namespace is `note:` for every type (not
`<type>:`) so a later promote (e.g. open_question -> decision) that changes `type`
leaves the immutable id honest. Pass --id to keep a stable id for a living node you
revise across sessions (a plan, an open question).

CLI:
  python notes.py add --type decision --summary "..." [--body "..."]
                      [--tags "routing,controllers"] [--status captured|active]
                      [--part-of '<json list of {topic,w}>']
                      [--edges '<json list of {rel,to,w}>'] [--id <id>]
                      [<project_root>] [--root <agent_dir>]

The graph root is <agent_dir>/amg, resolved by graph_store.resolve_amg_root (same
chain as reconcile/consolidate: --root -> AMG_AGENT_DIR -> config search upward ->
engine location -> default .claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import graph_store as gs
import reconcile as rc

try:
    import yaml
except ImportError:                       # pragma: no cover
    sys.stderr.write("notes.py needs PyYAML: pip install pyyaml\n")
    raise

# Windows consoles default to cp1252; force UTF-8 so Cyrillic summaries print.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

# The authored capture types. `type` is the node's shape; decision/adr are protected
# at consolidation (compaction.protect_types), the rest are episodic (episodic_types).
NOTE_TYPES = ("note", "decision", "adr", "open_question", "plan")


def _slug(text: str) -> str:
    """Human-readable id slug from free text: words joined by '-', lowercased, <=40."""
    return re.sub(r"[^\w]+", "-", (text or "").strip().lower()).strip("-")[:40].strip("-")


def _make_id(ntype: str, summary: str, body: str, tags: List[str]) -> str:
    """Content-addressed id: stable slug + 8-hex hash of the note's content, so an
    identical re-capture maps to the same node (no duplicate) while distinct content
    gets a distinct node — mirrors how derived nodes are addressed by source_hash."""
    base = _slug(summary) or (_slug(tags[0]) if tags else "") or "note"
    h = gs.sha256_text("\n".join([ntype, summary or "", body or "",
                                  ",".join(sorted(tags))]))[:8]
    return f"note:{base}-{h}"


def _stamp_edges(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize authored edges: keep {rel,to,w,coact,origin}; origin defaults to
    `authored` (alongside structural/semantic/synthesized/consolidation). A reconcile
    never rebuilds these (only structural edges are re-extracted), so they persist."""
    out: List[Dict[str, Any]] = []
    for e in edges:
        if isinstance(e, dict) and e.get("rel") and e.get("to"):
            out.append({"rel": e["rel"], "to": e["to"],
                        "w": float(e.get("w", 0.5)), "coact": int(e.get("coact", 0)),
                        "origin": e.get("origin", "authored")})
    return out


def _merge_tags(old: Optional[List[str]], new: List[str]) -> List[str]:
    seen, out = set(), []
    for t in list(old or []) + list(new):
        t = str(t)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _working_language(amg_root: Path) -> str:
    """working_language from config.yml, defaulting to 'en' when absent/unreadable —
    capturing a note must not require a fully-formed config (extract_structure.load_config
    would raise on a missing file)."""
    f = amg_root / "config.yml"
    if f.exists():
        try:
            cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            return str(cfg.get("working_language", "en"))
        except (OSError, yaml.YAMLError):
            pass
    return "en"


def add_note(project_root: Path, ntype: str, summary: str, body: str = "",
             tags: Optional[List[str]] = None, status: str = "captured",
             part_of: Optional[List[Dict[str, Any]]] = None, edges: Optional[List[Dict[str, Any]]] = None,
             node_id: Optional[str] = None, amg_root: Optional[Path] = None) -> Dict[str, Any]:
    """Write (or update) one authored node through a crash-safe transaction.

    A new id is created in the `notes/` bucket; an existing id (explicit --id, or a
    content-addressed collision = identical re-capture) is updated in place: `created`
    is preserved, `updated` is bumped, tags/part_of/edges accumulate (the same merge
    rules reconcile uses for derivation items). Returns {id, created, path, txid}.
    """
    if ntype not in NOTE_TYPES:
        raise ValueError(f"unknown note type {ntype!r}; expected one of {NOTE_TYPES}")
    if not (summary or "").strip():
        raise ValueError("a note needs a non-empty --summary")
    tags = list(tags or [])
    part_of = list(part_of or [])
    edges = list(edges or [])
    amg_root = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg_root)
    store.init()
    lang = _working_language(amg_root)
    nid = node_id or _make_id(ntype, summary, body, tags)

    with store.lock():
        store.recover()                    # heal any unfinished write before touching
        nodes = rc.load_nodes(store)
        existing = nodes.get(nid)
        now = rc._now()

        if existing is not None:
            meta = {k: v for k, v in existing.items() if not k.startswith("_")}
            created = meta.get("created", now)
            body_final = body if body else existing.get("_body", "")
            meta.update({"type": ntype, "source_kind": "authored", "policy": "authored",
                         "status": status, "tags": _merge_tags(meta.get("tags"), tags),
                         "lang": lang, "created": created, "updated": now,
                         "summary": summary})
            if part_of:
                meta["part_of"] = rc._merge_part_of(meta.get("part_of") or [],
                                                    part_of, True)
            if edges:
                meta["edges"] = rc._merge_edges(meta.get("edges") or [],
                                                _stamp_edges(edges),
                                                default_origin="authored")
            relpath = existing["_path"]
            created_flag = False
        else:
            meta = {"id": nid, "type": ntype,
                    "source_kind": "authored", "policy": "authored",
                    "status": status, "tags": tags, "part_of": part_of,
                    "edges": _stamp_edges(edges), "lang": lang,
                    "created": now, "updated": now, "summary": summary}
            body_final = body
            relpath = rc.node_relpath(nid, "notes")
            created_flag = True

        tx = store.transaction()
        tx.write(relpath, rc.serialize_node(meta, body_final))
        txid = tx.commit()

    return {"id": nid, "created": created_flag, "path": relpath, "txid": txid,
            "type": ntype, "status": status}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="notes.py", description="Safe authored-note capture.")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("add", help="capture one authored node")
    a.add_argument("--type", dest="ntype", required=True, choices=NOTE_TYPES)
    a.add_argument("--summary", required=True)
    a.add_argument("--body", default="")
    a.add_argument("--tags", default="", help="comma-separated labels")
    a.add_argument("--status", default="captured", choices=("captured", "active"))
    a.add_argument("--part-of", dest="part_of", default="",
                   help='JSON list of {topic,w}')
    a.add_argument("--edges", default="", help='JSON list of {rel,to,w}')
    a.add_argument("--id", dest="node_id", default=None,
                   help="explicit stable id (default: content-addressed)")
    a.add_argument("project_root", nargs="?", default=".")
    a.add_argument("--root", dest="cli_root", default=None, help="agent dir override")
    args = p.parse_args(argv[1:])

    if args.cmd != "add":
        p.print_help()
        return 0

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    part_of = json.loads(args.part_of) if args.part_of.strip() else []
    edges = json.loads(args.edges) if args.edges.strip() else []
    project_root = Path(args.project_root).resolve()
    amg_root = gs.resolve_amg_root(args.cli_root, project_root)
    res = add_note(project_root, args.ntype, args.summary, args.body, tags,
                   args.status, part_of, edges, args.node_id, amg_root)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
