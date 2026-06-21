#!/usr/bin/env python3
"""
consolidate.py — close the AMG memory loop. Crash-safe & idempotent.

Three jobs, mirroring how memory is maintained:

  weights : fold edge weights from the OUTCOME signal — outcome-gated, discriminative
            Hebbian reinforcement + exposure-gated decay + pruning + part_of
            renormalization. Fully deterministic, no LLM. Reinforce an edge only when
            both endpoints were USED in an accepted session (work/usage.log — a signal
            from OUTSIDE the retrieval loop, so it does not self-confirm like blind
            co-activation, §8.1); fade an edge that was merely surfaced in a pack
            (work/coactivation.log) but never used. "What helps the task wires together;
            what is shown but unused fades."

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

As a by-product, weights/apply also refresh <amg>/digest.md — a tiny always-on block
of the most salient standing decisions and open questions that the entry point imports
every session (insurance against the loop's main failure: the graph holds the answer
but it was never retrieved). It can also be regenerated on its own with `digest`.

CLI:
  python consolidate.py weights [<project_root>] [--root <agent_dir>]
  python consolidate.py plan    [<project_root>] [--root <agent_dir>]
  python consolidate.py apply <actions.json> [<project_root>] [--root <agent_dir>]
  python consolidate.py digest  [<project_root>] [--root <agent_dir>]

The graph root is <agent_dir>/amg, resolved by graph_store.resolve_amg_root:
--root -> AMG_AGENT_DIR env -> config search upward from <project_root> ->
the engine's own location -> the default <project_root>/.claude.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

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

# Outcome buckets for work/usage.log records (Stage 14). The improved Hebbian rule is
# OUTCOME-GATED: it reinforces an edge only when both endpoints were USED in a session
# with an ACCEPTED outcome (its source was edited and the work landed). That signal comes
# from OUTSIDE the retrieval loop — what the human did with the node — so reinforcing by it
# does NOT self-confirm like blind co-activation (§8.1). A REVERTED outcome weakens instead.
# The pipeline emits only `completed` today; accept/merge and revert are forward-compatible
# (auto-detecting a revert needs git/test integration — a later refinement).
USAGE_ACCEPTED = {"completed", "accepted", "merged"}
USAGE_REVERTED = {"reverted"}

DEFAULTS = {
    "hebbian_rate": 0.10, "decay_rate": 0.02, "prune_below": 0.05,
    "part_of_renormalize": True, "default_edge_weight": 0.5,
    # Hebbian weight updates are OFF by default until a measured uplift (roadmap task 14).
    # The rule is OUTCOME-GATED + DISCRIMINATIVE — NOT the old blind co-activation rule,
    # which measurably HURT recall on a sparse graph (the "highways" effect, §8.1/§8.2):
    #   * reinforce an edge only when both endpoints were USED in an accepted session
    #     (work/usage.log, the non-circular signal), by the discriminative headroom
    #     hebbian_rate*(1-w) so an already-strong edge does not run to the ceiling;
    #   * an edge merely surfaced in a pack (co-activation) but NOT used FADES by decay_rate
    #     — this demotes the highways instead of strengthening them.
    # While off, `weights` only ACCUMULATES coact (which feeds salience) and leaves w alone.
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
    # Episodic node types: authored captures that must be ROUTED through consolidation
    # judgment (salience, summarize/merge, promote/retire). note/section + the authored
    # transient types open_question/plan — both are states awaiting resolution (a question
    # gets answered -> promote/retire; a plan gets done), so they must be revisited rather
    # than living forever as active (which would surface a long-answered question as open).
    # decision/adr are NOT episodic — they are protected commitments (compaction.protect_types).
    "episodic_types": ["section", "note", "open_question", "plan"],
    "stale_age_days": 30,
    # Eval gate: measure a compaction on a graph CLONE before touching the real graph;
    # apply only if recall holds. Robust to missing/dead cases (skip, never false-reject).
    # on_fail reject|warn|revert (revert == reject: we measure before commit, so there is
    # nothing to roll back). See config.yml and apply_actions for the mechanism.
    "eval_gate": {
        "enabled": True,
        "cases": "",          # resolved from the store root in load_config (portable, 1.32)
        "min_recall_delta": -0.02,
        "min_hop_recall_delta": -0.02,
        "on_fail": "reject",
    },
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

# Relations whose two endpoints form a CONTRADICTION pair for the arbitration pass
# (Stage 14). amg-synth emits them as judgment edges; arbitration compares the two
# endpoints (source rank / freshness / confidence / verification) and issues a verdict.
CONFLICT_RELS = {"contradicts", "supersedes"}

# Arbitration verdict actions (Stage 14): NON-destructive status changes (+ a linking
# edge) that resolve a contradiction. Unlike COMPACTION_ACTIONS they archive/delete
# nothing — a node keeps its history — so they need neither the compaction.enabled gate,
# the protected-type guard, nor the eval gate. The judgment is the consolidator's; the
# code only detects candidates (make_plan) and applies the verdict transactionally.
ARBITRATION_ACTIONS = {"supersede", "dispute", "reject", "keep_both_with_context", "ask_user"}

# Source-priority hierarchy (THEORY §15.1: current code > docs > ADR > session > legacy
# > model guess) as a numeric rank the PLAN exposes to the consolidator. It is a HINT for
# judgment, not the verdict: the model still weighs freshness / confidence / verification
# (passed alongside) and decides. A settled ruling (decision/adr) ranks as ADR regardless
# of its (authored) provenance kind.
_SOURCE_RANK = {"code": 6, "doc": 5, "data": 5, "user": 5, "chat": 3, "model_inference": 1}


# --------------------------------------------------------------------------- #
# Node IO
# --------------------------------------------------------------------------- #

def load_config(amg_root: Path) -> Dict[str, Any]:
    cfg: Dict[str, Any] = json.loads(json.dumps(DEFAULTS))   # deep copy
    cfg["working_language"] = "en"          # summaries' language for created nodes
    # Portable default for eval_gate.cases, derived from the store location (1.32): the
    # shipped template's value is rendered to the agent dir at install; this covers a
    # config that omits it, with no hard-coded `.claude`.
    cfg["eval_gate"]["cases"] = str(amg_root.parent / "skills" / "amg-retrieve" / "evals" / "cases.json")
    f = amg_root / "config.yml"
    if f.exists():
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for key in ("hebbian_rate", "decay_rate", "prune_below", "part_of_renormalize",
                    "default_edge_weight", "apply_hebbian"):
            if key in (raw.get("weights") or {}):
                cfg[key] = raw["weights"][key]
        if "compaction" in raw:
            cfg["compaction"].update(raw["compaction"] or {})
        if "eval_gate" in raw:
            cfg["eval_gate"].update(raw["eval_gate"] or {})
        # plan tunables previously frozen in code (1.17): now overridable from config
        for key in ("near_duplicate_sim", "episodic_types", "stale_age_days"):
            if key in raw:
                cfg[key] = raw[key]
        cfg["working_language"] = raw.get("working_language", "en")
    return cfg


def _parse(text: str) -> Optional[Tuple[Dict[str, Any], str]]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    return (yaml.safe_load(m.group(1)) or {}), m.group(2)


def serialize(meta: Dict[str, Any], body: str) -> str:
    clean = {k: v for k, v in meta.items() if not k.startswith("_")}
    fm = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body or ''}".rstrip() + "\n"


def load_nodes(store: gs.GraphStore) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
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
# weights: outcome-gated reinforcement + exposure-gated decay + prune + renormalize
# --------------------------------------------------------------------------- #

def _usage_pairs(amg_root: Path) -> Tuple[Set[FrozenSet[str]], Set[FrozenSet[str]], bool]:
    """Read work/usage.log into (reward_pairs, punish_pairs, present).

    A reward pair is any unordered pair of nodes CO-USED in an ACCEPTED session (their
    source was edited and the work landed); a punish pair is the same in a REVERTED
    session. This is the OUTCOME-GATED signal that drives the improved Hebbian rule — it
    comes from outside the retrieval loop (what the human did), so reinforcing by it does
    not self-confirm like the circular co-activation signal (§8.1 / THEORY §15.5). The
    journal is written by lifecycle.session-end (Stage 13). `present` is True whenever the
    log exists, so the caller can consume it after folding even if no record carried an
    actionable outcome."""
    path = amg_root / "work" / "usage.log"
    reward: Set[FrozenSet[str]] = set()
    punish: Set[FrozenSet[str]] = set()
    if not path.exists():
        return reward, punish, False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        outcome = str(rec.get("outcome", "")).lower()
        if outcome in USAGE_ACCEPTED:
            bucket = reward
        elif outcome in USAGE_REVERTED:
            bucket = punish
        else:
            continue
        used = sorted({u for u in rec.get("used", []) if isinstance(u, str)})
        for i in range(len(used)):
            for j in range(i + 1, len(used)):
                bucket.add(frozenset((used[i], used[j])))
    return reward, punish, True


def fold_weights(project_root: Path, amg_root: Optional[Path] = None) -> Dict[str, Any]:
    amg = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg)
    store.init()
    cfg = load_config(amg)
    eta, lam, prune = cfg["hebbian_rate"], cfg["decay_rate"], cfg["prune_below"]
    default_w = cfg["default_edge_weight"]
    apply_hebbian = bool(cfg.get("apply_hebbian", False))

    # Exposure signal (co-activation): which edges were SURFACED together in a pack. It
    # always feeds the coact counter (salience) and, when the rule is on, marks an edge as
    # "considered" so a surfaced-but-unused edge fades (demoting highways). It is NOT used
    # to reinforce — that was the circular blind rule that hurt recall (§8.1/§8.2).
    log_path = store.root / "work" / "coactivation.log"
    pair_counts: Dict[Tuple[str, ...], int] = defaultdict(int)
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

    # Outcome signal (usage): co-used pairs from accepted / reverted sessions. Read ONLY
    # when the rule is on, so the default-off path never touches usage.log — it keeps
    # accumulating as the substrate and selftest_usage's separation holds.
    reward_pairs: Set[FrozenSet[str]] = set()
    punish_pairs: Set[FrozenSet[str]] = set()
    usage_present = False
    if apply_hebbian:
        reward_pairs, punish_pairs, usage_present = _usage_pairs(store.root)

    # Touch w ONLY when the rule is on AND there is an OUTCOME signal to learn from.
    # Without usage a read-only period teaches nothing about usefulness, so weights stay
    # frozen (idempotent re-run; audit 1.9). The reinforcement is outcome-gated +
    # discriminative (headroom (1-w)), the decay is exposure-gated — together they invert
    # the blind rule's highway effect: productive edges strengthen with diminishing
    # returns, surfaced-but-unused edges fade (§8.1/§8.2).
    update_w = apply_hebbian and bool(reward_pairs or punish_pairs)

    with store.lock():
        store.recover()
        nodes = load_nodes(store)
        tx = store.transaction()
        changed = 0
        rewarded = punished = decayed = 0

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
                if co > 0:                                # accumulate exposure ALWAYS (salience)
                    e["coact"] = int(e.get("coact", 0)) + co
                    touched = True
                if update_w:
                    pairkey = frozenset((nid, e["to"]))
                    is_reward = pairkey in reward_pairs
                    is_punish = pairkey in punish_pairs
                    w = float(e.get("w", default_w))
                    if is_reward:                         # outcome reward: discriminative headroom
                        w += eta * (1.0 - w)
                        rewarded += 1
                    if is_punish:                         # negative outcome: weaken proportionally
                        w -= eta * w
                        punished += 1
                    if co > 0 and not is_reward and not is_punish:
                        w -= lam                          # surfaced but unused -> fade (anti-highway)
                        decayed += 1
                    w = max(0.0, min(1.0, w))
                    e["w"] = round(w, 4)
                    touched = True
                    if w < prune:
                        continue                          # faded edge dropped (pruned)
                kept.append(e)
            if kept != edges:
                node["edges"] = kept
                touched = True
            # renormalize part_of so memberships sum to <= 1 (always; an invariant)
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

        # rotate the consumed signals into the archive (auditable, then cleared)
        if log_path.exists() and pair_counts:
            arch = f"{cfg['compaction']['archive_dir']}/coactivation-{int(time.time())}.log"
            tx.write(arch, log_path.read_text(encoding="utf-8"))
            tx.delete("work/coactivation.log")
        usage_path = store.root / "work" / "usage.log"
        if apply_hebbian and usage_present and usage_path.exists():
            arch = f"{cfg['compaction']['archive_dir']}/usage-{int(time.time())}.log"
            tx.write(arch, usage_path.read_text(encoding="utf-8"))
            tx.delete("work/usage.log")

        txid = tx.commit()
        if txid:
            _refresh_index(store.root, tx)     # warm the read-index under the lock
        _log(store, f"weights folded: apply_hebbian={update_w}, "
                    f"{len(pair_counts)} co-activated pairs, "
                    f"{rewarded} rewarded / {punished} punished / {decayed} decayed edges, "
                    f"{changed} nodes updated", txid)

    write_digest(project_root, amg)         # refresh the always-on digest post-fold
    return {"coact_pairs": len(pair_counts),
            "reward_pairs": len(reward_pairs), "punish_pairs": len(punish_pairs),
            "rewarded_edges": rewarded, "punished_edges": punished, "decayed_edges": decayed,
            "nodes_updated": changed, "hebbian_applied": update_w}


# --------------------------------------------------------------------------- #
# plan: branch budgets, salience, duplicate & episodic candidates
# --------------------------------------------------------------------------- #

def _node_text(node: Dict[str, Any]) -> List[str]:
    txt = " ".join([str(node.get("summary", "")), node.get("_body", "")[:400]])
    return [w.lower() for w in WORD_RE.findall(txt)]


def _jaccard(a: Set[str], b: Set[str]) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _branch_members(nodes: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """For each hub node, the ids that belong to its branch — reached two ways:

      * upward: a node whose part_of transitively reaches the hub (explicit
        membership, incl. weighted multi-membership from synthesis);
      * downward: from the hub, following its containment-ish edges (HUB_DOWN_RELS)
        transitively to non-hub nodes (a hub documents a module, the module defines
        its functions). This makes branches computable when a leaf's primary
        membership is the directory string rather than the hub node (audit 1.20).

    The downward walk stops at any other hub, so branches don't bleed together."""
    parent_topics: Dict[str, List[Any]] = {
        nid: [p.get("topic") for p in (n.get("part_of") or []) if isinstance(p, dict)]
        for nid, n in nodes.items()}
    hubs = [nid for nid, n in nodes.items() if n.get("type") in ("hub", "overview")]
    hub_set = set(hubs)
    members: Dict[str, Set[str]] = {h: set() for h in hubs}

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


