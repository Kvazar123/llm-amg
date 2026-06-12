#!/usr/bin/env python3
"""
reconcile.py — make the graph match the code/docs on disk. Crash-safe & idempotent.

Reconcile is the heart of consistency. It answers exactly the three cases that
matter when sources change:

  * added    : a source unit with no node  -> create a node
  * changed  : node.source_hash != current content hash -> update, mark for re-derive
  * stale    : hash unchanged but derivation lags (derived_from_hash != source_hash
               or status == stale) -> re-queue WITHOUT rewriting the node. The queue
               is rebuilt from graph state on every run, so a crash between the node
               transaction and the queue write heals on the next bootstrap.
  * deleted  : a mirror node whose source unit is gone -> purge
  * unchanged: same hash -> do nothing (no LLM call; truly idempotent and cheap)

Crucial safety rules (see ../references/consistency-model.md):
  * Only `derived_from_file` nodes from MIRROR sources are ever purged by source
    diff. `authored` notes (team chat, model conclusions) and `absorb`-derived
    notes are NEVER deleted here — deleting the `data/` folder must not lose them.
  * On a change, the OLD summary/edges are kept (status flipped to `stale`) until
    the semantic re-derivation is committed. A crash mid-derivation loses nothing.
  * All writes go through graph_store transactions, so any interruption recovers.

Commands:
  python reconcile.py bootstrap [<project_root>]   # build/reconcile from any state
  python reconcile.py plan      [<project_root>]   # same diff; structural writes + queue
  python reconcile.py apply <derivation.json>      # apply builder's semantic output
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import graph_store as gs
from extract_structure import extract, load_config

try:
    import yaml
except ImportError:                       # pragma: no cover
    sys.stderr.write("reconcile.py needs PyYAML: pip install pyyaml\n")
    raise


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


# --------------------------------------------------------------------------- #
# Node (de)serialization
# --------------------------------------------------------------------------- #

def node_relpath(unit_id: str, source_kind_dir: str) -> str:
    """Deterministic file path for a node id (collision-safe via id hash)."""
    tail = unit_id.split(":", 1)[-1]
    slug = re.sub(r"[^\w.-]+", "_", tail).strip("_")[:48] or "node"
    h = hashlib.sha256(unit_id.encode()).hexdigest()[:8]
    return f"nodes/{source_kind_dir}/{slug}-{h}.md"


def serialize_node(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body or ''}".rstrip() + "\n"


def parse_node(text: str) -> Optional[dict]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    meta["_body"] = m.group(2)
    return meta


def load_nodes(store: gs.GraphStore) -> Dict[str, dict]:
    """Map node id -> {meta..., _path}. Skips anything without a valid id."""
    out: Dict[str, dict] = {}
    for p in store.nodes_dir.rglob("*.md"):
        meta = parse_node(p.read_text(encoding="utf-8", errors="replace"))
        if meta and meta.get("id"):
            meta["_path"] = p.relative_to(store.root).as_posix()
            out[meta["id"]] = meta
    return out


def _dir_for(category: str) -> str:
    return {"code": "code", "doc": "doc", "data": "data"}.get(category, "notes")


def _part_of_for(unit: dict) -> List[dict]:
    """Path-based primary membership (the spanning-tree parent). Weighted
    multi-membership beyond this is added by the consolidation pass."""
    rel = unit["source_path"]
    parent = str(Path(rel).parent).replace("\\", "/")
    topic = parent if parent not in (".", "") else unit["category"]
    return [{"topic": topic, "w": 1.0}]


# --------------------------------------------------------------------------- #
# Plan / bootstrap
# --------------------------------------------------------------------------- #

def plan(project_root: Path) -> dict:
    amg_root = project_root / ".claude" / "amg"
    store = gs.GraphStore(amg_root)
    store.init()

    config = load_config(amg_root)
    units = {u["id"]: u for u in extract(project_root, config)}
    summary = {"added": 0, "changed": 0, "deleted": 0, "unchanged": 0,
               "requeued_stale": 0, "pointer_refreshed": 0}
    queue: List[dict] = []

    with store.lock():
        store.recover()                    # always heal before touching anything
        nodes = load_nodes(store)
        tx = store.transaction()

        # added / changed / unchanged
        for uid, unit in units.items():
            node = nodes.get(uid)
            kind_dir = _dir_for(unit["category"])
            relpath = node["_path"] if node else node_relpath(uid, kind_dir)

            if node is None:
                meta = {
                    "id": uid, "type": unit["kind"], "source_path": unit["source_path"],
                    "qualname": unit.get("qualname", ""), "lineno": unit.get("lineno"),
                    "source_kind": "derived_from_file", "policy": unit["policy"],
                    "source_hash": unit["content_sha"], "derived_from_hash": None,
                    "part_of": _part_of_for(unit),
                    "edges": _structural_edges(unit),
                    "lang": config.get("working_language", "en"),
                    "status": "stale", "summary": "", "updated": _now(),
                }
                tx.write(relpath, serialize_node(meta, ""))
                queue.append(_queue_item(unit))
                summary["added"] += 1

            elif node.get("source_hash") != unit["content_sha"]:
                # Update structural fields; KEEP the earned summary and semantic
                # edges until re-derived. Structural edges are re-extracted so the
                # graph stays structurally equal to the source (a new call gets
                # its edge, a dropped call loses it).
                node.pop("_path", None)
                body = node.pop("_body", "")
                node["source_hash"] = unit["content_sha"]
                node["type"] = unit["kind"]
                node["source_path"] = unit["source_path"]
                node["policy"] = unit["policy"]
                node["qualname"] = unit.get("qualname", "")
                node["lineno"] = unit.get("lineno")
                node["edges"] = _refresh_structural_edges(node.get("edges") or [], unit)
                node["status"] = "stale"
                node["updated"] = _now()
                node.setdefault("part_of", _part_of_for(unit))
                tx.write(relpath, serialize_node(node, body))
                queue.append(_queue_item(unit))
                summary["changed"] += 1
            else:
                # Source content unchanged; two kinds of lag may still remain.
                # Pointer drift: an edit ABOVE this unit shifted it without changing
                # its content hash -> refresh lineno/qualname only, no re-derivation.
                # Policy rides along: a folder moved between mirror_path/absorb_path
                # must not wait for a content change — the deletion rule reads the
                # node's policy, and a stale `mirror` there would purge knowledge
                # the user explicitly chose to absorb.
                drifted = (node.get("lineno") != unit.get("lineno")
                           or node.get("qualname") != unit.get("qualname", "")
                           or node.get("policy") != unit["policy"])
                if drifted:
                    node.pop("_path", None)
                    body = node.pop("_body", "")
                    node["qualname"] = unit.get("qualname", "")
                    node["lineno"] = unit.get("lineno")
                    node["policy"] = unit["policy"]
                    node["updated"] = _now()
                    tx.write(relpath, serialize_node(node, body))
                    summary["pointer_refreshed"] += 1
                # Derivation lag: the summary never caught up (e.g. a crash before
                # the queue write, or apply never ran) -> re-queue; the node file
                # itself needs no rewrite for this.
                if (node.get("derived_from_hash") != unit["content_sha"]
                        or node.get("status") == "stale"):
                    queue.append(_queue_item(unit))
                    summary["requeued_stale"] += 1
                elif not drifted:
                    summary["unchanged"] += 1

        # deleted: mirror nodes whose source unit vanished
        for uid, node in nodes.items():
            if uid in units:
                continue
            if node.get("source_kind") == "derived_from_file" and node.get("policy") == "mirror":
                tx.delete(node["_path"])
                summary["deleted"] += 1
            # authored / absorb notes are intentionally left untouched

        tx.commit()

        # Persist the work queue for the semantic builder (crash-safe write).
        work_dir = store.root / "work"
        gs.atomic_write_text(work_dir / "queue.json",
                             json.dumps({"generated": _now(), "units": queue},
                                        ensure_ascii=False, indent=2))

    summary["queued_for_semantic"] = len(queue)
    return summary


def _structural_edges(unit: dict) -> List[dict]:
    edges = []
    for mod in unit.get("imports", []) or []:
        edges.append({"rel": "imports", "to": f"code:{mod}", "w": 0.6, "coact": 0,
                      "origin": "structural"})
    rel = unit.get("source_path", "")
    for callee in unit.get("calls", []) or []:
        # best-effort same-file target; retrieval drops edges whose target node
        # does not exist, so cross-file calls are simply ignored until resolved.
        edges.append({"rel": "calls", "to": f"code:{rel}::{callee}", "w": 0.7, "coact": 0,
                      "origin": "structural"})
    seen, out = set(), []
    for e in edges:
        k = (e["rel"], e["to"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _refresh_structural_edges(existing: List[dict], unit: dict) -> List[dict]:
    """Re-extract deterministic edges for a changed unit, keeping earned ones.

    Old structural edges — marked `origin: structural`, or legacy-unmarked
    `imports`/`calls` (the only rels _structural_edges has ever produced) — are
    replaced by a fresh extraction; an edge that persists across the change
    inherits its earned weight and coact count. Edges of any other origin
    (semantic / synthesized / consolidation) are kept untouched.
    """
    old_structural: Dict[tuple, dict] = {}
    kept: List[dict] = []
    for e in existing:
        if isinstance(e, dict) and (
                e.get("origin") == "structural"
                or (e.get("origin") is None and e.get("rel") in ("imports", "calls"))):
            old_structural[(e.get("rel"), e.get("to"))] = e
        else:
            kept.append(e)
    kept_keys = {(e.get("rel"), e.get("to")) for e in kept if isinstance(e, dict)}
    fresh: List[dict] = []
    for e in _structural_edges(unit):
        old = old_structural.get((e["rel"], e["to"]))
        if old:                                  # survived the change: keep earned signal
            e["w"] = max(e["w"], float(old.get("w", 0)))
            e["coact"] = int(old.get("coact", 0))
        if (e["rel"], e["to"]) not in kept_keys:  # semantic layer already asserts it
            fresh.append(e)
    return kept + fresh


def _queue_item(unit: dict) -> dict:
    # qualname/lineno let the builder focus on the right slice of the source;
    # lang here is the SOURCE language/format (python/markdown/...), not the
    # node's `lang` field (which is the summary's working language).
    item = {"id": unit["id"], "kind": unit["kind"], "source_path": unit["source_path"],
            "category": unit["category"], "content_sha": unit["content_sha"],
            "qualname": unit.get("qualname", ""), "lineno": unit.get("lineno"),
            "lang": unit.get("lang")}
    if unit.get("text"):                  # pre-extracted (PDF/DOCX/XLSX): summarize from this
        item["text"] = unit["text"]
    return item


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------- #
# Apply semantic derivation from the builder subagent
# --------------------------------------------------------------------------- #

def apply_derivation(project_root: Path, derivation_path: Path) -> dict:
    """Apply derivation items to the graph. Two item shapes are supported:

      * update : {id, summary?, lang?, edges?, part_of?, body?} -> update the node
        with that id. Several items may target the SAME node (e.g. a part_of item
        plus a supersedes-edge item); each accumulates onto it.
      * create : {id, type, summary?, lang?, part_of?, edges?, body?} -> when no node
        with that id exists, CREATE it. This is how amg-synth materializes hub /
        overview nodes. Created with source_kind 'synthesized' (not derived_from_file)
        so a later reconcile never purges it as a vanished source.

    Updating sets derived_from_hash = source_hash and status 'active', so a unit
    counts as 'derived' only once its summary/edges are durably committed.
    """
    amg_root = project_root / ".claude" / "amg"
    store = gs.GraphStore(amg_root)
    items = json.loads(Path(derivation_path).read_text(encoding="utf-8"))
    default_lang = (load_config(amg_root) or {}).get("working_language", "en")
    applied, created, skipped = 0, 0, 0

    with store.lock():
        store.recover()
        nodes = load_nodes(store)
        tx = store.transaction()
        for item in items:
            node = nodes.get(item["id"])
            if node is None:
                if "type" in item:                       # synthesized node (e.g. a hub)
                    path = node_relpath(item["id"], "_hubs")
                    meta = {
                        "id": item["id"], "type": item["type"],
                        "source_kind": "synthesized", "policy": "authored",
                        "source_hash": None, "derived_from_hash": None,
                        "part_of": item.get("part_of", []),
                        "edges": [dict(e, coact=e.get("coact", 0),
                                       origin=e.get("origin", "synthesized"))
                                  for e in item.get("edges", [])],
                        "lang": item.get("lang", default_lang),
                        "status": "active", "summary": item.get("summary", ""),
                        "updated": _now(),
                    }
                    nodes[item["id"]] = dict(meta, _path=path, _body=item.get("body", ""))
                    tx.write(path, serialize_node(meta, item.get("body", "")))
                    created += 1
                else:
                    skipped += 1                          # update for an unknown id
                continue
            # existing node: read _path/_body WITHOUT popping, so repeated items on
            # the same node accumulate instead of losing the path on the second pass.
            if "summary" in item:
                node["summary"] = item["summary"]
            if "lang" in item:
                node["lang"] = item["lang"]
            if "part_of" in item:
                node["part_of"] = item["part_of"]
            if item.get("edges"):
                node["edges"] = _merge_edges(node.get("edges", []), item["edges"])
            node["derived_from_hash"] = node.get("source_hash")
            node["status"] = "active"
            node["updated"] = _now()
            if "body" in item:
                node["_body"] = item["body"]
            meta = {k: v for k, v in node.items() if not k.startswith("_")}
            tx.write(node["_path"], serialize_node(meta, node.get("_body", "")))
            applied += 1
        tx.commit()

    return {"applied": applied, "created": created, "skipped_missing": skipped}


def _merge_edges(existing: List[dict], incoming: List[dict],
                 default_origin: str = "semantic") -> List[dict]:
    """Merge by (rel, to); keep the higher weight and accumulated coact count.

    An existing edge keeps its origin (a structural edge confirmed by the
    judgment layer stays structural — it is still re-extractable); a new or
    unmarked one takes the incoming origin, defaulting to `default_origin`.
    """
    index = {(e.get("rel"), e.get("to")): dict(e) for e in existing}
    for e in incoming:
        key = (e.get("rel"), e.get("to"))
        if key in index:
            index[key]["w"] = max(index[key].get("w", 0), e.get("w", 0))
            index[key].setdefault("origin", e.get("origin", default_origin))
        else:
            index[key] = {"rel": e.get("rel"), "to": e.get("to"),
                          "w": e.get("w", 0.5), "coact": 0,
                          "origin": e.get("origin", default_origin)}
    return list(index.values())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "help"

    if cmd in ("plan", "bootstrap"):
        project_root = Path(argv[2]).resolve() if len(argv) > 2 else Path.cwd()
        result = plan(project_root)
        print(json.dumps(result, indent=2))
        return 0

    if cmd == "apply":
        if len(argv) < 3:
            print("usage: reconcile.py apply <derivation.json> [<project_root>]")
            return 2
        derivation = Path(argv[2])
        project_root = Path(argv[3]).resolve() if len(argv) > 3 else Path.cwd()
        print(json.dumps(apply_derivation(project_root, derivation), indent=2))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
