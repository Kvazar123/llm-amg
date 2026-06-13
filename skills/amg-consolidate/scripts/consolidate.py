#!/usr/bin/env python3
"""
consolidate.py — close the AMG memory loop. Crash-safe & idempotent.

Three jobs, mirroring how memory is maintained:

  weights : fold the co-activation log into edge weights (Hebbian reinforcement +
            passive decay + pruning + part_of renormalization). Fully deterministic,
            no LLM. "What fires together wires together; what is unused fades."

  plan    : analyze the graph and emit a work plan for the consolidator subagent:
            per-branch budget overflow (what to compact, in staged order),
            near-duplicate candidates, stale episodic candidates, and a deterministic
            salience score per episodic note (the soft signals are judged by the LLM).

  apply   : apply the consolidator's action list transactionally. Actions:
            promote / merge / summarize_episodes / introduce_subhub / shorten / retire.
            Originals are ARCHIVED (reversible), never silently destroyed, and every
            action is logged. Compaction is staged and stops once a branch is back
            under budget (minimal necessary compression).

Compaction never runs unless a branch exceeds its budget; protected node types
(decisions/ADRs) and high-centrality nodes are never collapsed or shortened.

All writes go through the graph_store journal, so an interruption at any point
recovers via `graph_store.py recover` on the next run.

CLI:
  python consolidate.py weights [<project_root>] [--root <agent_dir>]
  python consolidate.py plan    [<project_root>] [--root <agent_dir>]
  python consolidate.py apply <actions.json> [<project_root>] [--root <agent_dir>]

The graph root is <agent_dir>/amg, resolved by graph_store.resolve_amg_root:
--root -> AMG_AGENT_DIR env -> config search upward from <project_root> ->
the engine's own location -> the default <project_root>/.claude.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Import the crash-safe store from the bootstrap skill (fixed relative layout).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "amg-bootstrap" / "scripts"))
import graph_store as gs                                   # noqa: E402

try:
    import yaml
except ImportError:                                        # pragma: no cover
    sys.stderr.write("consolidate.py needs PyYAML: pip install pyyaml\n")
    raise

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
WORD_RE = re.compile(r"\w+", re.UNICODE)   # Unicode: must match non-Latin scripts too

DEFAULTS = {
    "hebbian_rate": 0.10, "decay_rate": 0.02, "prune_below": 0.05,
    "part_of_renormalize": True, "default_edge_weight": 0.5,
    # Hebbian weight updates are OFF by default: the co-activation signal is partly
    # circular (PPR weights -> pack -> co-activated pairs -> the same weights), so
    # `weights` only ACCUMULATES coact (which feeds salience) until an eval on/off
    # comparison proves the updates help retrieval (roadmap task 14). Flip on via
    # weights.apply_hebbian once measured.
    "apply_hebbian": False,
    "compaction": {
        "enabled": True,
        "default_branch_budget_nodes": 150,
        "default_branch_budget_tokens": 60000,
        "protect_types": ["decision", "adr"],
        "protect_min_centrality": 0.7,
        "archive_dir": "archive",
        "steps": ["summarize_episodes", "merge_near_duplicates",
                  "introduce_subhub", "lossy_shorten"],
    },
    "near_duplicate_sim": 0.82,
    "episodic_types": ["section", "note"],
    "stale_age_days": 30,
}

# Actions that COMPRESS the graph (vs. promote, which only raises a status). When
# compaction.enabled is false these are skipped unless the action carries
# force:true; a protected node (protect_types / high centrality) is likewise
# spared from a destructive action without force.
COMPACTION_ACTIONS = {"summarize_episodes", "merge", "introduce_subhub",
                      "shorten", "retire"}

# Relations that GROUND a node in code/docs (a spec/impl/doc points AT it). Both a
# node's own outgoing such edges and INBOUND ones count as provenance for salience
# (2.8 p.6): a code node that a doc documents is grounded even with no outgoing edge.
GROUND_RELS = {"documents", "implements", "specifies"}

# "Containment-ish" relations a hub follows DOWNWARD to reach its branch (1.20). A
# leaf's primary part_of points at a directory STRING (not a node), so the upward
# part_of walk alone leaves over_budget_branches empty on a real graph; following a
# hub's own outgoing structural edges (and a module's `defines` to its functions)
# down to non-hub nodes recovers the branch. The walk stops at any other hub.
HUB_DOWN_RELS = {"documents", "defines", "specifies", "implements", "contains"}


# --------------------------------------------------------------------------- #
# Node IO
# --------------------------------------------------------------------------- #

def load_config(amg_root: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))               # deep copy
    cfg["working_language"] = "en"          # summaries' language for created nodes
    f = amg_root / "config.yml"
    if f.exists():
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for key in ("hebbian_rate", "decay_rate", "prune_below", "part_of_renormalize",
                    "default_edge_weight", "apply_hebbian"):
            if key in (raw.get("weights") or {}):
                cfg[key] = raw["weights"][key]
        if "compaction" in raw:
            cfg["compaction"].update(raw["compaction"] or {})
        # plan tunables previously frozen in code (1.17): now overridable from config
        for key in ("near_duplicate_sim", "episodic_types", "stale_age_days"):
            if key in raw:
                cfg[key] = raw[key]
        cfg["working_language"] = raw.get("working_language", "en")
    return cfg


def _parse(text: str) -> Optional[Tuple[dict, str]]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    return (yaml.safe_load(m.group(1)) or {}), m.group(2)


def serialize(meta: dict, body: str) -> str:
    clean = {k: v for k, v in meta.items() if not k.startswith("_")}
    fm = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body or ''}".rstrip() + "\n"


def load_nodes(store: gs.GraphStore) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for p in store.nodes_dir.rglob("*.md"):
        parsed = _parse(p.read_text(encoding="utf-8", errors="replace"))
        if not parsed:
            continue
        meta, body = parsed
        if not meta.get("id"):
            continue
        meta["_path"] = p.relative_to(store.root).as_posix()
        meta["_body"] = body
        out[meta["id"]] = meta
    return out


def _toklen(text: str) -> int:
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# weights: Hebbian + decay + prune + renormalize
# --------------------------------------------------------------------------- #

def fold_weights(project_root: Path, amg_root: Optional[Path] = None) -> dict:
    amg = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg)
    store.init()
    cfg = load_config(amg)
    eta, lam, prune = cfg["hebbian_rate"], cfg["decay_rate"], cfg["prune_below"]
    default_w = cfg["default_edge_weight"]
    apply_hebbian = bool(cfg.get("apply_hebbian", False))

    log_path = store.root / "work" / "coactivation.log"
    pair_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for u, v in rec.get("coactivated", []):
                pair_counts[tuple(sorted((u, v)))] += 1
    max_co = max(pair_counts.values()) if pair_counts else 1
    # Touch w (decay + reinforcement + prune) ONLY when Hebbian updates are enabled
    # AND a new co-activation journal exists. Otherwise just ACCUMULATE coact (which
    # feeds salience) and leave w alone: the signal is partly circular, so weight
    # updates stay off until eval proves them (task 14); tying decay to the journal's
    # presence also makes a no-signal re-run a w-no-op (audit 1.9 idempotency).
    update_w = apply_hebbian and bool(pair_counts)

    with store.lock():
        store.recover()
        nodes = load_nodes(store)
        tx = store.transaction()
        changed = 0

        def co_for(u: str, v: str) -> int:
            return pair_counts.get(tuple(sorted((u, v))), 0)

        for nid, node in nodes.items():
            edges = node.get("edges") or []
            if not edges and not (node.get("part_of") and cfg["part_of_renormalize"]):
                continue
            kept = []
            touched = False
            for e in edges:
                if not isinstance(e, dict) or not e.get("to"):
                    kept.append(e)
                    continue
                co = co_for(nid, e["to"])
                if co > 0:                                # accumulate the signal ALWAYS
                    e["coact"] = int(e.get("coact", 0)) + co
                    touched = True
                if update_w:
                    w = float(e.get("w", default_w)) - lam    # passive decay
                    if co > 0:                                # Hebbian reinforcement
                        w += eta * (co / max_co)
                    w = max(0.0, min(1.0, w))
                    e["w"] = round(w, 4)
                    touched = True
                    if w < prune:
                        continue                          # faded edge dropped (pruned)
                kept.append(e)
            if kept != edges:
                node["edges"] = kept
                touched = True
            # renormalize part_of so memberships sum to <= 1
            if cfg["part_of_renormalize"] and node.get("part_of"):
                s = sum(float(p.get("w", 0)) for p in node["part_of"] if isinstance(p, dict))
                if s > 1.0:
                    for p in node["part_of"]:
                        if isinstance(p, dict):
                            p["w"] = round(float(p.get("w", 0)) / s, 4)
                    touched = True
            if touched:
                tx.write(node["_path"], serialize(node, node["_body"]))
                changed += 1

        # rotate the co-activation log into the archive (auditable, then cleared)
        if log_path.exists() and pair_counts:
            arch = f"{cfg['compaction']['archive_dir']}/coactivation-{int(time.time())}.log"
            tx.write(arch, log_path.read_text(encoding="utf-8"))
            tx.delete("work/coactivation.log")

        txid = tx.commit()
        _log(store, f"weights folded: apply_hebbian={update_w}, "
                    f"{len(pair_counts)} co-activated pairs, {changed} nodes updated", txid)

    return {"coact_pairs": len(pair_counts),
            "reinforced_edges": len(pair_counts) if update_w else 0,
            "nodes_updated": changed, "hebbian_applied": update_w}


# --------------------------------------------------------------------------- #
# plan: branch budgets, salience, duplicate & episodic candidates
# --------------------------------------------------------------------------- #

def _node_text(node: dict) -> List[str]:
    txt = " ".join([str(node.get("summary", "")), node.get("_body", "")[:400]])
    return [w.lower() for w in WORD_RE.findall(txt)]


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _branch_members(nodes: Dict[str, dict]) -> Dict[str, List[str]]:
    """For each hub node, the ids that belong to its branch — reached two ways:

      * upward: a node whose part_of transitively reaches the hub (explicit
        membership, incl. weighted multi-membership from synthesis);
      * downward: from the hub, following its containment-ish edges (HUB_DOWN_RELS)
        transitively to non-hub nodes (a hub documents a module, the module defines
        its functions). This makes branches computable when a leaf's primary
        membership is the directory string rather than the hub node (audit 1.20).

    The downward walk stops at any other hub, so branches don't bleed together."""
    parent_topics: Dict[str, List[str]] = {
        nid: [p.get("topic") for p in (n.get("part_of") or []) if isinstance(p, dict)]
        for nid, n in nodes.items()}
    hubs = [nid for nid, n in nodes.items() if n.get("type") in ("hub", "overview")]
    hub_set = set(hubs)
    members: Dict[str, set] = {h: set() for h in hubs}

    # upward: part_of transitively reaching a hub
    for nid in nodes:
        seen, stack = set(), list(parent_topics.get(nid, []))
        while stack:
            t = stack.pop()
            if t in seen or t not in nodes:
                continue
            seen.add(t)
            if t in members and nid != t:
                members[t].add(nid)
            stack.extend(parent_topics.get(t, []))

    # downward: hub -> containment-ish edges -> non-hub nodes, transitively
    for h in hubs:
        seen, stack = {h}, [h]
        while stack:
            cur = nodes.get(stack.pop())
            if not cur:
                continue
            for e in (cur.get("edges") or []):
                if not (isinstance(e, dict) and e.get("rel") in HUB_DOWN_RELS):
                    continue
                to = e.get("to")
                if to not in nodes or to in seen or to in hub_set:
                    continue
                seen.add(to)
                members[h].add(to)
                stack.append(to)

    return {h: sorted(m) for h, m in members.items()}


