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
from typing import Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:                       # pragma: no cover
    sys.stderr.write("retrieve.py needs PyYAML: pip install pyyaml\n")
    raise

# Windows consoles default to cp1252; force UTF-8 stdout so non-ASCII content
# (Cyrillic summaries, paths) prints without crashing.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
# Unicode word characters (\w is Unicode by default in Python 3): this MUST match
# non-Latin scripts. An ASCII-only [A-Za-z0-9_]+ silently drops Cyrillic/CJK/etc.,
# so a Russian-language graph would be invisible to BM25 and every non-Latin query
# would seed nothing.
WORD_RE = re.compile(r"\w+", re.UNICODE)

DEFAULTS = {
    "damping": 0.85,
    "max_hops": 30,                 # power-iteration cap (more iters => wider reach)
    "convergence_tol": 1e-6,
    "activation_threshold": 0.02,
    "seed_floor": 0.0,              # teleport mass given to every node (0 = pure relevance)
    "token_budget": {"strategic": 1200, "tactical": 2500,
                     "operational": 6000, "periphery_links": 40},
    "relation_priors": {
        "documents": 0.9, "specifies": 0.9, "implements": 0.9,
        "calls": 0.8, "depends_on": 0.8, "defines": 0.7, "part_of": 0.7,
        "imports": 0.6, "refines": 0.6, "exemplifies": 0.6, "relates_to": 0.5,
        "supersedes": 0.5, "contradicts": 0.3,
    },
    "relation_prior_default": 0.5,
    # Optional semantic seed enrichment (Stage 1.5). enabled: auto|on|off;
    # blend: 0=pure BM25 .. 1=pure semantic. Falls back to BM25 if no backend.
    "embeddings": {"enabled": "auto", "backend": "auto", "model": "", "blend": 0.5},
}

# Which node types land in which abstraction tier of the pack.
TIER_OF_TYPE = {
    "hub": "strategic", "overview": "strategic",
    "module": "tactical", "class": "tactical", "package": "tactical",
    "function": "operational", "section": "operational",
    "file": "operational", "method": "operational",
}
CODE_TYPES = {"module", "class", "function", "method", "file"}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def _default_store() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3] / "amg"        # .../.claude/amg


def load_config(store_root: Path) -> dict:
    cfg = dict(DEFAULTS)
    f = store_root / "config.yml"
    if f.exists():
        user = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("retrieval", {}) or {}
        for k, v in user.items():
            if k == "token_budget" and isinstance(v, dict):
                cfg["token_budget"] = {**DEFAULTS["token_budget"], **v}
            elif k == "embeddings" and isinstance(v, dict):
                cfg["embeddings"] = {**DEFAULTS["embeddings"], **v}
            else:
                cfg[k] = v
    return cfg


def _parse(text: str) -> Optional[Tuple[dict, str]]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    return meta, m.group(2)


def load_nodes(store_root: Path) -> Dict[str, dict]:
    """id -> {meta fields, body, text}. `text` is the bag of words used for BM25."""
    nodes: Dict[str, dict] = {}
    nodes_dir = store_root / "nodes"
    if not nodes_dir.exists():
        return nodes
    for p in nodes_dir.rglob("*.md"):
        parsed = _parse(p.read_text(encoding="utf-8", errors="replace"))
        if not parsed:
            continue
        meta, body = parsed
        nid = meta.get("id")
        if not nid:
            continue
        topics = " ".join(t.get("topic", "") for t in (meta.get("part_of") or [])
                          if isinstance(t, dict))
        text = " ".join([
            nid.split(":", 1)[-1].replace("::", " ").replace("/", " ").replace("_", " "),
            str(meta.get("summary", "")), topics, body[:600],
        ])
        nodes[nid] = {
            "id": nid, "type": meta.get("type", "node"),
            "source_path": meta.get("source_path"), "lineno": meta.get("lineno"),
            "summary": meta.get("summary", ""), "status": meta.get("status"),
            "edges": meta.get("edges") or [], "part_of": meta.get("part_of") or [],
            "body": body, "text": text, "tokens": [w.lower() for w in WORD_RE.findall(text)],
        }
    return nodes


# --------------------------------------------------------------------------- #
# Lexical relevance (BM25) -> teleport vector
# --------------------------------------------------------------------------- #

class BM25:
    def __init__(self, nodes: Dict[str, dict], k1: float = 1.5, b: float = 0.75):
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

