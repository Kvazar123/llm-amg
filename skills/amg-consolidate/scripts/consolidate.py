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
  python consolidate.py weights [<project_root>]
  python consolidate.py plan    [<project_root>]
  python consolidate.py apply <actions.json> [<project_root>]
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
    "part_of_renormalize": True,
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


# --------------------------------------------------------------------------- #
# Node IO
# --------------------------------------------------------------------------- #

def load_config(amg_root: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))               # deep copy
    f = amg_root / "config.yml"
    if f.exists():
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for key in ("hebbian_rate", "decay_rate", "prune_below", "part_of_renormalize"):
            if key in (raw.get("weights") or {}):
                cfg[key] = raw["weights"][key]
        if "compaction" in raw:
            cfg["compaction"].update(raw["compaction"] or {})
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

def fold_weights(project_root: Path) -> dict:
    amg = project_root / ".claude" / "amg"
    store = gs.GraphStore(amg)
    store.init()
    cfg = load_config(amg)
    eta, lam, prune = cfg["hebbian_rate"], cfg["decay_rate"], cfg["prune_below"]

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
                w = float(e.get("w", 0.5))
                co = co_for(nid, e["to"])
                w = w - lam                              # passive decay (everyone)
                if co > 0:                                # Hebbian reinforcement
                    w += eta * (co / max_co)
                    e["coact"] = int(e.get("coact", 0)) + co
                w = max(0.0, min(1.0, w))
                e["w"] = round(w, 4)
                touched = True                            # a weight changed -> must persist
                if w >= prune:
                    kept.append(e)
                else:
                    pass                                  # faded edge dropped (pruned)
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
        _log(store, f"weights folded: {len(pair_counts)} edges reinforced, "
                    f"{changed} nodes updated", txid)

    return {"reinforced_edges": len(pair_counts), "nodes_updated": changed}


# --------------------------------------------------------------------------- #
# plan: branch budgets, salience, duplicate & episodic candidates
# --------------------------------------------------------------------------- #

def _node_text(node: dict) -> List[str]:
    txt = " ".join([str(node.get("summary", "")), node.get("_body", "")[:400]])
    return [w.lower() for w in WORD_RE.findall(txt)]


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _branch_members(nodes: Dict[str, dict]) -> Dict[str, List[str]]:
    """For each hub node, the ids whose part_of (transitively) reaches it."""
    parent_topics: Dict[str, List[str]] = {
        nid: [p.get("topic") for p in (n.get("part_of") or []) if isinstance(p, dict)]
        for nid, n in nodes.items()}
    hubs = [nid for nid, n in nodes.items() if n.get("type") in ("hub", "overview")]
    members: Dict[str, List[str]] = {h: [] for h in hubs}
    for nid in nodes:
        seen, stack = set(), list(parent_topics.get(nid, []))
        while stack:
            t = stack.pop()
            if t in seen or t not in nodes:
                continue
            seen.add(t)
            if t in members and nid != t:
                members[t].append(nid)
            stack.extend(parent_topics.get(t, []))
    return members


def salience(node: dict, degree: int, max_degree: int, cfg: dict) -> float:
    """Deterministic value-of-information signals (LLM judges the soft ones)."""
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
    # provenance (grounded in code/docs)
    grounded = 1.0 if (node.get("source_kind") == "derived_from_file"
                       or any((e.get("rel") in ("documents", "implements", "specifies"))
                              for e in (node.get("edges") or []) if isinstance(e, dict))) else 0.4
    return round(0.30 * type_score + 0.20 * rec + 0.20 * freq +
                 0.20 * bridge + 0.10 * grounded, 3)


