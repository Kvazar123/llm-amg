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

Deterministic edge resolution (stage 19, roadmap §4.2 / audits 1.40-1.42): every
plan() builds cross-file symbol tables from the extracted units and resolves
`calls`/`inherits` through each file's own imports (never by name coincidence),
emits the `defines` containment backbone (module -> symbol, class -> method), and
canonicalizes judgment-layer edge targets by unique path suffix — both on apply and
in a whole-graph sweep, so a graph built before this pass heals on one bootstrap.

Reproducibility (stage 19, audit 1.46): applied per-unit derivations are stored in
a persistent cache under cache/derivations/, keyed by content_sha + the derivation
contract version + working_language. A wipe-and-rebuild restores them verbatim —
same content, same derivation, near-zero cost — instead of re-deriving differently
every time. bootstrap/plan restore cache hits automatically (derivation_cache: true).

Commands:
  python reconcile.py bootstrap [<project_root>] [--root <agent_dir>]
  python reconcile.py plan      [<project_root>] [--root <agent_dir>]
  python reconcile.py apply <derivation.json> [<project_root>] [--root <agent_dir>]
  python reconcile.py apply-cached [<project_root>] [--root <agent_dir>]  # restore from cache
  python reconcile.py metrics   [<project_root>] [--root <agent_dir>]   # connectivity report

The graph root is <agent_dir>/amg, resolved by graph_store.resolve_amg_root:
--root -> AMG_AGENT_DIR env -> upward search from <project_root> (agent-dir
presets first, then a bare amg/ only when it is an initialized store; an AMG
source checkout and the home level never resolve as a store) -> the engine's
own location -> the default <project_root>/.claude. Full rules: its docstring.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import graph_store as gs
from extract_structure import extract, load_config, resolve_sources, detect_policy_conflicts

try:
    import yaml
except ImportError:                       # pragma: no cover
    sys.stderr.write("reconcile.py needs PyYAML: pip install pyyaml\n")
    raise


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)

# Git merge-conflict markers (default + diff3 base) at line start. A conflicted node file
# carries these literal lines after a markdown-level git merge (stage 16); its YAML no
# longer parses, so load_nodes skips it (the graph keeps working) and find_conflict_markers
# reports it so the user resolves it and re-bootstraps. The `=======` middle marker is
# deliberately NOT matched — it collides with a setext heading underline; the open/base/
# close markers are unambiguous.
CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}|>{7}|\|{7})(?:\s|$)", re.M)


# --------------------------------------------------------------------------- #
# Node (de)serialization
# --------------------------------------------------------------------------- #

def node_relpath(unit_id: str, source_kind_dir: str) -> str:
    """Deterministic file path for a node id (collision-safe via id hash)."""
    tail = unit_id.split(":", 1)[-1]
    slug = re.sub(r"[^\w.-]+", "_", tail).strip("_")[:48] or "node"
    h = hashlib.sha256(unit_id.encode()).hexdigest()[:8]
    return f"nodes/{source_kind_dir}/{slug}-{h}.md"


def serialize_node(meta: Dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body or ''}".rstrip() + "\n"


def parse_node(text: str) -> Optional[Dict[str, Any]]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None          # malformed frontmatter (e.g. a git merge-conflict node) -> skip
    if not isinstance(meta, dict):
        return None          # a torn merge can yield non-mapping YAML; treat as no node
    meta["_body"] = m.group(2)
    return meta


def load_nodes(store: gs.GraphStore) -> Dict[str, Dict[str, Any]]:
    """Map node id -> {meta..., _path}. Skips anything without a valid id."""
    out: Dict[str, Dict[str, Any]] = {}
    for p in store.nodes_dir.rglob("*.md"):
        meta = parse_node(p.read_text(encoding="utf-8", errors="replace"))
        if meta and meta.get("id"):
            meta["_path"] = p.relative_to(store.root).as_posix()
            out[meta["id"]] = meta
    return out