def salience(node: Dict[str, Any], degree: int, max_degree: int, cfg: Dict[str, Any],
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


def _degree_map(nodes: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, int], int]:
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


def _is_protected(node: Optional[Dict[str, Any]], degree: Dict[str, int], max_deg: int,
                  cmp_cfg: Dict[str, Any]) -> bool:
    """A node the compaction layer must not collapse/shorten/retire/archive without
    an explicit force: a protected type (decision/adr) or a node whose normalized
    degree centrality exceeds protect_min_centrality. Enforced in code, not only in
    the consolidator prompt (1.11)."""
    if node is None:
        return False
    protect = {str(t).lower() for t in cmp_cfg.get("protect_types", [])}
    if (node.get("type") or "").lower() in protect:
        return True
    deg = degree.get(str(node.get("id", "")), 0)
    centrality = deg / max_deg if max_deg else 0.0
    return centrality > float(cmp_cfg.get("protect_min_centrality", 0.7))


def _inbound_grounded(nodes: Dict[str, Dict[str, Any]]) -> Set[str]:
    """Ids that some other node points AT with a grounding relation
    (documents/implements/specifies). Feeds salience's provenance signal (2.8 p.6)."""
    grounded: Set[str] = set()
    for n in nodes.values():
        for e in (n.get("edges") or []):
            if isinstance(e, dict) and e.get("rel") in GROUND_RELS and e.get("to") in nodes:
                grounded.add(e["to"])
    return grounded