def salience(node: dict, degree: int, max_degree: int, cfg: dict,
             grounded_inbound: bool = False) -> float:
    """Deterministic value-of-information signals (LLM judges the soft ones).

    `grounded_inbound` is True when some other node points AT this one with a
    grounding relation (documents/implements/specifies) — counted as provenance
    alongside the node's own such outgoing edges (2.8 p.6)."""
    # type prior (decisions/commitments are high-value)
    typ = (node.get("type") or "").lower()
    type_score = 1.0 if typ in cfg["compaction"]["protect_types"] else 0.4
    # recency
    rec = 0.5
    try:
        age_days = (time.time() - time.mktime(time.strptime(
            str(node.get("updated", ""))[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
        rec = max(0.0, 1.0 - age_days / max(cfg["stale_age_days"] * 4, 1))
    except (ValueError, TypeError):
        pass
    # frequency (accumulated co-activation) + bridging (degree centrality)
    coact = sum(int(e.get("coact", 0)) for e in (node.get("edges") or [])
                if isinstance(e, dict))
    freq = min(1.0, coact / 10.0)
    bridge = degree / max_degree if max_degree else 0.0
    # provenance (grounded in code/docs): own outgoing grounding edge, OR another
    # node grounds this one (inbound), OR it is projected from a file
    grounded = 1.0 if (node.get("source_kind") == "derived_from_file"
                       or grounded_inbound
                       or any(e.get("rel") in GROUND_RELS
                              for e in (node.get("edges") or []) if isinstance(e, dict))) else 0.4
    return round(0.30 * type_score + 0.20 * rec + 0.20 * freq +
                 0.20 * bridge + 0.10 * grounded, 3)


def _degree_map(nodes: Dict[str, dict]) -> Tuple[Dict[str, int], int]:
    """Degree centrality over edges whose target is a known node (both endpoints
    counted), and the max degree. Shared by the plan (salience bridging) and apply
    (high-centrality protection) so both judge centrality identically."""
    degree: Dict[str, int] = defaultdict(int)
    for nid, n in nodes.items():
        for e in (n.get("edges") or []):
            if isinstance(e, dict) and e.get("to") in nodes:
                degree[nid] += 1
                degree[e["to"]] += 1
    return degree, (max(degree.values()) if degree else 1)


def _is_protected(node: Optional[dict], degree: Dict[str, int], max_deg: int,
                  cmp_cfg: dict) -> bool:
    """A node the compaction layer must not collapse/shorten/retire/archive without
    an explicit force: a protected type (decision/adr) or a node whose normalized
    degree centrality exceeds protect_min_centrality. Enforced in code, not only in
    the consolidator prompt (1.11)."""
    if node is None:
        return False
    protect = {str(t).lower() for t in cmp_cfg.get("protect_types", [])}
    if (node.get("type") or "").lower() in protect:
        return True
    deg = degree.get(node.get("id"), 0)
    centrality = deg / max_deg if max_deg else 0.0
    return centrality > cmp_cfg.get("protect_min_centrality", 0.7)


def _inbound_grounded(nodes: Dict[str, dict]) -> set:
    """Ids that some other node points AT with a grounding relation
    (documents/implements/specifies). Feeds salience's provenance signal (2.8 p.6)."""
    grounded: set = set()
    for n in nodes.values():
        for e in (n.get("edges") or []):
            if isinstance(e, dict) and e.get("rel") in GROUND_RELS and e.get("to") in nodes:
                grounded.add(e["to"])
    return grounded


def _combine_part_of(memberships: List[dict], renormalize: bool) -> List[dict]:
    """Combine memberships by topic, keeping the strongest weight per topic, then
    renormalize to the simplex (sum <= 1) when asked. Used when merge folds two
    nodes' memberships and when introduce_subhub rewrites one topic (1.21)."""
    out: Dict[str, dict] = {}
    for p in memberships:
        if not (isinstance(p, dict) and p.get("topic")):
            continue
        t = p["topic"]
        w = float(p.get("w", 0))
        if t not in out or w > out[t]["w"]:
            out[t] = {"topic": t, "w": w}
    merged = list(out.values())
    if renormalize:
        s = sum(p["w"] for p in merged)
        if s > 1.0:
            for p in merged:
                p["w"] = round(p["w"] / s, 4)
    return merged


def _dedup_edges(edges: List[dict], owner_id: str) -> List[dict]:
    """Collapse edges by (rel, to): keep the max weight and SUM coact; drop a
    self-edge (to == owner). Applied to a neighbor after redirect_inbound so a node
    that pointed at both the survivor and a dropped node ends with one edge (1.22)."""
    out: Dict[Tuple, dict] = {}
    for e in edges:
        if not isinstance(e, dict) or not e.get("to") or e["to"] == owner_id:
            continue
        key = (e.get("rel"), e["to"])
        cur = out.get(key)
        if cur is None:
            out[key] = dict(e)
        else:
            cur["w"] = max(float(cur.get("w", 0)), float(e.get("w", 0)))
            cur["coact"] = int(cur.get("coact", 0)) + int(e.get("coact", 0))
    return list(out.values())


def make_plan(project_root: Path, amg_root: Optional[Path] = None) -> dict:
    amg = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg)
    store.init()
    cfg = load_config(amg)
    nodes = load_nodes(store)

    degree, max_deg = _degree_map(nodes)

    members = _branch_members(nodes)
    cmp_cfg = cfg["compaction"]
    over_budget = []
    # compaction.enabled is a real switch: when off, no branch is ever flagged
    # over budget, so the consolidator is never handed compression work (1.8).
    if cmp_cfg.get("enabled", True):
        for hub, mem in members.items():
            budget = nodes[hub].get("branch_budget", cmp_cfg["default_branch_budget_nodes"])
            tok = sum(_toklen(serialize(nodes[m], nodes[m].get("_body", ""))) for m in mem)
            if len(mem) > budget or tok > cmp_cfg["default_branch_budget_tokens"]:
                over_budget.append({"hub": hub, "size_nodes": len(mem), "size_tokens": tok,
                                    "budget_nodes": budget, "members": mem,
                                    "staged_steps": cmp_cfg["steps"]})

    # near-duplicate candidates (lexical Jaccard over summaries)
    ids = list(nodes)
    toks = {nid: set(_node_text(nodes[nid])) for nid in ids}
    dups = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sim = _jaccard(toks[ids[i]], toks[ids[j]])
            if sim >= cfg["near_duplicate_sim"]:
                dups.append({"a": ids[i], "b": ids[j], "sim": round(sim, 3)})

    # episodic candidates + salience
    grounded_in = _inbound_grounded(nodes)
    episodic = []
    for nid, n in nodes.items():
        if (n.get("type") in cfg["episodic_types"]
                and n.get("source_kind") not in ("derived_from_file",)):
            episodic.append({"id": nid,
                             "salience": salience(n, degree[nid], max_deg, cfg,
                                                  nid in grounded_in),
                             "protected": (n.get("type") or "").lower() in cmp_cfg["protect_types"]})
    episodic.sort(key=lambda x: x["salience"])

    plan = {"generated": _now(), "n_nodes": len(nodes),
            "over_budget_branches": over_budget,
            "near_duplicates": dups,
            "episodic_candidates": episodic[:50]}
    gs.atomic_write_text(store.root / "work" / "consolidation-plan.json",
                         json.dumps(plan, ensure_ascii=False, indent=2))
    return {"over_budget": len(over_budget), "duplicates": len(dups),
            "episodic": len(episodic)}


# --------------------------------------------------------------------------- #
# apply: enact the consolidator subagent's actions (transactional + archived)
# --------------------------------------------------------------------------- #

def apply_actions(project_root: Path, actions_path: Path,
                  amg_root: Optional[Path] = None) -> dict:
    amg = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg)
    cfg = load_config(amg)
    archive_dir = cfg["compaction"]["archive_dir"]
    actions = json.loads(Path(actions_path).read_text(encoding="utf-8"))
    counts: Dict[str, int] = defaultdict(int)

    with store.lock():
        store.recover()
        nodes = load_nodes(store)
        tx = store.transaction()
        cmp_cfg = cfg["compaction"]
        enabled = cmp_cfg.get("enabled", True)
        renorm = bool(cfg.get("part_of_renormalize", True))
        degree, max_deg = _degree_map(nodes)

        def archive(nid: str):
            n = nodes.get(nid)
            if n:
                tx.write(f"{archive_dir}/{Path(n['_path']).name}", serialize(n, n["_body"]))
                tx.delete(n["_path"])

        def redirect_inbound(old_ids: set, new_id: str):
            """Repoint every edge/membership that targets an archived id to the
            survivor, then dedup the neighbor's edges by (rel,to) and drop any
            self-edge the redirect created (1.22)."""
            for n in nodes.values():
                if n["id"] in old_ids:
                    continue
                ch = False
                for e in (n.get("edges") or []):
                    if isinstance(e, dict) and e.get("to") in old_ids:
                        e["to"] = new_id
                        ch = True
                for p in (n.get("part_of") or []):
                    if isinstance(p, dict) and p.get("topic") in old_ids:
                        p["topic"] = new_id
                        ch = True
                if ch:
                    n["edges"] = _dedup_edges(n.get("edges") or [], n["id"])
                    tx.write(n["_path"], serialize(n, n["_body"]))

        def newpath(nid: str, kind="notes"):
            slug = re.sub(r"[^\w.-]+", "_", nid.split(":", 1)[-1]).strip("_")[:48] or "node"
            h = gs.sha256_text(nid)[:8]
            return f"nodes/{kind}/{slug}-{h}.md"

        for act in actions:
            kind = act.get("action")
            force = bool(act.get("force"))

            # compaction.enabled off blocks every compression action unless forced (1.8)
            if kind in COMPACTION_ACTIONS and not enabled and not force:
                counts["skipped_disabled"] += 1
                continue
            # protected nodes are never collapsed/shortened/retired/archived without
            # force — enforced here in code, not only in the consolidator prompt (1.11)
            if not force:
                if kind in ("shorten", "retire"):
                    guard = [act.get("id")]
                elif kind == "merge":
                    guard = list(act.get("drop_ids", []))
                elif kind == "summarize_episodes":
                    guard = list(act.get("archive_ids", []))
                else:
                    guard = []
                if any(_is_protected(nodes.get(t), degree, max_deg, cmp_cfg)
                       for t in guard if t):
                    counts["skipped_protected"] += 1
                    continue

            if kind == "promote":
                n = nodes.get(act["id"])
                if not n:
                    continue
                if act.get("new_type"):
                    n["type"] = act["new_type"]
                n["status"] = act.get("status", "active")
                n["updated"] = _now()
                tx.write(n["_path"], serialize(n, n["_body"]))
                counts["promote"] += 1

            elif kind == "retire":
                archive(act["id"])
                counts["retire"] += 1

            elif kind == "shorten":
                n = nodes.get(act["id"])
                if not n:
                    continue
                # Save the full original ONCE: a repeated apply must not overwrite
                # the archived original with the already-shortened version (1.10).
                full_rel = f"{archive_dir}/{Path(n['_path']).name}.full"
                if not store.abspath(full_rel).exists():
                    tx.write(full_rel, serialize(n, n["_body"]))
                if "summary" in act:
                    n["summary"] = act["summary"]
                n["_body"] = act.get("body", "")
                n["updated"] = _now()
                tx.write(n["_path"], serialize(n, n["_body"]))
                counts["shorten"] += 1

            elif kind == "merge":
                keep_id = act["keep_id"]
                keep = nodes.get(keep_id)
                if not keep:
                    continue
                drop = set(act.get("drop_ids", []))
                # fold edges by (rel,to): max weight + summed coact; drop a self-edge
                # (a dropped node pointing back at keep) and edges into the drop set
                folded: Dict[Tuple[str, str], dict] = {}

                def _fold(e: dict) -> None:
                    if not isinstance(e, dict) or not e.get("to"):
                        return
                    if e["to"] == keep_id or e["to"] in drop:
                        return
                    key = (e.get("rel"), e["to"])
                    cur = folded.get(key)
                    if cur is None:
                        folded[key] = dict(e)
                    else:
                        cur["w"] = max(float(cur.get("w", 0)), float(e.get("w", 0)))
                        cur["coact"] = int(cur.get("coact", 0)) + int(e.get("coact", 0))

                for e in (keep.get("edges") or []):
                    _fold(e)
                memberships = list(keep.get("part_of") or [])
                for did in drop:
                    dn = nodes.get(did)
                    if not dn:
                        continue
                    for e in (dn.get("edges") or []):
                        _fold(e)
                    memberships += (dn.get("part_of") or [])   # fold the dropped node's homes
                    archive(did)
                keep["edges"] = list(folded.values())
                keep["part_of"] = _combine_part_of(memberships, renorm)
                if "summary" in act:
                    keep["summary"] = act["summary"]
                if "body" in act:
                    keep["_body"] = act["body"]
                keep["updated"] = _now()
                tx.write(keep["_path"], serialize(keep, keep["_body"]))
                redirect_inbound(drop, keep_id)
                counts["merge"] += 1

            elif kind == "summarize_episodes":
                nid = act["new_id"]
                # synthesized node: same canon as reconcile.apply_derivation
                # (policy authored, no source/derived hash); lands in _hubs, since
                # the data model routes source_kind==synthesized there (2.8 p.5)
                meta = {"id": nid, "type": act.get("type", "section"),
                        "source_kind": "synthesized", "policy": "authored",
                        "source_hash": None, "derived_from_hash": None,
                        "summary": act.get("summary", ""),
                        "part_of": act.get("part_of", []),
                        "edges": [dict(e, origin=e.get("origin", "consolidation"))
                                  for e in act.get("edges", [])],
                        "lang": act.get("lang", cfg["working_language"]),
                        "status": "active", "updated": _now()}
                tx.write(newpath(nid, "_hubs"), serialize(meta, act.get("body", "")))
                arch_ids = set(act.get("archive_ids", []))
                for aid in arch_ids:
                    archive(aid)
                redirect_inbound(arch_ids, nid)
                counts["summarize_episodes"] += 1

            elif kind == "introduce_subhub":
                hub_id = act["hub_id"]
                parent_topic = act.get("parent_topic")
                meta = {"id": hub_id, "type": "hub", "source_kind": "synthesized",
                        "policy": "authored", "source_hash": None,
                        "derived_from_hash": None, "summary": act.get("summary", ""),
                        "part_of": ([{"topic": parent_topic, "w": 1.0}]
                                    if parent_topic else []),
                        "edges": [], "lang": act.get("lang", cfg["working_language"]),
                        "status": "active", "updated": _now()}
                tx.write(newpath(hub_id, "_hubs"), serialize(meta, ""))
                for mid in act.get("member_ids", []):
                    mn = nodes.get(mid)
                    if not mn:
                        continue
                    # rewrite ONLY the parent topic to the sub-hub; keep the member's
                    # other memberships, renormalized (1.21). If it wasn't under the
                    # parent topic, just add the sub-hub membership without erasing.
                    new_po, replaced = [], False
                    for p in (mn.get("part_of") or []):
                        if not isinstance(p, dict):
                            continue
                        if p.get("topic") == parent_topic:
                            new_po.append({"topic": hub_id, "w": p.get("w", 1.0)})
                            replaced = True
                        else:
                            new_po.append(dict(p))
                    if not replaced:
                        new_po.append({"topic": hub_id, "w": 1.0})
                    mn["part_of"] = _combine_part_of(new_po, renorm)
                    mn["updated"] = _now()
                    tx.write(mn["_path"], serialize(mn, mn["_body"]))
                counts["introduce_subhub"] += 1

        txid = tx.commit()
        _log(store, f"consolidation applied: {dict(counts)}", txid)

    return dict(counts)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _log(store: gs.GraphStore, msg: str, txid: Optional[str]) -> None:
    try:
        line = f"## [{_now()}] {txid or '-'} consolidate | {msg}\n"
        with open(store.root / "log.md", "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "help"
    if cmd == "weights":
        root = Path(args[1]).resolve() if len(args) > 1 else Path.cwd()
        print(json.dumps(fold_weights(root, gs.resolve_amg_root(cli_root, root)), indent=2)); return 0
    if cmd == "plan":
        root = Path(args[1]).resolve() if len(args) > 1 else Path.cwd()
        print(json.dumps(make_plan(root, gs.resolve_amg_root(cli_root, root)), indent=2)); return 0
    if cmd == "apply":
        if len(args) < 2:
            print("usage: consolidate.py apply <actions.json> [<project_root>] "
                  "[--root <agent_dir>]"); return 2
        root = Path(args[2]).resolve() if len(args) > 2 else Path.cwd()
        print(json.dumps(apply_actions(root, Path(args[1]),
                                       gs.resolve_amg_root(cli_root, root)), indent=2)); return 0
    print(__doc__); return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
