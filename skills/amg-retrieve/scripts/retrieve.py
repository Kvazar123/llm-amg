#!/usr/bin/env python3
"""
retrieve.py — associative context retrieval for AMG.

Given a task/query, assemble a budgeted context pack from the graph so the model
sees the strategic surround plus the operational detail — without loading the whole
project. The method is spreading activation, formalized as *query-biased
Personalized PageRank* (Haveliwala 2002; the same construction HippoRAG 2024 uses
over a knowledge graph):

    pi = (1 - d) * p  +  d * M @ pi

  * M  is the column-stochastic transition built from STRUCTURAL edge conductance
       c(u,v) = w_edge * beta(rel), symmetrized so association flows both ways.
       M is query-INDEPENDENT, which is what preserves multi-hop reach: a node that
       shares no words with the query is still reached through its edges.
  * p  is the teleport / personalization vector. It encodes query relevance via a
       lexical BM25 score over each node's text (id + summary + body excerpt). Seeds
       (best lexical matches) dominate; relevance biases the stationary distribution
       toward the right region of the graph WITHOUT gating edges.

Convergence is guaranteed: with 0 < d < 1 the iteration is a contraction (power
method on a stochastic matrix; Perron-Frobenius).

The pack is assembled greedily by activation under per-tier token budgets
(strategic / tactical / operational / periphery) — a budgeted maximum-coverage
step whose greedy solution is within (1 - 1/e) of optimal because relevance is
submodular.

This script is READ-ONLY with respect to the graph: it never mutates nodes or
edges. Its only writes are its own output pack and an append-only co-activation
log that the consolidation pass folds in later (Hebbian "fire together, wire
together"); both are optional and lock-free.

CLI:
    python retrieve.py "how do we handle a declined card charge"
    python retrieve.py "<query>" --store /path/to/.claude/amg --top 12 --no-pack
    python retrieve.py "<query>" --explain      # show the edges that drove the top nodes
    python retrieve.py "<query>" --intent conflict   # history|conflict — surface retired /
        # contradicted nodes for a history/audit or "show contradictions" query. The
        # RETRIEVER SUBAGENT sets this from the query in ANY language (intent is the model's
        # to recognize; the code only applies it), so no language-specific keywords live here.
    python retrieve.py "<query>" --compact      # the pointer profile: modest built-in
        # budgets, operational bodies replaced by path:line pointer lines — for a
        # TARGETED lookup; the full profile is for entering an unfamiliar area. The
        # CALLER chooses (no activation statistic can tell the two query kinds apart).
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
except ImportError:                       # pragma: no cover
    sys.stderr.write("retrieve.py needs PyYAML: pip install pyyaml\n")
    raise

# Windows consoles default to cp1252; force UTF-8 stdout so non-ASCII content
# (Cyrillic summaries, paths) prints without crashing.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
# Unicode word characters (\w is Unicode by default in Python 3): this MUST match
# non-Latin scripts. An ASCII-only [A-Za-z0-9_]+ silently drops Cyrillic/CJK/etc.,
# so a Russian-language graph would be invisible to BM25 and every non-Latin query
# would seed nothing.
WORD_RE = re.compile(r"\w+", re.UNICODE)

DEFAULTS: Dict[str, Any] = {
    "damping": 0.85,
    "max_hops": 30,                 # power-iteration cap (more iters => wider reach)
    "convergence_tol": 1e-6,
    # Share of the TOP activation below which a node is dropped from the pack.
    # Activations are rescaled to max = 1 before assembly (see retrieve()), so the
    # cutoff is scale-free: an absolute cutoff on raw PPR mass (which sums to 1
    # over ALL nodes) would empty the pack on a large graph, where even the top
    # node's absolute activation falls below any fixed constant.
    "activation_threshold": 0.02,
    "seed_floor": 0.0,              # teleport mass given to every node (0 = pure relevance)
    "token_budget": {"strategic": 1200, "tactical": 2500,
                     "operational": 6000, "periphery_links": 40},
    "relation_priors": {
        "documents": 0.9, "specifies": 0.9, "implements": 0.9,
        "calls": 0.8, "depends_on": 0.8, "inherits": 0.8,
        "defines": 0.7, "part_of": 0.7,
        "imports": 0.6, "refines": 0.6, "exemplifies": 0.6, "relates_to": 0.5,
        "follows": 0.4, "supersedes": 0.3, "contradicts": 0.3,
    },
    "relation_prior_default": 0.5,
    # Per-status activation prior, applied AFTER PPR (re-ranking by node validity,
    # NOT a teleport gate). stale is NOT penalized — a just-changed node is often the
    # hottest; it is flagged in the pack instead. superseded is pushed down so a retired
    # claim never competes as an active fact; disputed (an open contradiction under
    # arbitration) sits in between and is surfaced as a conflict; rejected (a claim
    # arbitration found false) is pushed down hardest.
    "status_prior": {"active": 1.0, "stale": 1.0, "superseded": 0.2,
                     "disputed": 0.5, "rejected": 0.1},
    # Optional semantic seed enrichment. enabled: auto|on|off;
    # blend: 0=pure BM25 .. 1=pure semantic. Falls back to BM25 if no backend.
    "embeddings": {"enabled": "auto", "backend": "auto", "model": "", "blend": 0.5},
}

# Trust-layer defaults (a TOP-LEVEL config block, surfaced in load_config —
# default_confidence is read at ingest by reconcile, the rest govern pack marking here).
_VERIFICATION_DEFAULTS = {"enabled": True, "verify_code_claims": True,
                          "warn_on_unverified": True, "min_confidence_warn": 0.5}

# Which node types land in which abstraction tier of the pack.
TIER_OF_TYPE = {
    "hub": "strategic", "overview": "strategic",
    "decision": "strategic", "adr": "strategic",   # authored rulings: surface early
    # pattern nodes: synthesized, project-local generalizations of experience
    # (architectural pattern / recurring fix / anti-pattern / migration recipe). Strategic:
    # surface the reusable pattern before its instances, like a hub.
    "architectural_pattern": "strategic", "recurring_fix": "strategic",
    "anti_pattern": "strategic", "migration_recipe": "strategic",
    "module": "tactical", "class": "tactical", "package": "tactical",
    "function": "operational", "section": "operational",
    "file": "operational", "method": "operational",
}
CODE_TYPES = {"module", "class", "function", "method", "file"}
# Pattern nodes: synthesized, project-local generalizations of experience.
# Instances link to a pattern via `exemplifies`; the eval guards false analogies.
PATTERN_TYPES = {"architectural_pattern", "recurring_fix", "anti_pattern", "migration_recipe"}
# Authored rulings carry their payload in the body (the rationale), so render it
# inline whatever tier they land in — unlike a hub, whose body is a long overview.
DOC_BODY_TYPES = {"decision", "adr"}
# Pack trust flag text. The stale flag predates the layer and is
# unconditional; the verification/confidence flags are gated by the verification config.
# A flag NEVER downranks a node — it tells the model to confirm the claim against source
# (verify_claims.py) before relying on it (a just-changed node is often the most relevant).
_STALE_TEXT = "stale: summary may lag — open the source to verify"


def _trust_marks(node: Dict[str, Any], vcfg: Dict[str, Any]) -> str:
    """Compose the pack's trust annotation for a node from its lifecycle status and the
    trust-layer fields. Returns a `  ⟨…⟩` suffix or "". The stale flag is
    unconditional (backward-compatible); the rest fire only when verification.enabled, so
    turning the layer off restores the prior behavior exactly."""
    marks: List[str] = []
    status = node.get("status")
    if status == "stale":
        marks.append(_STALE_TEXT)
    elif status == "disputed":            # an unresolved contradiction (arbitration)
        marks.append("disputed: an unresolved contradiction — check the conflicting claim")
    elif status == "rejected":            # arbitration found this claim false
        marks.append("rejected: arbitration found this claim false")
    if vcfg.get("enabled", True):
        vstatus = (node.get("verification") or {}).get("status")
        is_code = node.get("type") in CODE_TYPES
        if vstatus == "contradicted":
            marks.append("contradicted: source check failed — re-verify before relying")
        elif (is_code and vstatus in (None, "unverified")
              and vcfg.get("verify_code_claims", True) and vcfg.get("warn_on_unverified", True)):
            marks.append("unverified: confirm this code claim against source")
        conf = node.get("confidence")
        try:
            if conf is not None and float(conf) < float(vcfg.get("min_confidence_warn", 0.5)):
                marks.append(f"low confidence {float(conf):.2f}")
        except (TypeError, ValueError):
            pass
    return "  ⟨" + "; ".join(marks) + "⟩" if marks else ""


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def _default_store() -> Path:
    """Resolve the graph store when --store is not given. Mirrors graph_store.
    resolve_amg_root (kept dependency-free here): walk upward from cwd, probing the
    agent-dir presets (.claude / .agents) FIRST — so a global engine finds the
    project's LOCAL graph under any preset environment (1.32) — then a bare amg/,
    accepted only when it is an INITIALIZED store (nodes/ + journal/). A candidate
    carrying the engine signature (skills/, agents/ or install.py inside) is the AMG
    source checkout, never a store; the HOME level is skipped —
    ~/<agent_dir>/amg holds the machine-wide defaults config, not a project store.
    Then the engine's own location (initialized stores only), then the
    .claude default. The retriever subagent passes --store explicitly; this only
    backs a bare manual run."""
    def _checkout(c: Path) -> bool:
        return any((c / m).exists() for m in ("skills", "agents", "install.py"))

    def _initialized(c: Path) -> bool:
        return (c / "nodes").is_dir() and (c / "journal").is_dir()

    home = Path.home().resolve()
    for d in (Path.cwd().resolve(), *Path.cwd().resolve().parents):
        if d == home:               # the home agent dir is the config-defaults layer
            continue
        for adir in (".claude", ".agents"):
            cand = d / adir / "amg"
            if (cand / "config.yml").exists() and not _checkout(cand):
                return cand
        cand = d / "amg"
        if (cand / "config.yml").exists() and not _checkout(cand) and _initialized(cand):
            return cand
    here = Path(__file__).resolve().parents[3] / "amg"   # engine location (dev / local)
    if _initialized(here) and not _checkout(here):
        return here
    return Path.cwd() / ".claude" / "amg"


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """Per-key overlay: nested dicts merge key-by-key, scalars/lists replace whole.

    Config must never silently drop a built-in default. An incomplete
    `relation_priors` / `token_budget` / `status_prior` in config.yml overlays the
    defaults instead of replacing the whole block — otherwise a prior the user did
    not restate would fall back to relation_prior_default and quietly mis-conduct.
    """
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _global_defaults_raw(local_raw: Dict[str, Any]) -> Dict[str, Any]:
    """The machine-wide defaults config (~/<agent_dir>/amg/config.yml, written by a
    global install) as a raw dict, or {}. Read ONLY when the
    local config carries the installer-written `agent_dir` key: it names the
    environment's home layer and marks the config as installer-made, so a minimal
    hand-made config (e.g. a test fixture) stays hermetic unless it opts in by adding
    the key. Mirrors extract_structure._global_defaults_raw (kept dependency-free)."""
    adir = str(local_raw.get("agent_dir") or "").strip()
    if not adir:
        return {}
    g = Path.home() / adir / "amg" / "config.yml"
    try:
        if not g.exists():
            return {}
        raw = yaml.safe_load(g.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def load_config(store_root: Path) -> Dict[str, Any]:
    f = store_root / "config.yml"
    raw: Dict[str, Any] = {}
    if f.exists():
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    # Config layering: global machine-wide defaults under the local config,
    # local overrides per key.
    raw = _deep_merge(_global_defaults_raw(raw), raw)
    cfg = _deep_merge(DEFAULTS, (raw.get("retrieval") or {}))
    # Surface top-level working_language into the retrieval cfg so embedding backend
    # selection can default to a multilingual model for non-English projects.
    cfg["working_language"] = raw.get("working_language", "en")
    # weights.default_edge_weight is the fallback weight for an edge with no explicit
    # w; read it here so build_adjacency does not hardcode 0.5.
    cfg["default_edge_weight"] = float((raw.get("weights") or {}).get("default_edge_weight", 0.5))
    # The trust layer is a TOP-LEVEL block (it governs ingest + pack marking), so
    # surface it into the retrieval cfg the renderer reads, merged over its defaults.
    cfg["verification"] = {**_VERIFICATION_DEFAULTS, **(raw.get("verification") or {})}
    return cfg


def _parse(text: str) -> Optional[Tuple[Dict[str, Any], str]]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None          # malformed frontmatter (e.g. a git merge-conflict node) -> skip
    if not isinstance(meta, dict):
        return None
    return meta, m.group(2)


def _node_from_meta(meta: Dict[str, Any], body: str, relpath: str
                    ) -> Optional[Dict[str, Any]]:
    """Build the in-memory node dict (id -> {meta fields, body, text, tokens, _path})
    from a parsed frontmatter + body. `text` is the BM25 bag of words. Shared by the
    nodes/*.md scan and by the SQLite index (index_store) so both produce the SAME
    shape — BM25 / build_adjacency / assemble_pack never need to know the source."""
    nid = meta.get("id")
    if not nid:
        return None
    topics = " ".join(t.get("topic", "") for t in (meta.get("part_of") or [])
                      if isinstance(t, dict))
    # authored-note tags (notes.py) are searchable labels: fold them into the BM25
    # bag so a note is findable by its tag, not only by summary/body words.
    tags = " ".join(str(t) for t in (meta.get("tags") or []) if t)
    text = " ".join([
        nid.split(":", 1)[-1].replace("::", " ").replace("/", " ").replace("_", " "),
        str(meta.get("summary", "")), topics, tags, body[:600],
    ])
    return {
        "id": nid, "type": meta.get("type", "node"),
        "source_path": meta.get("source_path"), "lineno": meta.get("lineno"),
        "line_end": meta.get("line_end"),
        "summary": meta.get("summary", ""), "status": meta.get("status"),
        # Trust fields read by pack marking / verify_claims (provenance itself
        # is NOT projected — retrieve never needs the origin kind, only confidence and
        # the verification verdict).
        "confidence": meta.get("confidence"),
        "verification": meta.get("verification") or {},
        "edges": meta.get("edges") or [], "part_of": meta.get("part_of") or [],
        "body": body, "text": text, "tokens": [w.lower() for w in WORD_RE.findall(text)],
        "_path": relpath,                                    # nodes/<bucket>/<file>.md
    }


def _scan_nodes(store_root: Path) -> Dict[str, Dict[str, Any]]:
    """The canonical, source-of-truth load: walk nodes/*.md and parse each file.
    Correct but O(files) reads + yaml.safe_load each — the cost the index avoids."""
    nodes: Dict[str, Dict[str, Any]] = {}
    nodes_dir = store_root / "nodes"
    if not nodes_dir.exists():
        return nodes
    for p in nodes_dir.rglob("*.md"):
        parsed = _parse(p.read_text(encoding="utf-8", errors="replace"))
        if not parsed:
            continue
        meta, body = parsed
        node = _node_from_meta(meta, body, p.relative_to(store_root).as_posix())
        if node:
            nodes[node["id"]] = node
    return nodes


def load_nodes(store_root: Path) -> Dict[str, Dict[str, Any]]:
    """id -> {meta fields, body, text}. `text` is the bag of words used for BM25.

    Fast path: read the disposable SQLite read-index (index_store) when it is FRESH
    (its stored signature matches a cheap stat-walk of nodes/). Otherwise scan
    nodes/*.md and best-effort rebuild the index for next time. The index is a cache,
    NEVER the source of truth: any mismatch, corruption, or error degrades to the
    scan, never to a wrong result (markdown stays the canon; the scan fallback is unconditional).
    The signature is taken BEFORE the scan, so an index built from a scan that raced a
    concurrent write is tagged with the pre-scan state and simply fails the next
    freshness check (rebuild) rather than ever being trusted stale."""
    store_root = Path(store_root)
    sig: Optional[str] = None
    try:
        import index_store
        fresh = index_store.read_if_fresh(store_root)
        if fresh is not None:
            return fresh
        sig = index_store.signature(store_root)        # BEFORE the scan (race-safe tag)
    except Exception:
        sig = None
    nodes = _scan_nodes(store_root)
    if sig is not None:
        try:
            import index_store
            index_store.build(store_root, nodes, sig)  # best-effort warm for next read
        except Exception:
            pass
    return nodes


# --------------------------------------------------------------------------- #
# Lexical relevance (BM25) -> teleport vector
# --------------------------------------------------------------------------- #

class BM25:
    def __init__(self, nodes: Dict[str, Dict[str, Any]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids = list(nodes)
        self.docs = {nid: nodes[nid]["tokens"] for nid in self.ids}
        self.len = {nid: len(t) for nid, t in self.docs.items()}
        self.avgdl = (sum(self.len.values()) / len(self.len)) if self.len else 0.0
        df: Dict[str, int] = defaultdict(int)
        for toks in self.docs.values():
            for term in set(toks):
                df[term] += 1
        N = max(len(self.ids), 1)
        self.idf = {t: math.log(1 + (N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def scores(self, query: str) -> Dict[str, float]:
        q = [w.lower() for w in WORD_RE.findall(query)]
        out: Dict[str, float] = {}
        for nid in self.ids:
            toks = self.docs[nid]
            if not toks:
                out[nid] = 0.0
                continue
            tf: Dict[str, int] = defaultdict(int)
            for w in toks:
                tf[w] += 1
            dl = self.len[nid]
            s = 0.0
            for term in q:
                if term not in tf:
                    continue
                idf = self.idf.get(term, 0.0)
                num = tf[term] * (self.k1 + 1)
                den = tf[term] + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += idf * num / den
            out[nid] = s
        return out


# --------------------------------------------------------------------------- #
# Graph + Personalized PageRank
# --------------------------------------------------------------------------- #


def _normalize(scores: Dict[str, float]) -> Dict[str, float]:
    """Scale positive scores to sum to 1 (a probability-like seed); 0 stays 0."""
    s = sum(v for v in scores.values() if v > 0)
    if s <= 0:
        return {k: 0.0 for k in scores}
    return {k: (v / s if v > 0 else 0.0) for k, v in scores.items()}

def build_adjacency(nodes: Dict[str, Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, List[Tuple[str, float]]]:
    """Symmetric structural conductance c(u,v) = w_edge * beta(rel).

    Only edges whose target is a known node are kept (so dir-path `part_of`
    strings that don't name a node are simply ignored). Query-independent.
    """
    priors = cfg["relation_priors"]
    default = cfg["relation_prior_default"]
    default_w = cfg.get("default_edge_weight", 0.5)
    acc: Dict[Tuple[str, str], float] = defaultdict(float)

    def add_edge(u: str, rel: str, v: str, w: float) -> None:
        if v not in nodes or u not in nodes or u == v:
            return
        c = float(w) * priors.get(rel, default)
        if c <= 0:
            return
        acc[(u, v)] += c
        acc[(v, u)] += c        # symmetrize: association is bidirectional

    for u, node in nodes.items():
        for e in node["edges"]:
            if isinstance(e, dict) and e.get("to"):
                add_edge(u, e.get("rel", "relates_to"), e["to"], e.get("w", default_w))
        for pm in node["part_of"]:
            if isinstance(pm, dict) and pm.get("topic"):
                add_edge(u, "part_of", pm["topic"], pm.get("w", 0.7))

    adj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for (u, v), c in acc.items():
        adj[u].append((v, c))
    return adj


def personalized_pagerank(teleport: Dict[str, float],
                          adj: Dict[str, List[Tuple[str, float]]],
                          all_ids: List[str], cfg: Dict[str, Any]) -> Dict[str, float]:
    d = cfg["damping"]
    tol = cfg["convergence_tol"]
    max_iter = int(cfg["max_hops"])

    total = sum(teleport.values())
    if total <= 0:
        return {nid: 0.0 for nid in all_ids}
    p = {nid: teleport.get(nid, 0.0) / total for nid in all_ids}
    outsum = {u: sum(w for _, w in nb) for u, nb in adj.items()}

    pi = dict(p)
    for _ in range(max_iter):
        new = {nid: (1 - d) * p[nid] for nid in all_ids}
        dangling = 0.0
        for u in all_ids:
            mass = pi[u]
            if mass <= 0:
                continue
            nb = adj.get(u)
            s = outsum.get(u, 0.0)
            if not nb or s <= 0:
                dangling += mass
                continue
            for v, w in nb:
                new[v] += d * mass * (w / s)
        if dangling > 0:                  # dead-ends teleport
            for nid in all_ids:
                new[nid] += d * dangling * p[nid]
        err = sum(abs(new[nid] - pi[nid]) for nid in all_ids)
        pi = new
        if err < tol:
            break
    return pi


# --------------------------------------------------------------------------- #
# Query intent: history/audit and conflict surfacing
#
# Intent is recognized by the MODEL — the retriever subagent reads the query in ANY
# language and passes an explicit flag — and the code only APPLIES it (meaning is the
# model's job, mechanics the code's; the same split as everywhere in AMG). A history or
# conflict intent lifts the retired-status downrank (§1.12 exception); a conflict intent
# also seeds the conflict subgraph (below). No language-specific keywords live here, so
# this is language- and environment-universal.
# --------------------------------------------------------------------------- #

_CONFLICT_RELS = {"contradicts", "supersedes"}
_RETIRED_STATUSES = {"superseded", "disputed", "rejected"}
INTENT_FLAGS = {"history", "conflict"}


def _conflict_nodes(nodes: Dict[str, Dict[str, Any]]) -> Set[str]:
    """Ids in a contradiction: a retired status (superseded/disputed/rejected), a
    contradicted verification, or an endpoint of a contradicts/supersedes edge. The
    conflict-subgraph seed for a 'show contradictions' query."""
    out: Set[str] = set()
    for nid, n in nodes.items():
        if (n.get("status") in _RETIRED_STATUSES
                or (n.get("verification") or {}).get("status") == "contradicted"):
            out.add(nid)
        for e in n.get("edges", []):
            if isinstance(e, dict) and e.get("rel") in _CONFLICT_RELS and e.get("to") in nodes:
                out.add(nid)
                out.add(e["to"])
    return out


# --------------------------------------------------------------------------- #
# Status prior (re-rank by node validity, after spreading)
# --------------------------------------------------------------------------- #

def _rescale_to_max(activation: Dict[str, float]) -> Dict[str, float]:
    """Rescale activations so the top node reads 1.0. PPR mass sums to 1 over the
    whole graph, so absolute activations shrink as the graph grows — the RANKING
    stays correct, but any absolute cutoff eventually drops everything. After this
    rescale, `activation_threshold` means "share of the top activation": scale-free,
    and identical in effect on a small graph (where the top already dwarfed the
    threshold). An all-zero activation (no seed matched) is returned unchanged —
    the pack is legitimately empty then."""
    peak = max(activation.values(), default=0.0)
    if peak <= 0:
        return activation
    return {nid: a / peak for nid, a in activation.items()}


def _apply_status_prior(activation: Dict[str, float], nodes: Dict[str, Dict[str, Any]],
                        cfg: Dict[str, Any], lift: bool = False) -> Dict[str, float]:
    """Scale final activation by a per-status prior so a superseded claim never
    competes as an active fact. Applied AFTER PPR, so it re-ranks by node validity
    without gating multi-hop flow (which already happened). stale stays at 1.0 — it
    is flagged in the pack (`_STALE_MARK`), not penalized.

    `lift` (set for a history/conflict-intent query) skips the downrank entirely: the
    only sub-1.0 priors are the retired statuses (superseded/disputed/rejected), and the
    user explicitly asked to see them, so they must not be buried (§1.12 exception)."""
    prior = cfg.get("status_prior") or {}
    if not prior or lift:
        return activation
    return {nid: a * float(prior.get(nodes[nid].get("status") or "active", 1.0))
            for nid, a in activation.items()}


def _edge_label(nodes: Dict[str, Dict[str, Any]], u: str, v: str) -> str:
    """Relation type(s) on the edge between u and v, either direction. Adjacency is
    symmetrized and rel-merged, so the label is recovered from the stored edges."""
    seen: List[str] = []
    for a, b in ((u, v), (v, u)):
        n = nodes.get(a) or {}
        for e in n.get("edges", []):
            if isinstance(e, dict) and e.get("to") == b and e.get("rel") and e["rel"] not in seen:
                seen.append(e["rel"])
        for pm in n.get("part_of", []):
            if isinstance(pm, dict) and pm.get("topic") == b and "part_of" not in seen:
                seen.append("part_of")
    return "+".join(seen) or "edge"


def _explain_inflow(ppr: Dict[str, float], adj: Dict[str, List[Tuple[str, float]]],
                    nodes: Dict[str, Dict[str, Any]], cfg: Dict[str, Any], top_ids: List[str],
                    k: int = 3) -> Dict[str, List[Dict[str, Any]]]:
    """For each node in top_ids, the k incoming edges that contributed the most
    activation mass. In PPR the inflow to v from u is d·pi[u]·c(u,v)/outsum[u]; the
    largest terms say which edges drove the node's activation — this grounds the
    'inspect the activation path' claim and makes eval cases easier to label."""
    d = float(cfg["damping"])
    outsum = {u: sum(c for _, c in nb) for u, nb in adj.items()}
    inflow: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for u, nb in adj.items():
        su, pu = outsum.get(u, 0.0), ppr.get(u, 0.0)
        if su <= 0 or pu <= 0:
            continue
        contrib = d * pu / su
        for v, c in nb:
            inflow[v].append((u, contrib * c))
    out: Dict[str, List[Dict[str, Any]]] = {}
    for v in top_ids:
        top = sorted(inflow.get(v, []), key=lambda x: x[1], reverse=True)[:k]
        denom = ppr.get(v, 0.0) or 1.0
        out[v] = [{"from": u, "rel": _edge_label(nodes, u, v),
                   "mass": round(m, 6), "share": round(m / denom, 3)} for u, m in top]
    return out


# --------------------------------------------------------------------------- #
# Pack assembly (budgeted, tiered)
# --------------------------------------------------------------------------- #

# The compact (pointer) profile's budgets are deliberately the CODE defaults above,
# not the config's token_budget: a targeted lookup must not inherit a config that
# widened the full profile for deep-context work. The caller chooses the profile
# (--compact) by the query's nature — a pointer question vs entering the unfamiliar —
# a distinction no scalar statistic of the activations carries (the seeded head and
# the inflow tail overlap in value; field gold sits as deep as rank ~222).
_COMPACT_BUDGET = dict(DEFAULTS["token_budget"])

# Script bands for the token estimate. BPE tokenizers spend ~4 chars/token on ASCII
# text but only ~2.2 on non-Latin alphabetic scripts (Cyrillic, Greek, Arabic, ...)
# and ~1.5 on CJK, so a flat len//4 undercounts non-English text by ~1.5-2x — and every
# token budget computed with it silently overflows on a non-English graph.
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
# CJK ideographs, kana, and hangul - the ~1.5 chars/token band.
_CJK_RE = re.compile("[⺀-鿿぀-ヿ가-힯豈-﫿]")


def _toklen(text: str) -> int:
    """Cheap token estimate, honest across scripts (ASCII ~4 chars/token,
    other alphabets ~2.2, CJK ~1.5). Pure ASCII takes the fast path unchanged."""
    non_ascii = _NON_ASCII_RE.findall(text)
    if not non_ascii:
        return max(1, len(text) // 4)
    cjk = sum(1 for ch in non_ascii if _CJK_RE.match(ch))
    ascii_n = len(text) - len(non_ascii)
    return max(1, int(ascii_n / 4 + (len(non_ascii) - cjk) / 2.2 + cjk / 1.5))


def assemble_pack(activation: Dict[str, float], nodes: Dict[str, Dict[str, Any]], cfg: Dict[str, Any],
                  compact: bool = False) -> Tuple[str, Dict[str, List[str]]]:
    """Greedy tiered assembly under the token budgets. The budgets are the size
    lever, deliberately the only one: an adaptive stop by accumulated activation
    mass was measured on a real field graph and rejected — PPR mass is spread over
    the whole connected graph (it sums to 1 across ALL nodes), so the ranked prefix
    never concentrates enough for a mass cutoff to separate signal from tail
    (shrink x1.01 at mass_stop 0.9); a Jaccard near-duplicate guard likewise moved
    nothing (near-dup curation belongs to consolidation). Raising the relative
    activation threshold trims the pack only where it starts costing recall.

    `compact` is the pointer profile — the CALLER's explicit size choice where the
    rejected statistics could not be one: the built-in modest budgets (_COMPACT_BUDGET,
    ignoring the config's token_budget) and no operational bodies — every file-backed
    node renders as a `path:line — name — summary` pointer line, only the authored
    rulings (decision/adr) keep their rationale body."""
    thr = cfg["activation_threshold"]
    budget = _COMPACT_BUDGET if compact else cfg["token_budget"]
    ranked = sorted((nid for nid, a in activation.items() if a >= thr),
                    key=lambda nid: activation[nid], reverse=True)

    tiers: Dict[str, List[str]] = {"strategic": [], "tactical": [],
                                   "operational": [], "periphery": []}
    spent = {"strategic": 0, "tactical": 0, "operational": 0}

    vcfg = cfg.get("verification") or {}
    for nid in ranked:
        node = nodes[nid]
        tier = TIER_OF_TYPE.get(node["type"], "operational")
        line = _render(node, tier, vcfg, compact)
        cost = _toklen(line)
        if tier in spent and spent[tier] + cost <= budget[tier]:
            tiers[tier].append(nid)
            spent[tier] += cost
        else:
            tiers["periphery"].append(nid)

    tiers["periphery"] = tiers["periphery"][: int(budget["periphery_links"])]
    return _render_pack(tiers, nodes, activation, vcfg, compact), tiers


def _code_pointer(node: Dict[str, Any]) -> str:
    """`path:line` for a code node, widened to `path:start-end` when line_end is known and
    differs, so the model can open the exact slice."""
    sp, ln, le = node.get("source_path"), node.get("lineno"), node.get("line_end")
    if le and ln and le != ln:
        return f"{sp}:{ln}-{le}"
    return f"{sp}:{ln}"


def _render(node: Dict[str, Any], tier: str, vcfg: Dict[str, Any],
            compact: bool = False) -> str:
    nid, summ = node["id"], (node["summary"] or "").strip()
    mark = _trust_marks(node, vcfg)
    if tier == "operational" and node["type"] in CODE_TYPES:
        loc = _code_pointer(node) if node.get("source_path") else nid
        return f"- `{loc}` — {nid.split('::')[-1]} — {summ}{mark}"
    if compact and tier == "operational" and node["type"] not in DOC_BODY_TYPES:
        # The pointer profile: no bodies. A doc/data node gets the same pointer line
        # as code (the summary carries the gist; the model opens the slice if needed);
        # a node with no source (an authored note) keeps its id line. decision/adr
        # fall through below — a ruling's value is its rationale, so its body stays.
        if node.get("source_path") and node.get("lineno"):
            return f"- `{_code_pointer(node)}` — {nid.split('::')[-1]} — {summ}{mark}"
        return f"- {nid} — {summ}{mark}"
    if tier == "operational" or node["type"] in DOC_BODY_TYPES:
        # operational docs/notes, and authored rulings in any tier: include the body
        body = node["body"].strip()
        head = f"### {nid}{mark}\n{summ}"
        return f"{head}\n\n{body}" if body else head
    return f"- {nid} — {summ}{mark}"


def _render_pack(tiers: Dict[str, List[str]], nodes: Dict[str, Dict[str, Any]],
                 activation: Dict[str, float], vcfg: Dict[str, Any],
                 compact: bool = False) -> str:
    out: List[str] = ["# Context pack (compact)" if compact else "# Context pack", ""]
    labels = [("strategic", "Strategic — overview & subsystems"),
              ("tactical", "Tactical — relevant modules"),
              ("operational", "Operational — code & detail in focus")]
    for key, title in labels:
        if not tiers[key]:
            continue
        out.append(f"## {title}")
        for nid in tiers[key]:
            out.append(_render(nodes[nid], key, vcfg, compact))
        out.append("")
    if tiers["periphery"]:
        out.append("## Related (follow if needed)")
        for nid in tiers["periphery"]:
            out.append(f"- {nid} — {(nodes[nid]['summary'] or '').strip()}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def retrieve(store_root: os.PathLike[str] | str, query: str,
             config: Optional[Dict[str, Any]] = None, write_pack: bool = True,
             log_coactivation: bool = True, explain: int = 0,
             intent: Optional[List[str]] = None, compact: bool = False) -> Dict[str, Any]:
    store_root = Path(store_root)
    cfg = config or load_config(store_root)
    nodes = load_nodes(store_root)
    if not nodes:
        return {"ranked": [], "pack": "# Context pack\n(empty graph)\n",
                "tiers": {}, "seeds": [], "n_nodes": 0}

    all_ids = list(nodes)
    bm25 = BM25(nodes)
    rel = bm25.scores(query)
    floor = cfg.get("seed_floor", 0.0)

    # Optional semantic seed enrichment: blend embedding similarity into
    # the teleport vector ONLY. The PPR spread below is unchanged, so multi-hop is
    # preserved and the embedding effect is isolated/measurable. Pure BM25 if no model.
    seed = rel
    emb_scores = None
    try:
        import embed
        emb_scores = embed.seed_scores(embed.get_embedder(cfg), nodes, query,
                                       store_root / "cache" / "embeddings.json")
    except Exception:
        emb_scores = None
    if emb_scores:
        blend = float((cfg.get("embeddings") or {}).get("blend", 0.5))
        rel_n, emb_n = _normalize(rel), _normalize(emb_scores)
        seed = {nid: (1 - blend) * rel_n.get(nid, 0.0) + blend * emb_n.get(nid, 0.0)
                for nid in all_ids}
    teleport = {nid: seed.get(nid, 0.0) + floor for nid in all_ids}

    # Query intent, supplied by the caller (the model recognized it — any
    # language; the code only applies it). A conflict intent ("show contradictions") seeds
    # the conflict subgraph: conflict nodes get teleport mass (so PPR flows through the
    # conflict region), still on top of the query seed — a topical conflict query stays
    # topical, a bare one surfaces conflicts broadly. A history/conflict intent lifts the
    # retired-status downrank below (§1.12 exception).
    flags = {f for f in (intent or []) if f in INTENT_FLAGS}
    if "conflict" in flags:
        cnodes = _conflict_nodes(nodes)
        if cnodes:
            per = (sum(teleport.values()) or 1.0) / len(cnodes)
            for nid in cnodes:
                teleport[nid] = teleport.get(nid, 0.0) + per

    adj = build_adjacency(nodes, cfg)
    ppr = personalized_pagerank(teleport, adj, all_ids, cfg)
    activation = _rescale_to_max(_apply_status_prior(ppr, nodes, cfg, lift=bool(flags)))

    pack, tiers = assemble_pack(activation, nodes, cfg, compact)
    ranked = sorted(((nid, activation[nid]) for nid in all_ids),
                    key=lambda kv: kv[1], reverse=True)
    seeds = sorted((nid for nid in all_ids if seed.get(nid, 0.0) > 0),
                   key=lambda nid: seed[nid], reverse=True)[:8]

    if write_pack:
        _atomic_write(store_root / "cache" / "pack.md", pack)
    if log_coactivation:
        _log_coactivation(store_root, query, tiers, adj)
        _log_pack(store_root, query, tiers, nodes)

    # Lazy derivation, the first-touch half: activated nodes still awaiting a summary
    # (status stale) so the amg-retrieve skill can derive them synchronously BEFORE the
    # answer (first touch is never empty). Harmless under eager — normally empty. Pack
    # order strategic..periphery, so the most prominent stale node comes first.
    stale_in_pack = [nid for tier in ("strategic", "tactical", "operational", "periphery")
                     for nid in tiers.get(tier, []) if nodes[nid].get("status") == "stale"]
    result = {"ranked": ranked, "pack": pack, "tiers": tiers, "seeds": seeds,
              "relevance": rel, "n_nodes": len(nodes), "intent": sorted(flags),
              "stale_in_pack": stale_in_pack}
    if explain:                           # decompose inflow on the RAW ppr (pre-prior)
        result["explain"] = _explain_inflow(ppr, adj, nodes, cfg,
                                            [nid for nid, _ in ranked[:explain]])
    return result


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _log_coactivation(store_root: Path, query: str, tiers: Dict[str, List[str]],
                      adj: Dict[str, List[Tuple[str, float]]]) -> None:
    """Append-only Hebbian signal. Consolidation folds these into edge weights."""
    inpack = set(tiers.get("strategic", []) + tiers.get("tactical", []) +
                 tiers.get("operational", []))
    pairs = []
    for u in inpack:
        for v, _ in adj.get(u, []):
            if v in inpack and u < v:
                pairs.append([u, v])
    if not pairs:
        return
    try:
        line = json.dumps({"ts": time.time(), "q": query[:120], "coactivated": pairs},
                          ensure_ascii=False) + "\n"
        path = store_root / "work" / "coactivation.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _log_pack(store_root: Path, query: str, tiers: Dict[str, List[str]],
              nodes: Dict[str, Dict[str, Any]]) -> None:
    """Append-only record of WHAT WAS IN THE PACK (id + source_path) to
    work/pack-log.jsonl. Session-end (lifecycle) intersects this with the files actually
    edited in the session to derive the USAGE provenance (usage.log) — the non-circular
    signal for the improved (outcome-gated) Hebbian rule. It is kept SEPARATE
    from coactivation.log: that is blind pack co-membership (circular, §8.1); this is the
    surface a real outcome can be attributed to. Lock-free, best-effort."""
    inpack = (tiers.get("strategic", []) + tiers.get("tactical", [])
              + tiers.get("operational", []))
    if not inpack:
        return
    items = [{"id": nid, "source_path": nodes[nid].get("source_path")} for nid in inpack]
    try:
        line = json.dumps({"ts": time.time(), "q": query[:120], "pack": items},
                          ensure_ascii=False) + "\n"
        path = store_root / "work" / "pack-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    query = argv[1]
    store = Path(_default_store())
    write_pack = True
    explain = False
    compact = False
    top = 15
    intent: List[str] = []                # set by the caller (the model) per its query
    i = 2
    while i < len(argv):
        if argv[i] == "--store":
            store = Path(argv[i + 1]); i += 2
        elif argv[i] == "--top":
            top = int(argv[i + 1]); i += 2
        elif argv[i] == "--no-pack":
            write_pack = False; i += 1
        elif argv[i] == "--explain":
            explain = True; i += 1
        elif argv[i] == "--compact":      # the pointer profile; see header
            compact = True; i += 1
        elif argv[i] == "--intent":       # history|conflict (comma-separated); see header
            intent = [f.strip() for f in argv[i + 1].split(",") if f.strip()]; i += 2
        else:
            i += 1

    n_explain = min(top, 10) if explain else 0
    res = retrieve(store, query, write_pack=write_pack,
                   log_coactivation=write_pack, explain=n_explain, intent=intent,
                   compact=compact)
    print(res["pack"])
    print("\n--- ranked (top {}) ---".format(top))
    for nid, a in res["ranked"][:top]:
        print(f"{a:8.4f}  {nid}")
    if res.get("stale_in_pack"):           # lazy derivation: derive these first
        print("\n--- stale in pack (lazy: derive before relying) ---")
        for nid in res["stale_in_pack"]:
            print(f"  {nid}")
    if n_explain:
        print("\n--- explain: top edges that drove each node "
              "(share = % of its activation) ---")
        ex = res.get("explain", {})
        for nid, _ in res["ranked"][:n_explain]:
            print(nid)
            for c in ex.get(nid, []):
                print(f"    <- {c['share'] * 100:5.1f}%  {c['rel']:<14} {c['from']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