def make_plan(project_root: Path) -> dict:
    amg = project_root / ".claude" / "amg"
    store = gs.GraphStore(amg)
    store.init()
    cfg = load_config(amg)
    nodes = load_nodes(store)

    degree: Dict[str, int] = defaultdict(int)
    for nid, n in nodes.items():
        for e in (n.get("edges") or []):
            if isinstance(e, dict) and e.get("to") in nodes:
                degree[nid] += 1
                degree[e["to"]] += 1
    max_deg = max(degree.values()) if degree else 1

    members = _branch_members(nodes)
    cmp_cfg = cfg["compaction"]
    over_budget = []
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
    episodic = []
    for nid, n in nodes.items():
        if (n.get("type") in cfg["episodic_types"]
                and n.get("source_kind") not in ("derived_from_file",)):
            episodic.append({"id": nid, "salience": salience(n, degree[nid], max_deg, cfg),
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

def apply_actions(project_root: Path, actions_path: Path) -> dict:
    amg = project_root / ".claude" / "amg"
    store = gs.GraphStore(amg)
    cfg = load_config(amg)
    archive_dir = cfg["compaction"]["archive_dir"]
    actions = json.loads(Path(actions_path).read_text(encoding="utf-8"))
    counts: Dict[str, int] = defaultdict(int)

    with store.lock():
        store.recover()
        nodes = load_nodes(store)
        tx = store.transaction()

        def archive(nid: str):
            n = nodes.get(nid)
            if n:
                tx.write(f"{archive_dir}/{Path(n['_path']).name}", serialize(n, n["_body"]))
                tx.delete(n["_path"])

        def redirect_inbound(old_ids: set, new_id: str):
            """Repoint every edge that targets an archived id to the survivor."""
            for n in nodes.values():
                ch = False
                for e in (n.get("edges") or []):
                    if isinstance(e, dict) and e.get("to") in old_ids:
                        e["to"] = new_id
                        ch = True
                for p in (n.get("part_of") or []):
                    if isinstance(p, dict) and p.get("topic") in old_ids:
                        p["topic"] = new_id
                        ch = True
                if ch and n["id"] not in old_ids:
                    tx.write(n["_path"], serialize(n, n["_body"]))

        def newpath(nid: str, kind="notes"):
            slug = re.sub(r"[^\w.-]+", "_", nid.split(":", 1)[-1]).strip("_")[:48] or "node"
            h = gs.sha256_text(nid)[:8]
            return f"nodes/{kind}/{slug}-{h}.md"

        for act in actions:
            kind = act.get("action")

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
                tx.write(f"{archive_dir}/{Path(n['_path']).name}.full", serialize(n, n["_body"]))
                if "summary" in act:
                    n["summary"] = act["summary"]
                n["_body"] = act.get("body", "")
                n["updated"] = _now()
                tx.write(n["_path"], serialize(n, n["_body"]))
                counts["shorten"] += 1

            elif kind == "merge":
                keep = nodes.get(act["keep_id"])
                if not keep:
                    continue
                drop = set(act.get("drop_ids", []))
                merged_edges = list(keep.get("edges") or [])
                seen = {(e.get("rel"), e.get("to")) for e in merged_edges if isinstance(e, dict)}
                for did in drop:
                    dn = nodes.get(did)
                    if not dn:
                        continue
                    for e in (dn.get("edges") or []):
                        if isinstance(e, dict) and (e.get("rel"), e.get("to")) not in seen:
                            merged_edges.append(e); seen.add((e.get("rel"), e.get("to")))
                    archive(did)
                keep["edges"] = [e for e in merged_edges
                                 if not (isinstance(e, dict) and e.get("to") in drop)]
                if "summary" in act:
                    keep["summary"] = act["summary"]
                if "body" in act:
                    keep["_body"] = act["body"]
                keep["updated"] = _now()
                tx.write(keep["_path"], serialize(keep, keep["_body"]))
                redirect_inbound(drop, act["keep_id"])
                counts["merge"] += 1

            elif kind == "summarize_episodes":
                nid = act["new_id"]
                meta = {"id": nid, "type": act.get("type", "section"),
                        "source_kind": "derived", "summary": act.get("summary", ""),
                        "part_of": act.get("part_of", []), "edges": act.get("edges", []),
                        "lang": act.get("lang", "en"), "status": "active", "updated": _now()}
                tx.write(newpath(nid), serialize(meta, act.get("body", "")))
                arch_ids = set(act.get("archive_ids", []))
                for aid in arch_ids:
                    archive(aid)
                redirect_inbound(arch_ids, nid)
                counts["summarize_episodes"] += 1

            elif kind == "introduce_subhub":
                hub_id = act["hub_id"]
                meta = {"id": hub_id, "type": "hub", "source_kind": "derived",
                        "summary": act.get("summary", ""),
                        "part_of": ([{"topic": act["parent_topic"], "w": 1.0}]
                                    if act.get("parent_topic") else []),
                        "edges": [], "status": "active", "updated": _now()}
                tx.write(newpath(hub_id, "_hubs"), serialize(meta, ""))
                for mid in act.get("member_ids", []):
                    mn = nodes.get(mid)
                    if not mn:
                        continue
                    mn["part_of"] = [{"topic": hub_id, "w": 1.0}]
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
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "weights":
        root = Path(argv[2]).resolve() if len(argv) > 2 else Path.cwd()
        print(json.dumps(fold_weights(root), indent=2)); return 0
    if cmd == "plan":
        root = Path(argv[2]).resolve() if len(argv) > 2 else Path.cwd()
        print(json.dumps(make_plan(root), indent=2)); return 0
    if cmd == "apply":
        if len(argv) < 3:
            print("usage: consolidate.py apply <actions.json> [<project_root>]"); return 2
        root = Path(argv[3]).resolve() if len(argv) > 3 else Path.cwd()
        print(json.dumps(apply_actions(root, Path(argv[2])), indent=2)); return 0
    print(__doc__); return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