def _combine_part_of(memberships: List[Dict[str, Any]], renormalize: bool) -> List[Dict[str, Any]]:
    """Combine memberships by topic, keeping the strongest weight per topic, then
    renormalize to the simplex (sum <= 1) when asked. Used when merge folds two
    nodes' memberships and when introduce_subhub rewrites one topic (1.21)."""
    out: Dict[str, Dict[str, Any]] = {}
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


def _dedup_edges(edges: List[Dict[str, Any]], owner_id: str) -> List[Dict[str, Any]]:
    """Collapse edges by (rel, to): keep the max weight and SUM coact; drop a
    self-edge (to == owner). Applied to a neighbor after redirect_inbound so a node
    that pointed at both the survivor and a dropped node ends with one edge (1.22)."""
    out: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
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


def _source_rank(node: Dict[str, Any]) -> int:
    """Numeric source-priority rank for arbitration (higher = more authoritative), from
    THEORY §15.1. A decision/ADR ranks as a settled ruling; otherwise rank by provenance
    kind (code/doc/data/user/chat/model_inference), defaulting to 2."""
    typ = (node.get("type") or "").lower()
    if typ in ("adr", "decision"):
        return 4
    kind = ((node.get("provenance") or {}).get("kind") or "").lower()
    return _SOURCE_RANK.get(kind, 2)


