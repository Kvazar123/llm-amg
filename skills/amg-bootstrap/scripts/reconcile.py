#!/usr/bin/env python3
"""
reconcile.py — make the graph match the code/docs on disk. Crash-safe & idempotent.

Reconcile is the heart of consistency. It answers exactly the three cases that
matter when sources change:

  * added    : a source unit with no node  -> create a node
  * changed  : node.source_hash != current content hash -> update, mark for re-derive
  * moved    : a deleted+added pair with the SAME content hash is the same unit at
               a new path/name -> migrate earned fields (summary, semantic edges
               with their coact, derived_from_hash, extra memberships) onto the new
               id and redirect inbound references; a pure move costs zero model calls
  * stale    : hash unchanged but derivation lags (derived_from_hash != source_hash
               or status == stale) -> re-queue WITHOUT rewriting the node. The queue
               is rebuilt from graph state on every run, so a crash between the node
               transaction and the queue write heals on the next bootstrap.
  * deleted  : a mirror node whose source unit is gone -> purge
  * unchanged: same hash -> do nothing (no LLM call; truly idempotent and cheap)
  * frozen   : an absorb_once node already exists -> ignore the source entirely
               (ingest ONCE, then freeze: later changes are not re-derived and the
               pointer is not drifted); like absorb it is never purged on deletion

Crucial safety rules (see ../references/consistency-model.md):
  * Only `derived_from_file` nodes from MIRROR sources are ever purged by source
    diff. `authored` notes (team chat, model conclusions) and `absorb`-derived
    notes are NEVER deleted here — deleting the `data/` folder must not lose them.
  * On a change, the OLD summary/edges are kept (status flipped to `stale`) until
    the semantic re-derivation is committed. A crash mid-derivation loses nothing.
  * All writes go through graph_store transactions, so any interruption recovers.

Commands:
  python reconcile.py bootstrap [<project_root>] [--root <agent_dir>]
  python reconcile.py plan      [<project_root>] [--root <agent_dir>]
  python reconcile.py apply <derivation.json> [<project_root>] [--root <agent_dir>]

The graph root is <agent_dir>/amg, resolved by graph_store.resolve_amg_root:
--root -> AMG_AGENT_DIR env -> the first ancestor of <project_root> holding
amg/config.yml (or .claude/amg/config.yml) -> the engine's own location ->
the default <project_root>/.claude.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import graph_store as gs
from extract_structure import extract, load_config, resolve_sources, detect_policy_conflicts

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

def plan(project_root: Path, amg_root: Optional[Path] = None) -> dict:
    amg_root = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg_root)
    store.init()

    config = load_config(amg_root)
    raw_units = extract(project_root, config, amg_root)
    units = {u["id"]: u for u in raw_units}
    summary = {"added": 0, "changed": 0, "moved": 0, "deleted": 0, "unchanged": 0,
               "requeued_stale": 0, "pointer_refreshed": 0, "frozen": 0}
    queue: List[dict] = []

    with store.lock():
        store.recover()                    # always heal before touching anything
        nodes = load_nodes(store)
        tx = store.transaction()

        module_map = _module_map(units)
        default_lang = config.get("working_language", "en")

        # moved: an added unit whose content matches a node that would be purged
        # is the same source at a new path/name. Migrate earned fields instead of
        # purge+create — otherwise every mirror refactoring erases earned memory.
        pairs = _detect_moves(units, nodes)
        moves = {old["id"]: unit["id"] for old, unit in pairs}
        migrated = [(_migrate_node(old, unit, module_map, default_lang), old, unit)
                    for old, unit in pairs]
        for (meta, needs_queue), old, unit in migrated:
            # second pass over the move map: an edge to ANOTHER simultaneously
            # moved file is translated here (same-file targets were already
            # rewritten inside _migrate_node)
            for e in meta["edges"]:
                if isinstance(e, dict) and e.get("to") in moves:
                    e["to"] = moves[e["to"]]
            tx.write(node_relpath(unit["id"], _dir_for(unit["category"])),
                     serialize_node(meta, old.get("_body", "")))
            tx.delete(old["_path"])
            if needs_queue:
                queue.append(_queue_item(unit))
            summary["moved"] += 1

        # Redirect inbound references (edges, part_of) to the moved ids. Dicts
        # are mutated in place and the transaction stages by path, so a node
        # later rewritten by the changed/drift branches keeps the redirect.
        if moves:
            for nid, node in nodes.items():
                if nid in moves:
                    continue
                redirected = False
                for e in node.get("edges") or []:
                    if isinstance(e, dict) and e.get("to") in moves:
                        e["to"] = moves[e["to"]]
                        redirected = True
                for p in node.get("part_of") or []:
                    if isinstance(p, dict) and p.get("topic") in moves:
                        p["topic"] = moves[p["topic"]]
                        redirected = True
                if redirected:
                    meta = {k: v for k, v in node.items() if not k.startswith("_")}
                    tx.write(node["_path"], serialize_node(meta, node.get("_body", "")))

        # added / changed / unchanged
        moved_new = set(moves.values())
        for uid, unit in units.items():
            if uid in moved_new:
                continue                   # already written by the migration above
            node = nodes.get(uid)
            if node is not None and unit["policy"] == "absorb_once":
                # absorb_once = ingested once, then frozen: source changes are ignored
                # (no re-derivation, no pointer drift); deletion never purges it (the
                # deleted pass is mirror-only). The FIRST ingest falls through below.
                if node.get("source_hash") != unit["content_sha"]:
                    summary["frozen"] += 1     # source changed but the node is frozen
                else:
                    summary["unchanged"] += 1
                continue
            kind_dir = _dir_for(unit["category"])
            relpath = node["_path"] if node else node_relpath(uid, kind_dir)

            if node is None:
                meta = {
                    "id": uid, "type": unit["kind"], "source_path": unit["source_path"],
                    "qualname": unit.get("qualname", ""), "lineno": unit.get("lineno"),
                    "source_kind": "derived_from_file", "policy": unit["policy"],
                    "source_hash": unit["content_sha"], "derived_from_hash": None,
                    "part_of": _part_of_for(unit),
                    "edges": _structural_edges(unit, module_map),
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
                node["edges"] = _refresh_structural_edges(node.get("edges") or [],
                                                          unit, module_map)
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
                # `type` is extraction-owned for mirror nodes (the changed branch
                # already overwrites it), so a kind-canon change (e.g. tree-sitter
                # grammar kinds -> function/class) converges without re-derivation.
                drifted = (node.get("lineno") != unit.get("lineno")
                           or node.get("qualname") != unit.get("qualname", "")
                           or node.get("policy") != unit["policy"]
                           or node.get("type") != unit["kind"])
                if drifted:
                    node.pop("_path", None)
                    body = node.pop("_body", "")
                    node["qualname"] = unit.get("qualname", "")
                    node["lineno"] = unit.get("lineno")
                    node["policy"] = unit["policy"]
                    node["type"] = unit["kind"]
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
            if uid in units or uid in moves:    # moved old ids are already deleted
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

    conflicts = detect_policy_conflicts(raw_units)        # 1.29: mirror/absorb overlap
    if conflicts:
        summary["policy_conflicts"] = conflicts[:20]
    missing = [p for p, _ in resolve_sources(config) if not (project_root / p).exists()]
    if missing:                                           # 1.30: a typo'd path is visible in plan
        summary["missing_sources"] = missing
    summary["queued_for_semantic"] = len(queue)
    return summary


def _module_map(units: Dict[str, dict]) -> Dict[str, str]:
    """Dotted module name -> source_path for the project's Python modules.

    `src/billing.py` registers `src.billing` and the suffix `billing`;
    `pkg/__init__.py` registers `pkg`. An ambiguous suffix (two billing.py in
    different dirs) resolves to nothing — a wrong edge is worse than a dangling
    one, and the full dotted path always stays unambiguous.
    """
    out: Dict[str, Optional[str]] = {}
    for u in units.values():
        if u.get("kind") != "module" or u.get("lang") != "python":
            continue
        rel = u["source_path"]
        parts = [s for s in rel[:-3].split("/") if s] if rel.endswith(".py") else []
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        for i in range(len(parts)):
            name = ".".join(parts[i:])
            if name in out and out[name] != rel:
                out[name] = None               # ambiguous suffix: refuse to guess
            elif name not in out:
                out[name] = rel
    return {k: v for k, v in out.items() if v}


def _structural_edges(unit: dict, module_map: Optional[Dict[str, str]] = None) -> List[dict]:
    edges = []
    for mod in unit.get("imports", []) or []:
        # in-project imports resolve to the module node id; stdlib/third-party
        # stay as the dotted name (a dangling target retrieval simply drops)
        target = (module_map or {}).get(mod)
        to = f"code:{target}" if target else f"code:{mod}"
        edges.append({"rel": "imports", "to": to, "w": 0.6, "coact": 0,
                      "origin": "structural"})
    rel = unit.get("source_path", "")
    for callee in unit.get("calls", []) or []:
        # best-effort same-file target; retrieval drops edges whose target node
        # does not exist, so cross-file calls are simply ignored until resolved.
        edges.append({"rel": "calls", "to": f"code:{rel}::{callee}", "w": 0.7, "coact": 0,
                      "origin": "structural"})
    prev = unit.get("follows")             # chat/session adjacency: this turn -> previous
    if prev:
        edges.append({"rel": "follows", "to": prev, "w": 0.3, "coact": 0,
                      "origin": "structural"})
    seen, out = set(), []
    for e in edges:
        k = (e["rel"], e["to"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _refresh_structural_edges(existing: List[dict], unit: dict,
                              module_map: Optional[Dict[str, str]] = None) -> List[dict]:
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
    for e in _structural_edges(unit, module_map):
        old = old_structural.get((e["rel"], e["to"]))
        if old:                                  # survived the change: keep earned signal
            e["w"] = max(e["w"], float(old.get("w", 0)))
            e["coact"] = int(old.get("coact", 0))
        if (e["rel"], e["to"]) not in kept_keys:  # semantic layer already asserts it
            fresh.append(e)
    return kept + fresh


def _detect_moves(units: Dict[str, dict], nodes: Dict[str, dict]
                  ) -> List[Tuple[dict, dict]]:
    """Pair would-be-purged nodes with would-be-created units by content hash.

    Only nodes the diff would otherwise delete (derived_from_file + mirror with
    a vanished source) are candidates: absorb nodes are never purged, so a
    moved absorb source keeps its orphan and grows a fresh node — consolidation
    merges such near-duplicates. Identical twins (same content at several
    paths) pair deterministically by sorted ids; a move combined with an edit
    (different hash) stays a plain delete+add.
    """
    gone: Dict[str, List[dict]] = {}
    for nid, node in nodes.items():
        if nid in units:
            continue
        if (node.get("source_kind") == "derived_from_file"
                and node.get("policy") == "mirror" and node.get("source_hash")):
            gone.setdefault(node["source_hash"], []).append(node)
    for cands in gone.values():
        cands.sort(key=lambda n: n["id"])
    pairs: List[Tuple[dict, dict]] = []
    for uid in sorted(units):
        if uid in nodes:
            continue
        cands = gone.get(units[uid]["content_sha"])
        if cands:
            pairs.append((cands.pop(0), units[uid]))
    return pairs


def _migrate_node(old: dict, unit: dict, module_map: Dict[str, str],
                  default_lang: str) -> Tuple[dict, bool]:
    """Node for a moved/renamed source unit: structural fields from the new
    unit, earned fields (summary, lang, semantic edges with their coact,
    derived_from_hash, extra memberships) from the old node. Same-file edge
    targets and the path-based primary membership are rewritten to the new
    path. Returns (meta, needs_requeue): a node whose derivation was current
    arrives active — a pure move costs zero model calls.
    """
    old_rel, new_rel = old.get("source_path", ""), unit["source_path"]
    edges = []
    for e in old.get("edges") or []:
        if isinstance(e, dict) and isinstance(e.get("to"), str) and old_rel:
            if e["to"] == f"code:{old_rel}":
                e = dict(e, to=f"code:{new_rel}")
            elif e["to"].startswith(f"code:{old_rel}::"):
                e = dict(e, to=f"code:{new_rel}::" + e["to"][len(f"code:{old_rel}::"):])
        edges.append(e)
    edges = _refresh_structural_edges(edges, unit, module_map)

    old_parent = str(Path(old_rel).parent).replace("\\", "/")
    old_primary = old_parent if old_parent not in (".", "") else unit["category"]
    new_primary = _part_of_for(unit)[0]["topic"]
    part_of, seen = [], set()
    for p in old.get("part_of") or []:
        if isinstance(p, dict) and p.get("topic"):
            topic = new_primary if p["topic"] == old_primary else p["topic"]
            if topic not in seen:
                part_of.append(dict(p, topic=topic))
                seen.add(topic)
    if not part_of:
        part_of = _part_of_for(unit)

    derived = old.get("derived_from_hash")
    fresh = derived == unit["content_sha"]
    status = (old.get("status") or "active") if fresh else "stale"
    meta = {
        "id": unit["id"], "type": unit["kind"], "source_path": new_rel,
        "qualname": unit.get("qualname", ""), "lineno": unit.get("lineno"),
        "source_kind": "derived_from_file", "policy": unit["policy"],
        "source_hash": unit["content_sha"], "derived_from_hash": derived,
        "part_of": part_of, "edges": edges,
        "lang": old.get("lang") or default_lang,
        "status": status, "summary": old.get("summary", ""), "updated": _now(),
    }
    return meta, (not fresh) or status == "stale"


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

def apply_derivation(project_root: Path, derivation_path: Path,
                     amg_root: Optional[Path] = None) -> dict:
    """Apply derivation items to the graph. Two item shapes are supported:

      * update : {id, summary?, lang?, edges?, part_of?, body?} -> update the node
        with that id. Several items may target the SAME node (e.g. a part_of item
        plus a supersedes-edge item); each accumulates onto it.
      * create : {id, type, summary?, lang?, part_of?, edges?, body?} -> when no node
        with that id exists, CREATE it. This is how amg-synth materializes hub /
        overview nodes. Created with source_kind 'synthesized' (not derived_from_file)
        so a later reconcile never purges it as a vanished source.

    Updating sets derived_from_hash = source_hash and flips status to 'active'
    ONLY when the item carries a new `summary` (or the node is not
    derived_from_file — synthesized/authored nodes live active): a unit counts
    as 'derived' once its summary is durably committed. An edges-/part_of-only
    item leaves a stale node stale, so reconcile keeps re-queueing it until a
    summary arrives.
    """
    amg_root = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg_root)
    items = json.loads(Path(derivation_path).read_text(encoding="utf-8"))
    config = load_config(amg_root) or {}
    default_lang = config.get("working_language", "en")
    weights_cfg = config.get("weights") or {}
    renormalize = bool(weights_cfg.get("part_of_renormalize", True))
    default_w = float(weights_cfg.get("default_edge_weight", 0.5))
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
                node["part_of"] = _merge_part_of(node.get("part_of") or [],
                                                 item["part_of"], renormalize)
            if item.get("edges"):
                node["edges"] = _merge_edges(node.get("edges", []), item["edges"],
                                             default_w=default_w)
            if "summary" in item or node.get("source_kind") != "derived_from_file":
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


def _merge_part_of(existing: List[dict], incoming: List[dict],
                   renormalize: bool) -> List[dict]:
    """Accumulate memberships by topic: a later item must not erase a membership
    an earlier one added. Same topic -> the incoming weight wins (the judgment
    layer's latest statement; taking the max would only ratchet weights upward
    and block rebalancing); different topics are appended. If the merged weights
    sum above 1, they are scaled back to the simplex (part_of_renormalize), the
    same rule consolidation applies.
    """
    out = {p["topic"]: dict(p) for p in existing
           if isinstance(p, dict) and p.get("topic")}
    for p in incoming:
        if not (isinstance(p, dict) and p.get("topic")):
            continue
        cur = out.get(p["topic"])
        if cur is not None:
            cur["w"] = p.get("w", cur.get("w", 1.0))
        else:
            out[p["topic"]] = dict(p)
    merged = list(out.values())
    if renormalize:
        s = sum(float(p.get("w", 0)) for p in merged)
        if s > 1.0:
            for p in merged:
                p["w"] = round(float(p.get("w", 0)) / s, 4)
    return merged


def _merge_edges(existing: List[dict], incoming: List[dict],
                 default_origin: str = "semantic", default_w: float = 0.5) -> List[dict]:
    """Merge by (rel, to); keep the higher weight and accumulated coact count.

    An existing edge keeps its origin (a structural edge confirmed by the
    judgment layer stays structural — it is still re-extractable); a new or
    unmarked one takes the incoming origin, defaulting to `default_origin`. A new
    edge with no explicit weight starts at `default_w` (weights.default_edge_weight).
    """
    index = {(e.get("rel"), e.get("to")): dict(e) for e in existing}
    for e in incoming:
        key = (e.get("rel"), e.get("to"))
        if key in index:
            index[key]["w"] = max(index[key].get("w", 0), e.get("w", 0))
            index[key].setdefault("origin", e.get("origin", default_origin))
        else:
            index[key] = {"rel": e.get("rel"), "to": e.get("to"),
                          "w": e.get("w", default_w), "coact": 0,
                          "origin": e.get("origin", default_origin)}
    return list(index.values())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "help"

    if cmd in ("plan", "bootstrap"):
        project_root = Path(args[1]).resolve() if len(args) > 1 else Path.cwd()
        amg_root = gs.resolve_amg_root(cli_root, project_root)
        print(json.dumps(plan(project_root, amg_root), indent=2))
        return 0

    if cmd == "apply":
        if len(args) < 2:
            print("usage: reconcile.py apply <derivation.json> [<project_root>] "
                  "[--root <agent_dir>]")
            return 2
        derivation = Path(args[1])
        project_root = Path(args[2]).resolve() if len(args) > 2 else Path.cwd()
        amg_root = gs.resolve_amg_root(cli_root, project_root)
        print(json.dumps(apply_derivation(project_root, derivation, amg_root), indent=2))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