def find_conflict_markers(store: gs.GraphStore) -> List[str]:
    """Scan nodes/*.md for git merge-conflict markers and return the conflicted files'
    relpaths, sorted. Read-only (lock-free). After a markdown-level git merge (stage 16)
    a same-node conflict leaves these markers; load_nodes already skips such a file (its
    frontmatter no longer parses), so the graph keeps working — this is how status /
    repair / bootstrap SURFACE the conflict so the user resolves it and re-bootstraps."""
    out: List[str] = []
    if not store.nodes_dir.exists():
        return out
    for p in sorted(store.nodes_dir.rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if CONFLICT_MARKER_RE.search(text):
            out.append(p.relative_to(store.root).as_posix())
    return out


def _dir_for(category: str) -> str:
    return {"code": "code", "doc": "doc", "data": "data"}.get(category, "notes")


def _part_of_for(unit: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Path-based primary membership (the spanning-tree parent). Weighted
    multi-membership beyond this is added by the consolidation pass."""
    rel = unit["source_path"]
    parent = str(Path(rel).parent).replace("\\", "/")
    topic = parent if parent not in (".", "") else unit["category"]
    return [{"topic": topic, "w": 1.0}]


# --------------------------------------------------------------------------- #
# Disposable read-index refresh (index_store lives in the amg-retrieve skill)
# --------------------------------------------------------------------------- #

def _refresh_index(amg_root: Path, tx: gs.Transaction) -> None:
    """Best-effort: fold this committed write into the disposable SQLite read-index
    (index_store) so the next retrieve reads it instead of re-scanning nodes/*.md.
    Call under the caller's lock (the index signature must match disk). Cross-skill
    import via sys.path — the established pattern (consolidate imports graph_store the
    same way). Swallows everything: the index is a cache, and retrieve.load_nodes
    rebuilds on any signature mismatch, so a missed refresh is harmless."""
    try:
        idx_dir = str(Path(__file__).resolve().parents[2] / "amg-retrieve" / "scripts")
        if idx_dir not in sys.path:
            sys.path.insert(0, idx_dir)
        import index_store
        written, deleted = tx.node_paths()
        if written or deleted:
            index_store.refresh_after_commit(amg_root, written, deleted)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Plan / bootstrap
# --------------------------------------------------------------------------- #

def plan(project_root: Path, amg_root: Optional[Path] = None) -> Dict[str, Any]:
    amg_root = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg_root)
    store.init()

    config = load_config(amg_root)
    raw_units = extract(project_root, config, amg_root)
    units = {u["id"]: u for u in raw_units}
    summary: Dict[str, Any] = {"added": 0, "changed": 0, "moved": 0, "deleted": 0, "unchanged": 0,
               "requeued_stale": 0, "pointer_refreshed": 0, "edges_refreshed": 0, "frozen": 0,
               "auto_summarized": 0}
    queue: List[Dict[str, Any]] = []

    with store.lock():
        store.recover()                    # always heal before touching anything
        nodes = load_nodes(store)
        tx = store.transaction()

        symbols = _build_symbols(units)    # cross-file resolver tables (stage 19)
        default_lang = config.get("working_language", "en")
        commit = _git_commit(project_root)     # ingest-time provenance.commit (best-effort)
        text_cap = int(config.get("queue_text_max_chars", QUEUE_TEXT_MAX_CHARS) or 0)
        trivial_max = int(config.get("trivial_unit_max_lines", 0) or 0)
        cache_on = bool(config.get("derivation_cache", True))

        def _auto_summary(unit: Dict[str, Any]) -> Optional[str]:
            """The trivial-unit shortcut, cache-aware: a unit already derived once
            restores its EARNED judgment verbatim from the derivation cache, so the
            template applies only to a genuine cache miss (audit 1.47)."""
            s = _trivial_summary(unit, trivial_max)
            if s is not None and cache_on and _cache_lookup(
                    store.root, str(default_lang), unit["content_sha"]):
                return None                # let apply_cached restore the earned summary
            return s

        # moved: an added unit whose content matches a node that would be purged
        # is the same source at a new path/name. Migrate earned fields instead of
        # purge+create — otherwise every mirror refactoring erases earned memory.
        pairs = _detect_moves(units, nodes)
        moves = {old["id"]: unit["id"] for old, unit in pairs}

        # The post-diff id universe and its suffix index, for canonical-target repair
        # (audit 1.42): every unit id plus every surviving non-unit node (hubs, notes,
        # absorb orphans); purged mirrors and the moved-away old ids are excluded.
        deleted_ids = {nid for nid, node in nodes.items()
                       if nid not in units and nid not in moves
                       and node.get("source_kind") == "derived_from_file"
                       and node.get("policy") == "mirror"}
        id_index = set(units) | {nid for nid in nodes
                                 if nid not in deleted_ids and nid not in moves}
        sfx = _build_suffix_index(id_index)

        migrated = [(_migrate_node(old, unit, symbols, default_lang, commit), old, unit)
                    for old, unit in pairs]
        for (meta, needs_queue), old, unit in migrated:
            # second pass over the move map: an edge to ANOTHER simultaneously
            # moved file is translated here (same-file targets were already
            # rewritten inside _migrate_node)
            for e in meta["edges"]:
                if isinstance(e, dict) and e.get("to") in moves:
                    e["to"] = moves[e["to"]]
            meta["edges"] = _normalize_edges(meta["edges"], id_index, sfx)
            tx.write(node_relpath(unit["id"], _dir_for(unit["category"])),
                     serialize_node(meta, old.get("_body", "")))
            tx.delete(old["_path"])
            if needs_queue:
                queue.append(_queue_item(unit, text_cap))
            summary["moved"] += 1

        # Redirect inbound references (edges, part_of) to the moved ids. Dicts
        # are mutated in place and the transaction stages by path, so a node
        # later rewritten by the changed/drift branches keeps the redirect.
        if moves:
            for nid, rnode in nodes.items():
                if nid in moves:
                    continue
                redirected = False
                for e in rnode.get("edges") or []:
                    if isinstance(e, dict) and e.get("to") in moves:
                        e["to"] = moves[e["to"]]
                        redirected = True
                for p in rnode.get("part_of") or []:
                    if isinstance(p, dict) and p.get("topic") in moves:
                        p["topic"] = moves[p["topic"]]
                        redirected = True
                if redirected:
                    meta = {k: v for k, v in rnode.items() if not k.startswith("_")}
                    tx.write(rnode["_path"], serialize_node(meta, rnode.get("_body", "")))

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
                auto = _auto_summary(unit)
                meta = {
                    "id": uid, "type": unit["kind"], "source_path": unit["source_path"],
                    "qualname": unit.get("qualname", ""), "lineno": unit.get("lineno"),
                    "line_end": unit.get("line_end", unit.get("lineno")),
                    "source_kind": "derived_from_file", "policy": unit["policy"],
                    "source_hash": unit["content_sha"],
                    "derived_from_hash": unit["content_sha"] if auto else None,
                    "provenance": _provenance(unit["category"], commit),
                    "verification": _fresh_verification(),
                    "part_of": _part_of_for(unit),
                    "edges": _structural_edges(unit, symbols),
                    "lang": config.get("working_language", "en"),
                    "status": "active" if auto else "stale",
                    "summary": auto or "", "updated": _now(),
                }
                tx.write(relpath, serialize_node(meta, ""))
                if auto:
                    summary["auto_summarized"] += 1
                else:
                    queue.append(_queue_item(unit, text_cap))
                summary["added"] += 1

            elif node.get("source_hash") != unit["content_sha"]:
                # Update structural fields; KEEP the earned summary and semantic
                # edges until re-derived. Structural edges are re-extracted so the
                # graph stays structurally equal to the source (a new call gets
                # its edge, a dropped call loses it). A now-trivial unit is
                # auto-summarized instead of queued: the old summary describes the
                # OLD code, the template describes the current one.
                auto = _auto_summary(unit)
                node.pop("_path", None)
                body = node.pop("_body", "")
                node["source_hash"] = unit["content_sha"]
                node["type"] = unit["kind"]
                node["source_path"] = unit["source_path"]
                node["policy"] = unit["policy"]
                node["qualname"] = unit.get("qualname", "")
                node["lineno"] = unit.get("lineno")
                node["line_end"] = unit.get("line_end", unit.get("lineno"))
                node["provenance"] = _provenance(unit["category"], commit)
                node["verification"] = _fresh_verification()   # source changed -> re-verify
                node["edges"] = _normalize_edges(
                    _refresh_structural_edges(node.get("edges") or [], unit, symbols),
                    id_index, sfx)
                if auto:
                    node["summary"] = auto
                    node["derived_from_hash"] = unit["content_sha"]
                    node["status"] = "active"
                    node.pop("confidence", None)   # any old estimate rated the old summary
                else:
                    node["status"] = "stale"
                node["updated"] = _now()
                node.setdefault("part_of", _part_of_for(unit))
                tx.write(relpath, serialize_node(node, body))
                if auto:
                    summary["auto_summarized"] += 1
                else:
                    queue.append(_queue_item(unit, text_cap))
                summary["changed"] += 1
            else:
                # Source content unchanged; three kinds of lag may still remain.
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
                # Edge canon lag: structural extraction improved (the resolver,
                # defines, inherits — stage 19) or an edge target's canonical id now
                # exists. Re-extract + normalize and rewrite ONLY when the result
                # differs, so an already-canonical graph stays a strict no-op
                # (idempotency preserved) while a pre-resolver graph heals on one
                # bootstrap without a single model call.
                current_edges = node.get("edges") or []
                fresh_edges = _normalize_edges(
                    _refresh_structural_edges(current_edges, unit, symbols),
                    id_index, sfx)
                edges_changed = not _edges_equivalent(fresh_edges, current_edges)
                # Derivation lag: the summary never caught up (e.g. a crash before
                # the queue write, or apply never ran). A lagging TRIVIAL unit is
                # resolved right here by the auto-summary (it would otherwise loop
                # in the queue forever); anything else re-queues.
                lagging = (node.get("derived_from_hash") != unit["content_sha"]
                           or node.get("status") == "stale")
                auto = _auto_summary(unit) if lagging else None
                if drifted or edges_changed or auto:
                    node["qualname"] = unit.get("qualname", "")
                    node["lineno"] = unit.get("lineno")
                    node["line_end"] = unit.get("line_end", unit.get("lineno"))
                    node["policy"] = unit["policy"]
                    node["type"] = unit["kind"]
                    if edges_changed:        # a pure reorder never churns the file
                        node["edges"] = fresh_edges
                    if auto:
                        node["summary"] = auto
                        node["derived_from_hash"] = unit["content_sha"]
                        node["status"] = "active"
                        node.pop("confidence", None)
                    node["updated"] = _now()
                    meta = {k: v for k, v in node.items() if not k.startswith("_")}
                    tx.write(relpath, serialize_node(meta, node.get("_body", "")))
                    if drifted:
                        summary["pointer_refreshed"] += 1
                    if edges_changed:
                        summary["edges_refreshed"] += 1
                if lagging:
                    if auto:
                        summary["auto_summarized"] += 1
                    else:
                        queue.append(_queue_item(unit, text_cap))
                        summary["requeued_stale"] += 1
                elif not drifted and not edges_changed:
                    summary["unchanged"] += 1

        # deleted: mirror nodes whose source unit vanished
        for uid in sorted(deleted_ids):
            tx.delete(nodes[uid]["_path"])
            summary["deleted"] += 1
            # authored / absorb notes are intentionally left untouched (not in the set)

        # Canonical-target sweep over nodes with no unit this run (hubs, notes,
        # absorb orphans): their judgment edges may point at a target written
        # without its full path — re-bind when exactly one canonical id exists
        # (audit 1.42). Unit-backed nodes were normalized in their branches above.
        # Writes only on an actual change, so a canonical graph stays a no-op.
        for nid, node in nodes.items():
            if nid in units or nid in moves or nid in deleted_ids:
                continue
            current = node.get("edges") or []
            fixed = _normalize_edges(current, id_index, sfx)
            if fixed is not current:         # same object back == nothing repaired
                node["edges"] = fixed
                meta = {k: v for k, v in node.items() if not k.startswith("_")}
                tx.write(node["_path"], serialize_node(meta, node.get("_body", "")))
                summary["edges_refreshed"] += 1

        txid = tx.commit()
        if txid:
            _refresh_index(store.root, tx)     # warm the read-index under the lock

        # Persist the work queue for the semantic builder (crash-safe write).
        work_dir = store.root / "work"
        gs.atomic_write_text(work_dir / "queue.json",
                             json.dumps({"generated": _now(), "units": queue},
                                        ensure_ascii=False, indent=2))

        # Audit line (transactional, de-duped by txid; 1.15). Only when something
        # actually changed — txid is None for an empty diff — so an unchanged re-run
        # stays a true no-op for the graph AND the log (idempotency, see docstring).
        if txid:
            store.append_log(
                "reconcile",
                f"bootstrap: added={summary['added']} changed={summary['changed']} "
                f"moved={summary['moved']} deleted={summary['deleted']} "
                f"requeued={summary['requeued_stale']} frozen={summary['frozen']} "
                f"auto={summary['auto_summarized']} "
                f"edges_refreshed={summary['edges_refreshed']} queued={len(queue)}", txid)

    conflicts = detect_policy_conflicts(raw_units)        # 1.29: mirror/absorb overlap
    if conflicts:
        summary["policy_conflicts"] = conflicts[:20]
    missing = [p for p, _ in resolve_sources(config) if not (project_root / p).exists()]
    if missing:                                           # 1.30: a typo'd path is visible in plan
        summary["missing_sources"] = missing
    merge_conflicts = find_conflict_markers(store)        # stage 16: leftover git markers
    if merge_conflicts:
        summary["conflict_markers"] = merge_conflicts[:20]
    summary["queued_for_semantic"] = len(queue)
    return summary


def _module_map(units: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
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


def _build_symbols(units: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Cross-file symbol tables for the deterministic edge resolver (stage 19,
    roadmap §4.2 / audits 1.40-1.42): the module map (dotted name -> source path),
    per-file top-level definition names, per-file qualname sets, and per-file import
    bindings (local name -> dotted target, from the chunker). Resolution goes THROUGH
    a file's own imports — never by global name coincidence — because a wrong edge is
    worse than a dangling one. Built once per plan() from the extracted units."""
    top: Dict[str, Set[str]] = {}
    quals: Dict[str, Set[str]] = {}
    bindings: Dict[str, Dict[str, str]] = {}
    for u in units.values():
        if u.get("category") != "code":
            continue
        rel = u.get("source_path") or ""
        q = u.get("qualname") or ""
        if q:
            quals.setdefault(rel, set()).add(q)
            if "." not in q:
                top.setdefault(rel, set()).add(q)
        if u.get("kind") == "module" and u.get("import_bindings"):
            bindings[rel] = dict(u["import_bindings"])
    return {"module_map": _module_map(units), "top": top, "quals": quals,
            "bindings": bindings}


def _resolve_dotted(dotted: str, symbols: Dict[str, Any]) -> Optional[str]:
    """Bind a dotted chain (`util.helper2`, `pkg.mod.Class.method`) to a unit id: the
    longest dotted prefix naming an in-project module wins, and the remainder must be
    an existing qualname in that module — else None (a stdlib/third-party module, or
    a symbol the module does not define, never becomes an edge)."""
    module_map: Dict[str, str] = symbols.get("module_map") or {}
    quals: Dict[str, Set[str]] = symbols.get("quals") or {}
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        target = module_map.get(".".join(parts[:i]))
        if target:
            qual = ".".join(parts[i:])
            return f"code:{target}::{qual}" if qual in quals.get(target, set()) else None
    return None


def _resolve_symbol(unit: Dict[str, Any], name: str,
                    symbols: Dict[str, Any]) -> Optional[str]:
    """Resolve a called or inherited name from `unit`'s point of view to a canonical
    unit id, or None (-> no edge at all; audit 1.40). Order: a bare name binds to a
    same-file top-level definition, then to the file's import bindings; a same-file
    qualified reference (`Box.make`) binds directly; `self.X`/`cls.X` binds to a
    method of the owning class; any other dotted chain expands its head through the
    import bindings and then the module map. Builtins, unknown receivers, and
    external modules resolve to nothing."""
    if not symbols:
        return None
    rel = unit.get("source_path") or ""
    top: Set[str] = (symbols.get("top") or {}).get(rel) or set()
    quals: Set[str] = (symbols.get("quals") or {}).get(rel) or set()
    bindings: Dict[str, str] = (symbols.get("bindings") or {}).get(rel) or {}
    if "." not in name:
        if name in top:
            return f"code:{rel}::{name}"
        bound = bindings.get(name)
        return _resolve_dotted(bound, symbols) if bound else None
    if name in quals:                        # same-file qualified ref (Box.method)
        return f"code:{rel}::{name}"
    head, rest = name.split(".", 1)
    if head in ("self", "cls"):
        q = unit.get("qualname") or ""
        cls = q if unit.get("kind") == "class" else (q.rsplit(".", 1)[0] if "." in q else "")
        if cls and f"{cls}.{rest}" in quals:
            return f"code:{rel}::{cls}.{rest}"
        return None
    alias = bindings.get(head)
    if not alias:
        return None          # only the file's own imports may bind a chain (no guessing)
    return _resolve_dotted(f"{alias}.{rest}", symbols)


def _structural_edges(unit: Dict[str, Any], symbols: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Deterministic edges for one unit (stage 19 completes roadmap §4.2):

      * imports  : an in-project module resolves to its node id via the module map;
                   stdlib/third-party stay dotted names — legitimately dangling, they
                   record the import fact and retrieval simply drops them;
      * defines  : the containment backbone — module -> top-level symbol, class ->
                   method (audit 1.41); targets exist by construction (w 1.0, like
                   the primary part_of membership: containment is definitional);
      * inherits : class -> base class, resolved like calls (audit 1.41; w 0.8 —
                   subclass-base coupling is tighter than a call);
      * calls    : resolved against the symbol tables (same file -> import bindings
                   -> module map) and emitted ONLY when the target unit exists —
                   builtins and unresolvable receivers produce no edge (audit 1.40);
      * follows  : chat/session adjacency (unchanged).
    """
    module_map: Dict[str, str] = (symbols or {}).get("module_map") or {}
    edges = []
    for mod in unit.get("imports", []) or []:
        target = module_map.get(mod)
        to = f"code:{target}" if target else f"code:{mod}"
        edges.append({"rel": "imports", "to": to, "w": 0.6, "coact": 0,
                      "origin": "structural"})
    rel = unit.get("source_path", "")
    for qual in unit.get("defines", []) or []:
        edges.append({"rel": "defines", "to": f"code:{rel}::{qual}", "w": 1.0,
                      "coact": 0, "origin": "structural"})
    for base in unit.get("bases", []) or []:
        target2 = _resolve_symbol(unit, base, symbols or {})
        if target2 and target2 != unit.get("id"):
            edges.append({"rel": "inherits", "to": target2, "w": 0.8, "coact": 0,
                          "origin": "structural"})
    for callee in unit.get("calls", []) or []:
        target2 = _resolve_symbol(unit, callee, symbols or {})
        if target2 and target2 != unit.get("id"):
            edges.append({"rel": "calls", "to": target2, "w": 0.7, "coact": 0,
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


def _build_suffix_index(ids: Set[str]) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
    """(category, qualifier) -> [(path, id)] over the known node ids, for canonical-id
    repair (audit 1.42): a judgment-layer target written without its leading
    directories (`code:core/foo.py::bar`) re-binds to the ONE node whose path ends
    with the written path on a '/' boundary. Ambiguity -> no repair."""
    out: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
    for nid in ids:
        if ":" not in nid:
            continue
        cat, tail = nid.split(":", 1)
        path, _, qual = tail.partition("::")
        out[(cat, qual)].append((path, nid))
    return out


def _edges_equivalent(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> bool:
    """Order-insensitive equality of two edge lists. Edge order carries no meaning
    (adjacency accumulates by (rel, to)), so a mere reordering by the structural
    refresh must not count as a change — otherwise every node that earned semantic
    edges would be rewritten once per bootstrap for nothing."""
    if len(a) != len(b):
        return False

    def key(e: Any) -> Tuple[str, str]:
        if isinstance(e, dict):
            return (str(e.get("rel")), str(e.get("to")))
        return ("", str(e))

    return sorted(a, key=key) == sorted(b, key=key)


def _repair_target(to: str, sfx: Dict[Tuple[str, str], List[Tuple[str, str]]]) -> Optional[str]:
    """The unique canonical id whose path ends with the written target's path (same
    category and qualifier), or None. Segment-safe: `core/foo.py` matches
    `src/pkg/core/foo.py` but a dotted external name (`json`) never matches a real
    path (`src/json.py` does not end with `/json`)."""
    if ":" not in to:
        return None
    cat, tail = to.split(":", 1)
    path, _, qual = tail.partition("::")
    if not path:
        return None
    hits = [nid for p, nid in sfx.get((cat, qual), ())
            if p != path and p.endswith("/" + path)]
    return hits[0] if len(hits) == 1 else None


def _normalize_edges(edges: List[Dict[str, Any]], ids: Set[str],
                     sfx: Dict[Tuple[str, str], List[Tuple[str, str]]]) -> List[Dict[str, Any]]:
    """Re-bind edge targets that name no existing node to their canonical id when
    exactly one candidate exists (path-suffix repair, audit 1.42). Unrepairable
    targets stay as written — retrieval drops dangling edges, and `imports` to
    external modules are legitimately dangling. Returns the SAME list object when
    nothing changed (cheap no-op detection for callers); on a change the result is
    deduplicated by (rel, to), since a repair may collide with an existing edge."""
    repaired = False
    out: List[Dict[str, Any]] = []
    for e in edges:
        to = e.get("to") if isinstance(e, dict) else None
        if isinstance(to, str) and to not in ids:
            fixed = _repair_target(to, sfx)
            if fixed:
                e = dict(e, to=fixed)
                repaired = True
        out.append(e)
    if not repaired:
        return edges
    merged: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    passthrough: List[Dict[str, Any]] = []
    for e in out:
        if not isinstance(e, dict) or not e.get("to"):
            passthrough.append(e)
            continue
        key = (e.get("rel"), e["to"])
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(e)
        else:
            cur["w"] = max(float(cur.get("w", 0)), float(e.get("w", 0)))
            cur["coact"] = int(cur.get("coact", 0)) + int(e.get("coact", 0))
    return passthrough + list(merged.values())


def _refresh_structural_edges(existing: List[Dict[str, Any]], unit: Dict[str, Any],
                              symbols: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Re-extract deterministic edges for a unit, keeping earned ones.

    Old structural edges — marked `origin: structural`, or legacy-unmarked
    `imports`/`calls` (the only rels the pre-resolver extraction ever produced) —
    are replaced by a fresh extraction; an edge that persists across the change
    inherits its earned weight and coact count, while a target the resolver no
    longer emits (a builtin, a same-file-glued miss) is dropped. Edges of any other
    origin (semantic / synthesized / consolidation) are kept untouched.
    """
    old_structural: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    kept: List[Dict[str, Any]] = []
    for e in existing:
        if isinstance(e, dict) and (
                e.get("origin") == "structural"
                or (e.get("origin") is None and e.get("rel") in ("imports", "calls"))):
            old_structural[(e.get("rel"), e.get("to"))] = e
        else:
            kept.append(e)
    kept_keys = {(e.get("rel"), e.get("to")) for e in kept if isinstance(e, dict)}
    fresh: List[Dict[str, Any]] = []
    for e in _structural_edges(unit, symbols):
        old = old_structural.get((e["rel"], e["to"]))
        if old:                                  # survived the change: keep earned signal
            e["w"] = max(e["w"], float(old.get("w", 0)))
            e["coact"] = int(old.get("coact", 0))
        if (e["rel"], e["to"]) not in kept_keys:  # semantic layer already asserts it
            fresh.append(e)
    return kept + fresh


def _detect_moves(units: Dict[str, Dict[str, Any]], nodes: Dict[str, Dict[str, Any]]
                  ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Pair would-be-purged nodes with would-be-created units by content hash.

    Only nodes the diff would otherwise delete (derived_from_file + mirror with
    a vanished source) are candidates: absorb nodes are never purged, so a
    moved absorb source keeps its orphan and grows a fresh node — consolidation
    merges such near-duplicates. Identical twins (same content at several
    paths) pair deterministically by sorted ids; a move combined with an edit
    (different hash) stays a plain delete+add.
    """
    gone: Dict[str, List[Dict[str, Any]]] = {}
    for nid, node in nodes.items():
        if nid in units:
            continue
        if (node.get("source_kind") == "derived_from_file"
                and node.get("policy") == "mirror" and node.get("source_hash")):
            gone.setdefault(node["source_hash"], []).append(node)
    for group in gone.values():
        group.sort(key=lambda n: n["id"])
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for uid in sorted(units):
        if uid in nodes:
            continue
        cands = gone.get(units[uid]["content_sha"])
        if cands:
            pairs.append((cands.pop(0), units[uid]))
    return pairs


def _migrate_node(old: Dict[str, Any], unit: Dict[str, Any], symbols: Dict[str, Any],
                  default_lang: str, commit: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
    """Node for a moved/renamed source unit: structural fields from the new
    unit, earned fields (summary, lang, semantic edges with their coact,
    derived_from_hash, extra memberships) from the old node. Same-file edge
    targets and the path-based primary membership are rewritten to the new
    path. Returns (meta, needs_requeue): a node whose derivation was current
    arrives active — a pure move costs zero model calls.

    A move is content-identical, so any verification the old node earned stays valid
    and rides along; provenance carries the new path's kind and the current commit.
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
    edges = _refresh_structural_edges(edges, unit, symbols)

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
        "line_end": unit.get("line_end", unit.get("lineno")),
        "source_kind": "derived_from_file", "policy": unit["policy"],
        "source_hash": unit["content_sha"], "derived_from_hash": derived,
        "provenance": _provenance(unit["category"], commit),
        "verification": old.get("verification") or _fresh_verification(),
        "part_of": part_of, "edges": edges,
        "lang": old.get("lang") or default_lang,
        "status": status, "summary": old.get("summary", ""), "updated": _now(),
    }
    return meta, (not fresh) or status == "stale"


# Protocol dunders that change HOW a class is used even when their body is one line
# (callable, context manager, attribute/index magic, iteration, construction): a
# code-verbatim template would state the mechanics but miss exactly that semantic,
# so these always go to the judgment layer regardless of size. Python-construct
# names, not natural language — the engine stays language-universal.
_SIGNIFICANT_DUNDERS = {
    "__call__", "__enter__", "__exit__", "__getattr__", "__getattribute__",
    "__setattr__", "__delattr__", "__getitem__", "__setitem__", "__delitem__",
    "__iter__", "__next__", "__new__", "__init_subclass__",
}


def _trivial_summary(unit: Dict[str, Any], max_lines: int) -> Optional[str]:
    """Deterministic auto-summary for a TRIVIAL code unit — a function whose whole
    definition spans at most `max_lines` lines: dunders, one-line getters and other
    mechanical bodies (audit 1.47). The summary is the unit's own code collapsed to
    one line — language-neutral (identifiers verbatim; the working_language rule
    concerns prose), token-bearing for lexical seeding, impossible to hallucinate.
    Returns None when the unit does not qualify (only code functions; the protocol
    dunders above are exempt — their presence is the semantic).

    Deliberately a SUMMARY, not a skip: plan() re-queues any unit whose
    derived_from_hash lags forever, so a merely-skipped unit would loop in the queue.
    The auto-summary marks the unit derived (derived_from_hash = source_hash) while
    the pointer and structural edges stay intact."""
    if max_lines <= 0 or unit.get("kind") != "function" or unit.get("category") != "code":
        return None
    lineno, line_end = unit.get("lineno"), unit.get("line_end")
    if not lineno or not line_end or (int(line_end) - int(lineno) + 1) > max_lines:
        return None
    if (unit.get("qualname") or "").rsplit(".", 1)[-1] in _SIGNIFICANT_DUNDERS:
        return None
    text = (unit.get("text") or "").strip()
    if not text:
        return None
    return re.sub(r"\s+", " ", text)[:240]


# Ceiling on the unit text carried into work/queue.json (chars). Every chunker now
# hands over the unit's exact content slice, and inlining it is strictly cheaper for
# the builder than a Read call (same content cost, no tool round-trip that re-sends
# the whole accumulated context — the dominant token sink of a build, audit 1.47).
# The cap only guards against pathological units (a minified bundle, a giant module):
# above it the item falls back to the pointer and the builder reads the slice itself.
QUEUE_TEXT_MAX_CHARS = 20000


def _queue_item(unit: Dict[str, Any], text_cap: int = QUEUE_TEXT_MAX_CHARS) -> Dict[str, Any]:
    # qualname/lineno/line_end let the builder focus on the right slice of the
    # source; lang here is the SOURCE language/format (python/markdown/...), not
    # the node's `lang` field (which is the summary's working language).
    item = {"id": unit["id"], "kind": unit["kind"], "source_path": unit["source_path"],
            "category": unit["category"], "content_sha": unit["content_sha"],
            "qualname": unit.get("qualname", ""), "lineno": unit.get("lineno"),
            "line_end": unit.get("line_end", unit.get("lineno")),
            "lang": unit.get("lang")}
    text = unit.get("text")
    if text and 0 < len(text) <= text_cap:    # cap 0 = pointer-only queue (opt-out)
        item["text"] = text               # summarize from this; do not re-open the source
    return item


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------- #
# Provenance, confidence & verification (Stage 13)
# --------------------------------------------------------------------------- #

DEFAULT_CONFIDENCE = 0.7        # fallback when the builder emits a summary but no estimate


def _git_commit(project_root: Path) -> Optional[str]:
    """Best-effort short git commit at the source root, for provenance.commit (Stage 13).
    Returns None when git is absent, the dir is not a repo, or anything fails — the field
    is OPTIONAL, so a non-git project simply omits it (no hard dependency on git)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _git_branch(project_root: Path) -> Optional[str]:
    """Best-effort current git branch at the source root (branch awareness, stage 16).
    Returns None when git is absent, the dir is not a repo, HEAD is detached (rev-parse
    yields 'HEAD'), or anything fails — OPTIONAL, like _git_commit (no hard git dependency)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    branch = out.stdout.strip()
    return branch if branch and branch != "HEAD" else None     # 'HEAD' = detached -> None


def _git_changed_since(project_root: Path, commit: str) -> Optional[set[str]]:
    """Best-effort: the set of source paths changed between `commit` and HEAD, via
    `git diff --name-only --relative` (source-freshness-by-commit, stage 16). `--relative`
    makes the paths relative to project_root, so they line up with a node's source_path
    even when project_root is a subdirectory of the repo. Returns None when git is absent,
    the commit no longer resolves, or anything fails — the signal is OPTIONAL and only
    COMPLEMENTS the authoritative content-hash check (verify), never replaces it."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "diff", "--name-only", "--relative",
             commit, "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def _provenance(kind: str, commit: Optional[str]) -> Dict[str, Any]:
    """Origin block for a node. It carries ONLY the dimensions not already in the flat
    operational fields — source_path/source_hash/lineno/line_end ARE the file-projected
    node's provenance, so they are not duplicated here: just the origin `kind`
    (code/doc/data) and an optional ingest-time `commit`."""
    prov: Dict[str, Any] = {"kind": kind}
    if commit:
        prov["commit"] = commit
    return prov


def _fresh_verification() -> Dict[str, str]:
    """A never-checked verification record: the default for a new or just-changed node.
    The pack flags it; a code claim is checked live before answering (verify_claims.py)."""
    return {"status": "unverified", "method": "none"}


def _clamp01(x: Any, default: float = DEFAULT_CONFIDENCE) -> float:
    """Coerce a confidence estimate into [0, 1]; fall back to `default` on a bad value."""
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def _synth_provenance(item: Dict[str, Any]) -> Dict[str, Any]:
    """Provenance for a synthesized/created node (a hub/overview): kind `model_inference`
    — it is the model's own synthesis OVER other nodes, not a file projection — plus an
    optional `derived_from` list, the upstream ids it was distilled from (Stage 13 task 3
    'source ids'; the source pointer a synthesized node has no flat source_path for)."""
    prov: Dict[str, Any] = {"kind": "model_inference"}
    df = item.get("derived_from")
    if df:
        prov["derived_from"] = df
    return prov


# --------------------------------------------------------------------------- #
# Persistent derivation cache (stage 19, audit 1.46)
# --------------------------------------------------------------------------- #

# Version of the derivation contract (the item schema + the semantics the builder
# prompts promise). Bump it when either changes materially: every cached entry keyed
# under an older contract silently misses, forcing a fresh derivation.
DERIVATION_CONTRACT = 1


def _derivation_cache_path(amg_root: Path, sha: str) -> Path:
    """cache/derivations/<sha[:2]>/<sha>.json — one file per unit content hash (the
    two-hex fan-out keeps a big graph's cache directory listable)."""
    return amg_root / "cache" / "derivations" / sha[:2] / f"{sha}.json"


def _cache_store(amg_root: Path, lang: str, per_sha: Dict[str, List[Dict[str, Any]]]) -> int:
    """Persist applied per-unit derivation items keyed by their content_sha. The
    entry records the derivation contract version and the working language — a
    changed contract or summary language must miss, never silently return foreign
    derivations (audit 1.46). Best-effort: the cache is an economy, not correctness."""
    stored = 0
    for sha, its in per_sha.items():
        try:
            gs.atomic_write_text(
                _derivation_cache_path(amg_root, sha),
                json.dumps({"contract": DERIVATION_CONTRACT, "lang": lang,
                            "items": its}, ensure_ascii=False))
            stored += 1
        except OSError:
            pass
    return stored


def _cache_lookup(amg_root: Path, lang: str, sha: str) -> Optional[List[Dict[str, Any]]]:
    """The cached derivation items for a content hash, or None on a miss / a stale
    contract / another working language / an unreadable entry."""
    p = _derivation_cache_path(amg_root, sha)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("contract") != DERIVATION_CONTRACT
            or data.get("lang") != lang or not isinstance(data.get("items"), list)):
        return None
    return [it for it in data["items"] if isinstance(it, dict)]


def apply_cached(project_root: Path, amg_root: Optional[Path] = None) -> Dict[str, Any]:
    """Restore derivations for queued units from the persistent cache (audit 1.46).

    Reads work/queue.json, gathers the cached items of every unit whose content_sha
    hits (same derivation contract and working language), applies them through the
    STANDARD apply path — validation, target normalization, and the content_sha
    freshness check all included — and rewrites the queue to the remainder. The
    result: a wipe-and-rebuild re-derives only genuinely new content; everything
    already derived once restores verbatim at near-zero cost (practical determinism,
    stronger than temperature=0). bootstrap/plan run this automatically when
    `derivation_cache` is enabled; the CLI command exists for manual/partial runs.
    """
    amg_root = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    config = load_config(amg_root) or {}
    if not bool(config.get("derivation_cache", True)):
        return {"enabled": False, "restored_units": 0}
    qpath = amg_root / "work" / "queue.json"
    if not qpath.exists():
        return {"restored_units": 0, "remaining": 0}
    data = json.loads(qpath.read_text(encoding="utf-8"))
    units: List[Dict[str, Any]] = data.get("units", []) if isinstance(data, dict) else []
    lang = str(config.get("working_language", "en"))
    items: List[Dict[str, Any]] = []
    hit_ids: Set[str] = set()
    for u in units:
        sha = u.get("content_sha")
        cached = _cache_lookup(amg_root, lang, str(sha)) if sha else None
        if cached:
            items.extend(cached)
            hit_ids.add(str(u.get("id")))
    if not items:
        return {"restored_units": 0, "remaining": len(units)}
    tmp = amg_root / "work" / "cached-derivation.json"
    gs.atomic_write_text(tmp, json.dumps(items, ensure_ascii=False))
    result = apply_derivation(project_root, tmp, amg_root)
    # Drop restored units from the queue; keep any hit whose node stayed stale (a
    # source changed between plan and restore — the freshness check skipped it).
    nodes = load_nodes(gs.GraphStore(amg_root))
    remaining = [u for u in units
                 if str(u.get("id")) not in hit_ids
                 or (nodes.get(str(u.get("id"))) or {}).get("status") == "stale"]
    gs.atomic_write_text(qpath, json.dumps(
        {"generated": data.get("generated"), "units": remaining},
        ensure_ascii=False, indent=2))
    return {"restored_units": len(units) - len(remaining),
            "remaining": len(remaining), **result}


# --------------------------------------------------------------------------- #
# Apply semantic derivation from the builder subagent
# --------------------------------------------------------------------------- #

# Category prefixes a synthesized create item may double up (`hub:overview:x`
# instead of `overview:x`) — collapsed by _sanitize_item when the item's own type
# names the inner prefix.
_SYNTH_PREFIX_RE = re.compile(r"^(hub|overview|pattern|note):((?:hub|overview|pattern|note):.+)$")


def _sanitize_item(item: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Per-item validation/normalization for apply (audit 1.43): repair what is
    mechanically repairable, skip what is not — one malformed item must never abort
    the batch. Returns (clean_item, None) or (None, reason).

    Repairs: swapped confidence/edges fields (the observed builder failure: the edge
    list under `confidence`, a float under `edges`); a doubled category prefix on a
    create item's id; malformed edge / membership entries (dropped one by one); a
    non-list `edges`/`part_of` and a non-string `summary`/`lang`/`body` (field
    dropped, the rest of the item applies); an out-of-range edge weight (dropped ->
    the default applies at merge)."""
    if not isinstance(item, dict):
        return None, "item is not an object"
    it = dict(item)
    if isinstance(it.get("confidence"), list) and it["confidence"] and all(
            isinstance(e, dict) and ("to" in e or "rel" in e) for e in it["confidence"]):
        conf = it.get("edges")               # the swapped-away numeric estimate, if any
        it["edges"] = it.pop("confidence")
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            it["confidence"] = conf
    nid = it.get("id")
    if not isinstance(nid, str) or not nid.strip():
        return None, "missing id"
    nid = nid.strip()
    m = _SYNTH_PREFIX_RE.match(nid)
    if m and str(it.get("type", "")).strip() == m.group(2).split(":", 1)[0]:
        nid = m.group(2)                     # hub:overview:x + type overview -> overview:x
    it["id"] = nid
    if "edges" in it:
        if not isinstance(it["edges"], list):
            it.pop("edges")
        else:
            edges = []
            for e in it["edges"]:
                if not (isinstance(e, dict) and isinstance(e.get("to"), str)
                        and e["to"].strip() and isinstance(e.get("rel"), str)):
                    continue                 # a malformed edge entry drops, not the item
                e = dict(e, to=e["to"].strip())
                if "w" in e:
                    try:
                        w = float(e["w"])
                        if not 0.0 < w <= 1.0:
                            raise ValueError
                        e["w"] = w
                    except (TypeError, ValueError):
                        e.pop("w")           # default_edge_weight applies at merge
                edges.append(e)
            it["edges"] = edges
    if "part_of" in it:
        if not isinstance(it["part_of"], list):
            it.pop("part_of")
        else:
            it["part_of"] = [p for p in it["part_of"]
                             if isinstance(p, dict) and p.get("topic")]
    for key in ("summary", "lang", "body", "content_sha"):
        if key in it and not isinstance(it[key], str):
            it.pop(key)
    return it, None


def apply_derivation(project_root: Path, derivation_path: Path,
                     amg_root: Optional[Path] = None) -> Dict[str, Any]:
    """Apply derivation items to the graph. Two item shapes are supported:

      * update : {id, summary?, lang?, edges?, part_of?, body?, content_sha?} -> update
        the node with that id. Several items may target the SAME node (e.g. a part_of
        item plus a supersedes-edge item); each accumulates onto it. If content_sha is
        present and no longer equals the node's source_hash (the source changed since
        the item was derived), the item is SKIPPED (skipped_stale) — resumable
        derivation never applies a summary built against stale content (task 13).
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

    Robust per item (audit 1.43): each item is sanitized (_sanitize_item) and
    applied under its own guard, so a malformed one is repaired or skipped
    (`skipped_invalid` + reasons) without aborting the batch; edge targets are
    canonicalized against the existing node set (audit 1.42) before merging.
    """
    amg_root = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg_root)
    items = json.loads(Path(derivation_path).read_text(encoding="utf-8"))
    if not isinstance(items, list):
        items = [items]
    config = load_config(amg_root) or {}
    default_lang = config.get("working_language", "en")
    weights_cfg = config.get("weights") or {}
    renormalize = bool(weights_cfg.get("part_of_renormalize", True))
    default_w = float(weights_cfg.get("default_edge_weight", 0.5))
    # default_confidence lives in the verification block (Stage 13); a top-level key is
    # still honored for back-compat, then the constant.
    default_conf = _clamp01((config.get("verification") or {}).get(
        "default_confidence", config.get("default_confidence", DEFAULT_CONFIDENCE)))
    cache_enabled = bool(config.get("derivation_cache", True))
    cache_items: Dict[str, List[Dict[str, Any]]] = {}
    applied, created, skipped, skipped_stale = 0, 0, 0, 0
    skipped_invalid = 0
    invalid: List[str] = []

    def _note_invalid(reason: str) -> None:
        nonlocal skipped_invalid
        skipped_invalid += 1
        if len(invalid) < 10:
            invalid.append(reason)

    with store.lock():
        store.recover()
        nodes = load_nodes(store)
        known = set(nodes)
        sfx = _build_suffix_index(known)
        tx = store.transaction()
        for raw_item in items:
            item, reason = _sanitize_item(raw_item)
            if item is None:
                _note_invalid(reason or "invalid item")
                continue
            # Cache the sanitized item BEFORE normalization (audit 1.46): targets are
            # re-bound against whatever graph state a future restore sees, so the
            # cache stays a faithful record of what the model said (cleaned).
            cache_copy = (json.loads(json.dumps(item))
                          if cache_enabled and item.get("content_sha") else None)
            if item.get("edges"):            # bind targets to canonical ids (1.42)
                item["edges"] = _normalize_edges(item["edges"], known, sfx)
            try:
                node = nodes.get(item["id"])
                if node is None:
                    if "type" in item:                       # synthesized node (e.g. a hub)
                        path = node_relpath(item["id"], "_hubs")
                        meta = {
                            "id": item["id"], "type": item["type"],
                            "source_kind": "synthesized", "policy": "authored",
                            "source_hash": None, "derived_from_hash": None,
                            "provenance": _synth_provenance(item),
                            "confidence": _clamp01(item.get("confidence", default_conf),
                                                   default_conf),
                            "verification": _fresh_verification(),
                            "part_of": item.get("part_of", []),
                            "edges": [dict(e, coact=e.get("coact", 0),
                                           origin=e.get("origin", "synthesized"))
                                      for e in item.get("edges", [])],
                            "lang": item.get("lang", default_lang),
                            "status": "active", "summary": item.get("summary", ""),
                            "updated": _now(),
                        }
                        nodes[item["id"]] = dict(meta, _path=path, _body=item.get("body", ""))
                        known.add(item["id"])
                        tx.write(path, serialize_node(meta, item.get("body", "")))
                        created += 1
                    else:
                        skipped += 1                          # update for an unknown id
                    continue
                # Resumable derivation (task 13): a derived item echoes the content_sha it was
                # built from. If the source changed since (the node's source_hash moved on),
                # applying it would attach a summary for STALE content AND mark the node derived
                # for the NEW hash — a blind stale derivation. Skip it; the node stays stale and
                # the next reconcile re-queues it. Only for source-derived nodes (synthesized/
                # authored carry source_hash null -> no check, so a leftover hub item still applies).
                isha = item.get("content_sha")
                if isha and node.get("source_hash") and isha != node["source_hash"]:
                    skipped_stale += 1
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
                # Confidence estimate from the builder (Stage 13 task 3): an explicit value
                # wins; otherwise a node that just earned a summary takes the default once.
                if "confidence" in item:
                    node["confidence"] = _clamp01(item["confidence"], default_conf)
                elif "summary" in item and node.get("confidence") is None:
                    node["confidence"] = default_conf
                if "summary" in item or node.get("source_kind") != "derived_from_file":
                    node["derived_from_hash"] = node.get("source_hash")
                    node["status"] = "active"
                node["updated"] = _now()
                if "body" in item:
                    node["_body"] = item["body"]
                meta = {k: v for k, v in node.items() if not k.startswith("_")}
                tx.write(node["_path"], serialize_node(meta, node.get("_body", "")))
                applied += 1
                if cache_copy is not None:     # applied update item -> persist (1.46)
                    cache_items.setdefault(cache_copy["content_sha"], []).append(cache_copy)
            except Exception as exc:         # one bad item must not sink the batch (1.43)
                _note_invalid(f"{item.get('id')}: {type(exc).__name__}: {exc}")
        txid = tx.commit()
        if txid:
            _refresh_index(store.root, tx)     # warm the read-index under the lock
            store.append_log(                  # transactional audit line (1.15)
                "reconcile",
                f"apply: applied={applied} created={created} skipped={skipped} "
                f"invalid={skipped_invalid}", txid)
        if cache_items:                        # under the lock; atomic per-file writes
            _cache_store(store.root, default_lang, cache_items)

    result: Dict[str, Any] = {"applied": applied, "created": created,
                              "skipped_missing": skipped, "skipped_stale": skipped_stale,
                              "skipped_invalid": skipped_invalid}
    if invalid:
        result["invalid"] = invalid
    return result


def _merge_part_of(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]],
                   renormalize: bool) -> List[Dict[str, Any]]:
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


def _merge_edges(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]],
                 default_origin: str = "semantic", default_w: float = 0.5) -> List[Dict[str, Any]]:
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
# Connectivity metrics: the build-acceptance gate (stage 19, audit 1.44)
# --------------------------------------------------------------------------- #

# Advisory thresholds for the acceptance verdict; overridable via the
# `connectivity_gate` config block. A healthy fully-built graph is ONE large
# component with no unresolved internal targets (external `imports` don't count).
GATE_DEFAULTS: Dict[str, Any] = {"min_largest_share": 0.9, "max_dangling_internal": 0}


def graph_metrics(nodes: Dict[str, Dict[str, Any]],
                  gate_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Connectivity / build-quality metrics over a loaded node set (audit 1.44).

    Lives in the reconcile layer deliberately: graph_store is domain-blind, so
    fragmentation metrics belong where the data model is read; lifecycle.status
    surfaces them. Connectivity follows the same view retrieval conducts on — edges
    whose target exists plus part_of memberships that name a node, symmetrized.

    Dangling edges are split by legitimacy: an unresolved `imports` target is an
    external module (correctly dead, kept as a record of the import fact); ANY other
    unresolved target is an internal miss the resolver could not bind (audits
    1.40/1.42). Deferred `stale` nodes are reported but never counted as defects —
    under lazy derivation an underived node is an expected state, not fragmentation
    (roadmap §4.10); their structural backbone keeps them connected regardless.

    The verdict (`gate`: ok | attention) compares against the `connectivity_gate`
    thresholds and is ADVISORY: a skeleton mid-build is legitimately unlinked, so
    nothing fails hard — the bootstrap skill reads the verdict as its acceptance
    gate and reacts (run the global linker, inspect the samples).
    """
    gate = {**GATE_DEFAULTS, **(gate_cfg or {})}
    ids = set(nodes)
    parent: Dict[str, str] = {nid: nid for nid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    edges_total = resolved = dangling_internal = dangling_external = 0
    dangling_samples: List[str] = []
    linked: Set[str] = set()
    for nid, node in nodes.items():
        for e in node.get("edges") or []:
            if not isinstance(e, dict) or not e.get("to"):
                continue
            edges_total += 1
            if e["to"] in ids:
                resolved += 1
                union(nid, e["to"])
                linked.add(nid)
                linked.add(e["to"])
            elif e.get("rel") == "imports":
                dangling_external += 1       # stdlib / third-party: legitimately dead
            else:
                dangling_internal += 1
                if len(dangling_samples) < 10:
                    dangling_samples.append(f"{nid} -{e.get('rel')}-> {e['to']}")
        for p in node.get("part_of") or []:
            if isinstance(p, dict) and p.get("topic") in ids:
                union(nid, p["topic"])
                linked.add(nid)
                linked.add(p["topic"])

    comp_sizes: Dict[str, int] = defaultdict(int)
    for nid in ids:
        comp_sizes[find(nid)] += 1
    largest = max(comp_sizes.values()) if comp_sizes else 0
    share = round(largest / len(ids), 4) if ids else 1.0
    isolated = sorted(nid for nid in ids if nid not in linked)

    # Doc nodes with no outgoing `documents` edge (stage 19, task 6). Advisory: a
    # chat/session turn or a plain note legitimately documents nothing; stale
    # (not-yet-derived) nodes are excluded — they could not have earned one yet.
    doc_undocumented = sorted(
        nid for nid, n in nodes.items()
        if (n.get("_path") or "").startswith("nodes/doc/")
        and n.get("status") != "stale"
        and not any(isinstance(e, dict) and e.get("rel") == "documents"
                    for e in n.get("edges") or []))

    ok = (share >= float(gate["min_largest_share"])
          and dangling_internal <= int(gate["max_dangling_internal"]))
    return {
        "nodes": len(ids),
        "components": len(comp_sizes),
        "largest_component_share": share,
        "isolated_nodes": len(isolated),
        "isolated_sample": isolated[:10],
        "edges_total": edges_total,
        "edges_resolved": resolved,
        "dangling_internal": dangling_internal,
        "dangling_internal_sample": dangling_samples,
        "dangling_external_imports": dangling_external,
        "doc_without_documents": len(doc_undocumented),
        "doc_without_documents_sample": doc_undocumented[:10],
        "stale_nodes": sum(1 for n in nodes.values() if n.get("status") == "stale"),
        "gate": "ok" if ok else "attention",
    }


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
        summary = plan(project_root, amg_root)
        # Restore cache hits automatically (audit 1.46): after this, the printed
        # queued_for_semantic is the REAL model work left — a rebuild over unchanged
        # content derives nothing.
        if summary.get("queued_for_semantic"):
            cached = apply_cached(project_root, amg_root)
            if cached.get("restored_units"):
                summary["restored_from_cache"] = cached["restored_units"]
                summary["queued_for_semantic"] = cached.get(
                    "remaining", summary["queued_for_semantic"])
        print(json.dumps(summary, indent=2))
        return 0

    if cmd == "apply-cached":
        project_root = Path(args[1]).resolve() if len(args) > 1 else Path.cwd()
        amg_root = gs.resolve_amg_root(cli_root, project_root)
        print(json.dumps(apply_cached(project_root, amg_root), indent=2))
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

    if cmd == "metrics":
        # Read-only connectivity report (audit 1.44). The gate thresholds come from
        # the local config read tolerantly — a diagnostic must not exit on a missing
        # or odd config the way extraction deliberately does.
        project_root = Path(args[1]).resolve() if len(args) > 1 else Path.cwd()
        amg_root = gs.resolve_amg_root(cli_root, project_root)
        gate_cfg: Dict[str, Any] = {}
        cfg_file = amg_root / "config.yml"
        if cfg_file.exists():
            try:
                raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict) and isinstance(raw.get("connectivity_gate"), dict):
                    gate_cfg = raw["connectivity_gate"]
            except (OSError, yaml.YAMLError):
                gate_cfg = {}
        store = gs.GraphStore(amg_root)
        print(json.dumps(graph_metrics(load_nodes(store), gate_cfg),
                         ensure_ascii=False, indent=2))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