def _node_arb_info(node: Dict[str, Any]) -> Dict[str, Any]:
    """The comparison inputs the consolidator weighs when arbitrating a contradiction:
    source rank, confidence, freshness (updated), verification status, provenance kind,
    and current lifecycle status. The code lays these out; the model judges."""
    return {"type": node.get("type"), "status": node.get("status") or "active",
            "source_rank": _source_rank(node), "confidence": node.get("confidence"),
            "updated": node.get("updated"),
            "verification": (node.get("verification") or {}).get("status"),
            "provenance_kind": (node.get("provenance") or {}).get("kind")}


def _contradiction_candidates(nodes: Dict[str, Dict[str, Any]]
                              ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Detect contradictions for the arbitration pass (Stage 14):

      * pairs — node-vs-node conflicts: two nodes linked by a contradicts/supersedes edge
        (CONFLICT_RELS), each side with its comparison inputs (_node_arb_info);
      * source_contradicted — single nodes whose summary lost to their LIVE source
        (verification.status == 'contradicted'; the source won, §15.1) — candidates to
        supersede/reject.

    Detection is deterministic; the verdict is the subagent's (mechanics determined,
    meaning by the model)."""
    pairs: List[Dict[str, Any]] = []
    seen: Set[FrozenSet[str]] = set()
    for nid, n in nodes.items():
        for e in (n.get("edges") or []):
            if not (isinstance(e, dict) and e.get("rel") in CONFLICT_RELS):
                continue
            to = e.get("to")
            if to not in nodes or to == nid or frozenset((nid, to)) in seen:
                continue
            seen.add(frozenset((nid, to)))
            pairs.append({"a": nid, "b": to, "rel": e["rel"],
                          "a_info": _node_arb_info(n), "b_info": _node_arb_info(nodes[to])})
    source_contradicted = [{"id": nid, **_node_arb_info(n)} for nid, n in nodes.items()
                           if (n.get("verification") or {}).get("status") == "contradicted"]
    return pairs, source_contradicted


def make_plan(project_root: Path, amg_root: Optional[Path] = None) -> Dict[str, Any]:
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

    # near-duplicate candidates (lexical Jaccard over summaries). Restricted to the
    # EPISODIC, non-source-derived nodes that merge_near_duplicates can actually merge
    # (§1.27): the old all-pairs scan was O(n^2) over the WHOLE graph (~5e7 set
    # comparisons at 10^4 nodes) and could even propose merging two mirror nodes —
    # futile, since reconcile just recreates them. Same filter as episodic_candidates
    # below, so the consolidator only ever sees mergeable pairs. (If a real graph ever
    # shows a large episodic k, add a MinHash/LSH prefilter — measured, not preemptive.)
    dup_ids = [nid for nid, n in nodes.items()
               if n.get("type") in cfg["episodic_types"]
               and n.get("source_kind") not in ("derived_from_file",)]
    toks = {nid: set(_node_text(nodes[nid])) for nid in dup_ids}
    dups = []
    for i in range(len(dup_ids)):
        for j in range(i + 1, len(dup_ids)):
            sim = _jaccard(toks[dup_ids[i]], toks[dup_ids[j]])
            if sim >= cfg["near_duplicate_sim"]:
                dups.append({"a": dup_ids[i], "b": dup_ids[j], "sim": round(sim, 3)})

    # episodic candidates + salience
    grounded_in = _inbound_grounded(nodes)
    episodic: List[Dict[str, Any]] = []
    for nid, n in nodes.items():
        if (n.get("type") in cfg["episodic_types"]
                and n.get("source_kind") not in ("derived_from_file",)):
            episodic.append({"id": nid,
                             "salience": salience(n, degree[nid], max_deg, cfg,
                                                  nid in grounded_in),
                             "protected": (n.get("type") or "").lower() in cmp_cfg["protect_types"]})
    episodic.sort(key=lambda x: float(x["salience"]))

    # contradiction candidates for the arbitration pass (detect only; the consolidator
    # compares the laid-out inputs and issues the verdict — Stage 14)
    contradictions, source_contradicted = _contradiction_candidates(nodes)

    plan = {"generated": _now(), "n_nodes": len(nodes),
            "over_budget_branches": over_budget,
            "near_duplicates": dups,
            "episodic_candidates": episodic[:50],
            "contradictions": contradictions,
            "source_contradicted": source_contradicted}
    gs.atomic_write_text(store.root / "work" / "consolidation-plan.json",
                         json.dumps(plan, ensure_ascii=False, indent=2))
    return {"over_budget": len(over_budget), "duplicates": len(dups),
            "episodic": len(episodic), "contradictions": len(contradictions),
            "source_contradicted": len(source_contradicted)}


# --------------------------------------------------------------------------- #
# digest: a tiny always-on block of standing decisions & open questions
# --------------------------------------------------------------------------- #

# The always-on digest is insurance against the memory loop's main failure mode: the
# graph holds the answer but the model never retrieved it. Consolidation writes the
# most salient standing decisions and open questions to <amg>/digest.md, which the
# entry point imports every session, so they ride along even with no retrieval.
DIGEST_DECISION_TYPES = {"decision", "adr"}
DIGEST_QUESTION_TYPES = {"open_question"}
DIGEST_STATUSES = {"active", "captured"}     # surfaced; superseded/retired excluded
DIGEST_MAX_DECISIONS = 6
DIGEST_MAX_QUESTIONS = 4


def _digest_rows(nodes: Dict[str, Dict[str, Any]], types: Set[str], cfg: Dict[str, Any],
                 degree: Dict[str, int], max_deg: int, grounded_in: Set[str],
                 limit: int) -> List[Dict[str, Any]]:
    """The `limit` most salient nodes of the given types and a surfaced status,
    ranked by the same salience rubric consolidation uses elsewhere (so the digest
    agrees with what consolidation considers valuable)."""
    cand = [n for n in nodes.values()
            if (n.get("type") or "").lower() in types
            and (n.get("status") or "active") in DIGEST_STATUSES]
    cand.sort(key=lambda n: salience(n, degree.get(n["id"], 0), max_deg, cfg,
                                     n["id"] in grounded_in), reverse=True)
    return cand[:limit]


def _render_digest(decisions: List[Dict[str, Any]], questions: List[Dict[str, Any]]) -> str:
    head = ["<!-- AMG memory digest — auto-generated by consolidation; do not edit by hand. -->",
            "## AMG memory digest — standing decisions & open questions", ""]
    if not decisions and not questions:
        return "\n".join(head + ["_No active decisions or open questions captured yet._"]) + "\n"

    def row(n: Dict[str, Any]) -> str:
        summ = " ".join((n.get("summary") or "").split()) or "(no summary)"
        return f"- **{n.get('type')}** — {summ}  ·  `{n.get('id')}`"

    body = [row(n) for n in decisions] + [row(n) for n in questions]
    tail = ["", "_Retrieve for the full context around any of these (amg-retrieve skill)._"]
    return "\n".join(head + body + tail) + "\n"


def write_digest(project_root: Path, amg_root: Optional[Path] = None) -> Dict[str, Any]:
    """Regenerate <amg_root>/digest.md from the graph: the top standing decisions and
    open questions by salience, as a small markdown block the entry point imports every
    session (roadmap Stage 8). Deterministic and read-only over the graph — a single
    atomic write of one file outside nodes/, so it needs no store lock."""
    amg = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg)
    store.init()
    cfg = load_config(amg)
    nodes = load_nodes(store)
    degree, max_deg = _degree_map(nodes)
    grounded_in = _inbound_grounded(nodes)
    decisions = _digest_rows(nodes, DIGEST_DECISION_TYPES, cfg, degree, max_deg,
                             grounded_in, DIGEST_MAX_DECISIONS)
    questions = _digest_rows(nodes, DIGEST_QUESTION_TYPES, cfg, degree, max_deg,
                             grounded_in, DIGEST_MAX_QUESTIONS)
    gs.atomic_write_text(amg / "digest.md", _render_digest(decisions, questions))
    return {"decisions": len(decisions), "open_questions": len(questions)}


# --------------------------------------------------------------------------- #
# eval gate: compaction must not silently hurt retrieval
# --------------------------------------------------------------------------- #

def _load_eval_modules() -> Tuple[Any, Any]:
    """Soft-import the eval harness (eval_retrieval -> retrieve) from the sibling
    amg-retrieve skill. Returns (E, R) or (None, None) when unavailable, so weights/
    plan and a no-gate apply never hard-depend on it (the gate then skips)."""
    try:
        retrieve_dir = Path(__file__).resolve().parents[2] / "amg-retrieve" / "scripts"
        if str(retrieve_dir) not in sys.path:
            sys.path.insert(0, str(retrieve_dir))
        import eval_retrieval as E
        import retrieve as R
        return E, R
    except Exception:
        return None, None


def _clone_for_eval(amg: Path) -> Path:
    """Copy the graph (nodes/ + config.yml + cases) into a throwaway store, so the
    proposed actions can be applied and MEASURED there without touching the real graph
    — the gate measures on the would-be result, then commits to the real graph only if
    recall holds (so 'reject' needs no rollback). Returns the clone's amg root."""
    tmp = Path(tempfile.mkdtemp(prefix="amg-gate-"))
    clone = tmp / "amg"
    (clone / "nodes").mkdir(parents=True)
    if (amg / "nodes").exists():
        shutil.copytree(amg / "nodes", clone / "nodes", dirs_exist_ok=True)
    for fn in ("config.yml", "cases.json"):
        if (amg / fn).exists():
            shutil.copy2(amg / fn, clone / fn)
    return clone