def build_adjacency(nodes: Dict[str, dict], cfg: dict) -> Dict[str, List[Tuple[str, float]]]:
    """Symmetric structural conductance c(u,v) = w_edge * beta(rel).

    Only edges whose target is a known node are kept (so dir-path `part_of`
    strings that don't name a node are simply ignored). Query-independent.
    """
    priors = cfg["relation_priors"]
    default = cfg["relation_prior_default"]
    acc: Dict[Tuple[str, str], float] = defaultdict(float)

    def add_edge(u: str, rel: str, v: str, w: float):
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
                add_edge(u, e.get("rel", "relates_to"), e["to"], e.get("w", 0.5))
        for pm in node["part_of"]:
            if isinstance(pm, dict) and pm.get("topic"):
                add_edge(u, "part_of", pm["topic"], pm.get("w", 0.7))

    adj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for (u, v), c in acc.items():
        adj[u].append((v, c))
    return adj


def personalized_pagerank(teleport: Dict[str, float],
                          adj: Dict[str, List[Tuple[str, float]]],
                          all_ids: List[str], cfg: dict) -> Dict[str, float]:
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
# Pack assembly (budgeted, tiered)
# --------------------------------------------------------------------------- #

def _toklen(text: str) -> int:
    return max(1, len(text) // 4)         # cheap token estimate (~4 chars/token)


def assemble_pack(activation: Dict[str, float], nodes: Dict[str, dict], cfg: dict
                  ) -> Tuple[str, Dict[str, List[str]]]:
    thr = cfg["activation_threshold"]
    budget = cfg["token_budget"]
    ranked = sorted((nid for nid, a in activation.items() if a >= thr),
                    key=lambda nid: activation[nid], reverse=True)

    tiers: Dict[str, List[str]] = {"strategic": [], "tactical": [],
                                   "operational": [], "periphery": []}
    spent = {"strategic": 0, "tactical": 0, "operational": 0}

    for nid in ranked:
        node = nodes[nid]
        tier = TIER_OF_TYPE.get(node["type"], "operational")
        line = _render(node, tier)
        cost = _toklen(line)
        if tier in spent and spent[tier] + cost <= budget[tier]:
            tiers[tier].append(nid)
            spent[tier] += cost
        else:
            tiers["periphery"].append(nid)

    tiers["periphery"] = tiers["periphery"][: int(budget["periphery_links"])]
    return _render_pack(tiers, nodes, activation), tiers


def _render(node: dict, tier: str) -> str:
    nid, summ = node["id"], (node["summary"] or "").strip()
    if tier == "operational" and node["type"] in CODE_TYPES:
        loc = f"{node['source_path']}:{node['lineno']}" if node.get("source_path") else nid
        return f"- `{loc}` — {nid.split('::')[-1]} — {summ}"
    if tier == "operational":            # docs / notes: include the text we own
        body = node["body"].strip()
        return f"### {nid}\n{summ}\n\n{body}" if body else f"### {nid}\n{summ}"
    return f"- {nid} — {summ}"


def _render_pack(tiers, nodes, activation) -> str:
    out: List[str] = ["# Context pack", ""]
    labels = [("strategic", "Strategic — overview & subsystems"),
              ("tactical", "Tactical — relevant modules"),
              ("operational", "Operational — code & detail in focus")]
    for key, title in labels:
        if not tiers[key]:
            continue
        out.append(f"## {title}")
        for nid in tiers[key]:
            out.append(_render(nodes[nid], key))
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

def retrieve(store_root: os.PathLike | str, query: str,
             config: Optional[dict] = None, write_pack: bool = True,
             log_coactivation: bool = True) -> dict:
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

    # Optional semantic seed enrichment (Stage 1.5): blend embedding similarity into
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

    adj = build_adjacency(nodes, cfg)
    activation = personalized_pagerank(teleport, adj, all_ids, cfg)

    pack, tiers = assemble_pack(activation, nodes, cfg)
    ranked = sorted(((nid, activation[nid]) for nid in all_ids),
                    key=lambda kv: kv[1], reverse=True)
    seeds = sorted((nid for nid in all_ids if seed.get(nid, 0.0) > 0),
                   key=lambda nid: seed[nid], reverse=True)[:8]

    if write_pack:
        _atomic_write(store_root / "cache" / "pack.md", pack)
    if log_coactivation:
        _log_coactivation(store_root, query, tiers, adj)

    return {"ranked": ranked, "pack": pack, "tiers": tiers,
            "seeds": seeds, "relevance": rel, "n_nodes": len(nodes)}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _log_coactivation(store_root: Path, query: str, tiers: dict, adj: dict) -> None:
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
    top = 15
    i = 2
    while i < len(argv):
        if argv[i] == "--store":
            store = Path(argv[i + 1]); i += 2
        elif argv[i] == "--top":
            top = int(argv[i + 1]); i += 2
        elif argv[i] == "--no-pack":
            write_pack = False; i += 1
        else:
            i += 1

    res = retrieve(store, query, write_pack=write_pack, log_coactivation=write_pack)
    print(res["pack"])
    print("\n--- ranked (top {}) ---".format(top))
    for nid, a in res["ranked"][:top]:
        print(f"{a:8.4f}  {nid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