def _gate_cases(amg: Path, cases_path: Any, project_root: Path, R: Any) -> List[Dict[str, Any]]:
    """Load labeled cases and keep only those whose gold_ids resolve in THIS graph.
    Returns [] for missing / empty / dead cases — the gate then SKIPS (never falsely
    rejects), so the shipped unresolved template leaves a fresh install safe."""
    if not cases_path:
        return []
    p = Path(cases_path)
    if not p.is_absolute():                       # roadmap default is project-relative
        p = project_root / cases_path
    if not p.exists():
        return []
    try:
        cases = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    graph_ids = set(R.load_nodes(amg).keys())
    return [c for c in cases
            if isinstance(c, dict) and c.get("query") and c.get("gold_ids")
            and (set(c["gold_ids"]) & graph_ids)]


def _action_ids(act: Dict[str, Any]) -> Set[str]:
    """Every node id an action references — for attributing a lost gold node to it."""
    ids = {act[k] for k in ("id", "keep_id", "new_id", "hub_id") if act.get(k)}
    for k in ("drop_ids", "archive_ids", "member_ids"):
        ids.update(act.get(k) or [])
    return ids


def _gate_regressions(baseline: Dict[str, Any], after: Dict[str, Any], actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per case: gold that was in the pack before and is gone after, with the actions
    that referenced each lost id (best-effort attribution for the report)."""
    after_by_id = {r["id"]: r for r in after["per_case"]}
    out = []
    for rb in baseline["per_case"]:
        ra = after_by_id.get(rb["id"])
        if not ra:
            continue
        lost = sorted(set(rb.get("pack_gold", [])) - set(ra.get("pack_gold", [])))
        if not lost:
            continue
        attribution = [{"gold_id": nid,
                        "actions": sorted({a.get("action") for a in actions
                                           if isinstance(a, dict) and nid in _action_ids(a)})}
                       for nid in lost]
        out.append({"case": rb["id"], "lost_gold": lost, "attribution": attribution})
    return out


def _gate_decide(baseline: Dict[str, Any], after: Dict[str, Any], gate_cfg: Dict[str, Any],
                 actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare before/after aggregates and decide. recall is PACK recall (compaction
    changes pack composition, not just top-K ranking); hop_recall isolates the edge
    contribution. on_fail reject|revert -> 'rejected' (revert == reject: we measured
    before commit); warn -> apply anyway with the regression recorded."""
    b, a = baseline["aggregate"], after["aggregate"]
    b_rec, a_rec = b.get("amg_pack_recall"), a.get("amg_pack_recall")
    rec_delta = round((a_rec or 0.0) - (b_rec or 0.0), 4)
    b_hop = (b.get("amg") or {}).get("hop_recall")
    a_hop = (a.get("amg") or {}).get("hop_recall")
    hop_delta = (round(a_hop - b_hop, 4)
                 if a_hop is not None and b_hop is not None else None)
    min_rec = gate_cfg.get("min_recall_delta", -0.02)
    min_hop = gate_cfg.get("min_hop_recall_delta", -0.02)
    failed = rec_delta < min_rec or (hop_delta is not None and hop_delta < min_hop)
    on_fail = str(gate_cfg.get("on_fail", "reject")).lower()
    status = "ok" if not failed else ("warn" if on_fail == "warn" else "rejected")
    return {"status": status, "on_fail": on_fail, "cases": b.get("cases", 0),
            "pack_recall_before": b_rec, "pack_recall_after": a_rec,
            "recall_delta": rec_delta, "hop_recall_delta": hop_delta,
            "min_recall_delta": min_rec, "min_hop_recall_delta": min_hop,
            "regressions": _gate_regressions(baseline, after, actions)}


def _eval_gate(project_root: Path, amg: Path, actions_path: Path,
               actions: List[Dict[str, Any]], cfg: Dict[str, Any], enabled: bool) -> Optional[Dict[str, Any]]:
    """Run the gate, or return None when it does not apply (disabled, or no compression
    action will actually run). On a fail it returns a decision dict; callers apply or
    reject accordingly. Measurement uses a graph clone and never touches the real graph
    or the co-activation journal (evaluate_case runs retrieve with writes off)."""
    gate_cfg = cfg.get("eval_gate") or {}
    if not gate_cfg.get("enabled", True):
        return None
    will_compress = any(a.get("action") in COMPACTION_ACTIONS and (enabled or a.get("force"))
                        for a in actions if isinstance(a, dict))
    if not will_compress:
        return None                               # nothing destructive -> no gate needed
    E, R = _load_eval_modules()
    if E is None:
        return {"status": "skipped",
                "reason": "eval harness unavailable (amg-retrieve scripts not importable)"}
    cases = _gate_cases(amg, gate_cfg.get("cases"), project_root, R)
    if not cases:
        return {"status": "skipped",
                "reason": "no resolvable labeled cases — gate disarmed; point "
                          "eval_gate.cases at your own file (ids from inspect_graph.py)"}
    eval_cfg = R.load_config(amg)
    baseline = E.run(amg, cases, eval_cfg)
    clone = _clone_for_eval(amg)
    try:
        apply_actions(project_root, actions_path, clone, _run_gate=False)
        after = E.run(clone, cases, eval_cfg)
    finally:
        shutil.rmtree(clone.parent, ignore_errors=True)
    return _gate_decide(baseline, after, gate_cfg, actions)


def _write_gate_report(store: gs.GraphStore, report: Dict[str, Any]) -> None:
    try:
        gs.atomic_write_text(store.root / "work" / "eval-gate-report.json",
                             json.dumps(report, ensure_ascii=False, indent=2))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# apply: enact the consolidator subagent's actions (transactional + archived)
# --------------------------------------------------------------------------- #

def apply_actions(project_root: Path, actions_path: Path,
                  amg_root: Optional[Path] = None, _run_gate: bool = True) -> Dict[str, Any]:
    amg = Path(amg_root) if amg_root else gs.resolve_amg_root(start=project_root)
    store = gs.GraphStore(amg)
    cfg = load_config(amg)
    archive_dir = cfg["compaction"]["archive_dir"]
    actions = json.loads(Path(actions_path).read_text(encoding="utf-8"))
    counts: Dict[str, int] = defaultdict(int)
    cmp_cfg = cfg["compaction"]
    enabled = cmp_cfg.get("enabled", True)

    with store.lock():
        store.recover()
        nodes = load_nodes(store)

        # Eval gate: measure the proposed compaction on a graph clone and reject (or
        # warn) on a recall drop BEFORE building the real transaction (_run_gate is
        # off for the clone's own apply, so there is no recursion). Skips robustly.
        gate = _eval_gate(project_root, amg, actions_path, actions, cfg, enabled) \
            if _run_gate else None
        if gate is not None:
            _write_gate_report(store, gate)
            if gate["status"] == "rejected":
                _log(store, "eval-gate REJECTED compaction "
                            f"(Δrecall={gate['recall_delta']}, Δhop={gate['hop_recall_delta']})",
                     None)
                return {"gate": "rejected", "recall_delta": gate["recall_delta"],
                        "hop_recall_delta": gate["hop_recall_delta"]}

        tx = store.transaction()
        renorm = bool(cfg.get("part_of_renormalize", True))
        degree, max_deg = _degree_map(nodes)

        def archive(nid: str) -> None:
            n = nodes.get(nid)
            if n:
                tx.write(f"{archive_dir}/{Path(n['_path']).name}", serialize(n, n["_body"]))
                tx.delete(n["_path"])

        def redirect_inbound(old_ids: Set[str], new_id: str) -> None:
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

        def newpath(nid: str, kind: str = "notes") -> str:
            slug = re.sub(r"[^\w.-]+", "_", nid.split(":", 1)[-1]).strip("_")[:48] or "node"
            h = gs.sha256_text(nid)[:8]
            return f"nodes/{kind}/{slug}-{h}.md"

        arb_audit: List[str] = []           # arbitration verdict lines -> arbitration.md

        def set_status(nid: str, status: str) -> None:
            """Arbitration verdict: set a node's lifecycle status (superseded/disputed/
            rejected) and bump updated. Non-destructive — the node and its history stay."""
            n = nodes.get(nid)
            if n:
                n["status"] = status
                n["updated"] = _now()
                tx.write(n["_path"], serialize(n, n["_body"]))

        def ensure_edge(src: str, rel: str, dst: str) -> None:
            """Add a (rel, dst) edge to src if absent (origin consolidation), so the
            contradiction/supersession is explicit and retrieval surfaces both sides."""
            n = nodes.get(src)
            if not n or src == dst or dst not in nodes:
                return
            edges = n.get("edges") or []
            if any(isinstance(e, dict) and e.get("rel") == rel and e.get("to") == dst
                   for e in edges):
                return
            edges.append({"rel": rel, "to": dst, "w": cfg["default_edge_weight"],
                          "coact": 0, "origin": "consolidation"})
            n["edges"] = edges
            tx.write(n["_path"], serialize(n, n["_body"]))

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
                folded: Dict[Tuple[Any, Any], Dict[str, Any]] = {}

                def _fold(e: Dict[str, Any]) -> None:
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

            # --- arbitration verdicts (Stage 14): non-destructive status changes + a
            # linking edge; no compaction gate / protection / eval gate applies ---
            elif kind == "supersede":
                winner, loser = act.get("winner_id"), act.get("loser_id")
                if not (winner and loser) or winner == loser:
                    continue
                if not (nodes.get(winner) and nodes.get(loser)):
                    continue
                set_status(loser, "superseded")
                ensure_edge(winner, "supersedes", loser)   # make the supersession explicit
                arb_audit.append(_arb_line("supersede", f"{winner} <- {loser}", act))
                counts["supersede"] += 1

            elif kind in ("dispute", "ask_user"):
                ids = [i for i in (act.get("ids") or []) if nodes.get(i)]
                if len(ids) < 2:
                    continue
                for i in ids:                            # both/all sides marked disputed
                    set_status(i, "disputed")
                for i in ids[1:]:                        # link them so retrieval surfaces it
                    ensure_edge(ids[0], "contradicts", i)
                arb_audit.append(_arb_line(kind, " <> ".join(ids), act,
                                           extra="NEEDS USER" if kind == "ask_user" else ""))
                counts[kind] += 1

            elif kind == "keep_both_with_context":
                ids = [i for i in (act.get("ids") or []) if nodes.get(i)]
                if len(ids) < 2:
                    continue
                for i in ids[1:]:                        # link both sides but leave active
                    ensure_edge(ids[0], "contradicts", i)
                arb_audit.append(_arb_line("keep_both_with_context", " <> ".join(ids), act))
                counts["keep_both_with_context"] += 1

            elif kind == "reject":
                rid = act.get("id")
                if not (rid and nodes.get(rid)):
                    continue
                set_status(rid, "rejected")
                arb_audit.append(_arb_line("reject", str(rid), act))
                counts["reject"] += 1

        # Arbitration audit trail: append the verdicts to arbitration.md within THIS
        # transaction (atomic with the status/edge changes), so the basis of every memory
        # verdict is durably visible — conflicts are never resolved silently (Stage 14 DoD).
        if arb_audit:
            arb_rel = "arbitration.md"
            prior = (store.abspath(arb_rel).read_text(encoding="utf-8")
                     if store.abspath(arb_rel).exists() else "")
            head = "" if prior else "# AMG arbitration log — contradiction verdicts (auto-generated)\n\n"
            tx.write(arb_rel, prior + head + "\n".join(arb_audit) + "\n")

        txid = tx.commit()
        if txid:
            _refresh_index(store.root, tx)     # warm the read-index under the lock
        msg = f"consolidation applied: {dict(counts)}"
        if gate is not None and gate["status"] == "warn":
            msg += (f" | eval-gate WARNING applied despite recall drop "
                    f"(Δrecall={gate['recall_delta']}, Δhop={gate['hop_recall_delta']})")
        _log(store, msg, txid)

    if _run_gate:                           # the real apply refreshes the digest
        write_digest(project_root, amg)     # (the clone's gate apply skips it)
    result = dict(counts)
    if gate is not None:
        result["gate"] = gate["status"]
    return result


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _arb_line(action: str, subject: str, act: Dict[str, Any], extra: str = "") -> str:
    """One human-readable arbitration audit line for arbitration.md (Stage 14): what was
    decided, on which nodes, the reason, and the sources compared — so the user can see
    the basis of a memory verdict (DoD: conflicts are not resolved silently)."""
    reason = " ".join(str(act.get("reason", "")).split()) or "(no reason given)"
    sources = act.get("sources")
    src = f"  | sources: {sources}" if sources else ""
    tag = f"  [{extra}]" if extra else ""
    return f"## [{_now()}] {action}{tag}  {subject}  | reason: {reason}{src}"


def _log(store: gs.GraphStore, msg: str, txid: Optional[str]) -> None:
    """Append a consolidation audit line through the store's transactional action
    log (de-duped by txid, bounded by rotation). Best-effort, under the lock the
    caller already holds (1.15: log is now part of a committed transaction)."""
    store.append_log("consolidate", msg, txid)


def _refresh_index(amg: Path, tx: gs.Transaction) -> None:
    """Best-effort: fold this committed write into the disposable SQLite read-index
    (index_store, in the amg-retrieve skill) under the caller's lock, so the next
    retrieve reads it instead of re-scanning nodes/*.md. Mirrors
    reconcile._refresh_index; swallows everything — the index is a cache that
    retrieve rebuilds on any signature mismatch."""
    try:
        idx_dir = str(Path(__file__).resolve().parents[2] / "amg-retrieve" / "scripts")
        if idx_dir not in sys.path:
            sys.path.insert(0, idx_dir)
        import index_store
        written, deleted = tx.node_paths()
        if written or deleted:
            index_store.refresh_after_commit(amg, written, deleted)
    except Exception:
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
    if cmd == "digest":
        root = Path(args[1]).resolve() if len(args) > 1 else Path.cwd()
        print(json.dumps(write_digest(root, gs.resolve_amg_root(cli_root, root)), indent=2)); return 0
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
