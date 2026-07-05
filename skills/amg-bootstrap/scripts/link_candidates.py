#!/usr/bin/env python3
"""
link_candidates.py — deterministic preparation for the GLOBAL semantic linking pass.

A per-batch builder cannot see across domains by construction, so cross-domain
edges (doc <-> code, example <-> guide, ADR -> code) need a global pass. Its raw
material is prepared here, deterministically and without a model:

  * candidate pairs : for every derived node, the top-K most similar nodes from
    OTHER domains (or other files of the same domain) that are not yet linked —
    by embedding cosine over the already-cached seed vectors (embed.py) when a
    backend is available, else by a lexical token-overlap fallback (the same soft
    degradation retrieval seeding uses). Similarity only NOMINATES; the amg-linker
    subagent confirms or rejects each candidate by meaning and sets weights.
  * batches         : work/link-batch-<nnn>.json — bounded slices of "node + its
    candidates' summaries" plus the global hub list, so each linker instance runs
    in a small context while the candidate set stays globally informed (similarity
    over the whole graph — batch execution, global reach; not a mega-agent).
  * hub candidates  : --hubs writes work/hub-candidates.json — deterministic hub
    anchors from the directory structure (subtree sizes), so amg-synth names and
    refines a STABLE taxonomy instead of inventing hub ids anew per rebuild.

Reads the summary layer through retrieve.load_nodes (the SQLite read-index — no
nodes/*.md rescan). Read-only over the graph; writes only work/ files. Stale
(not-yet-derived) nodes are skipped: they have no summary to link by — under lazy
derivation they join the pass as they are derived, so the pass is incrementally
re-runnable (already-linked pairs are never re-nominated).

CLI:
  python link_candidates.py [<project_root>] [--root <agent_dir>]
        [--top K] [--batch N] [--min-sim S]
  python link_candidates.py --hubs [<project_root>] [--root <agent_dir>]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import graph_store as gs
from extract_structure import load_config
from partition_queue import subtree_key

# Cross-skill import of the retrieval layer (established pattern: consolidate ->
# graph_store, lifecycle -> consolidate). retrieve gives the index-backed node
# loader; embed gives the cached seed vectors the candidates reuse.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "amg-retrieve" / "scripts"))
import embed as em                                          # noqa: E402
import retrieve as rt                                       # noqa: E402

# Tunables of the linking pass, overridable via the `linker` config block: how many
# candidates each node nominates, the embedding-cosine floor below which a nominee
# is noise, and how many nodes one linker batch carries (a bounded subagent context).
LINKER_DEFAULTS: Dict[str, Any] = {"top_k": 5, "min_sim": 0.35, "batch_nodes": 40}

# The lexical fallback nominates only pairs sharing at least this many informative
# tokens — with no embedding backend a single shared word is noise, not a signal.
_MIN_SHARED_TOKENS = 2

# Directory groups smaller than this never become hub candidates: a hub over two
# files adds a level without adding orientation.
_MIN_HUB_MEMBERS = 3


def _bucket(node: Dict[str, Any]) -> str:
    """The node's physical bucket (code/doc/data/notes/_hubs) from its on-disk path."""
    parts = (node.get("_path") or "").split("/")
    return parts[1] if len(parts) > 1 else "?"


def _eligible(nodes: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Nodes the linking pass can work with: derived (a summary exists) and not
    stale — a deferred lazy node joins later, when its first touch derives it."""
    return {nid: n for nid, n in nodes.items()
            if (n.get("summary") or "").strip() and n.get("status") != "stale"}


def _linked_pairs(nodes: Dict[str, Dict[str, Any]]) -> Set[frozenset[str]]:
    """Unordered pairs already connected by any edge — never re-nominated."""
    out: Set[frozenset[str]] = set()
    for nid, n in nodes.items():
        for e in n.get("edges") or []:
            if isinstance(e, dict) and e.get("to") in nodes:
                out.add(frozenset((nid, str(e["to"]))))
    return out


def _pair_allowed(a: str, b: str, nodes: Dict[str, Dict[str, Any]],
                  linked: Set[frozenset[str]]) -> bool:
    """A nominee is useful when it could carry a CROSS link the builder missed:
    another domain, or another file of the same domain; same-file neighbors are
    already linked structurally, and existing pairs need no confirmation."""
    if a == b or frozenset((a, b)) in linked:
        return False
    na, nb = nodes[a], nodes[b]
    if _bucket(na) != _bucket(nb):
        return True
    sa, sb = na.get("source_path"), nb.get("source_path")
    return bool(sa and sb and sa != sb)


def _embedding_candidates(order: List[str], vecs: Dict[str, List[float]],
                          nodes: Dict[str, Dict[str, Any]], linked: Set[frozenset[str]],
                          top_k: int, min_sim: float) -> Optional[Dict[str, List[Tuple[str, float]]]]:
    """Top-K allowed nominees per node by embedding cosine, or None when numpy is
    unavailable (both embedding backends depend on it, so this only happens when
    the vectors themselves could not exist either). Block-wise matrix products keep
    memory bounded on big graphs."""
    try:
        import numpy as np
    except ImportError:                       # pragma: no cover - backends need numpy
        return None
    ids = [nid for nid in order if nid in vecs]
    if len(ids) < 2:
        return {}
    mat = np.asarray([vecs[nid] for nid in ids], dtype=np.float32)
    out: Dict[str, List[Tuple[str, float]]] = {}
    probe = min(len(ids) - 1, max(top_k * 6, 16))   # oversample, then filter eligibility
    for start in range(0, len(ids), 512):
        block = mat[start:start + 512] @ mat.T      # (rows, N) cosine (unit vectors)
        for i in range(block.shape[0]):
            row = block[i]
            src = ids[start + i]
            idx = np.argpartition(-row, probe)[:probe + 1]
            ranked = sorted(((float(row[j]), ids[int(j)]) for j in idx), reverse=True)
            picks: List[Tuple[str, float]] = []
            for sim, other in ranked:
                if sim < min_sim:
                    break
                if _pair_allowed(src, other, nodes, linked):
                    picks.append((other, round(sim, 4)))
                    if len(picks) >= top_k:
                        break
            if picks:
                out[src] = picks
    return out


def _lexical_candidates(order: List[str], nodes: Dict[str, Dict[str, Any]],
                        linked: Set[frozenset[str]], top_k: int
                        ) -> Dict[str, List[Tuple[str, float]]]:
    """The no-backend fallback: nominate by informative-token overlap (Jaccard) over
    the summary bag, via an inverted index (never all-pairs). Deterministic and
    offline; weaker than embeddings, which is exactly the seeding trade-off."""
    toks: Dict[str, Set[str]] = {
        nid: {t for t in nodes[nid].get("tokens") or [] if len(t) >= 3}
        for nid in order}
    df: Counter[str] = Counter(t for ts in toks.values() for t in ts)
    ceiling = max(4, len(order) // 4)         # a token in >25% of nodes separates nothing
    inv: Dict[str, List[str]] = defaultdict(list)
    for nid in order:
        for t in toks[nid]:
            if df[t] <= ceiling:
                inv[t].append(nid)
    out: Dict[str, List[Tuple[str, float]]] = {}
    for nid in order:
        shared: Counter[str] = Counter()
        for t in toks[nid]:
            if df[t] <= ceiling:
                for other in inv[t]:
                    if other != nid:
                        shared[other] += 1
        scored = []
        for other, n_shared in shared.items():
            if n_shared < _MIN_SHARED_TOKENS:
                continue
            if not _pair_allowed(nid, other, nodes, linked):
                continue
            union = len(toks[nid] | toks[other]) or 1
            scored.append((round(n_shared / union, 4), other))
        scored.sort(key=lambda s: (-s[0], s[1]))
        if scored:
            out[nid] = [(other, sim) for sim, other in scored[:top_k]]
    return out


def build_batches(project_root: Path, amg_root: Path,
                  overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Nominate candidates for every eligible node and write the linker batches to
    work/link-batch-<nnn>.json. Returns the pass summary (mode, counts, batches)."""
    config = load_config(amg_root)
    cfg = {**LINKER_DEFAULTS, **(config.get("linker") or {}), **(overrides or {})}
    top_k, min_sim = int(cfg["top_k"]), float(cfg["min_sim"])
    batch_nodes = max(1, int(cfg["batch_nodes"]))

    nodes = rt.load_nodes(amg_root)
    eligible = _eligible(nodes)
    linked = _linked_pairs(nodes)
    order = sorted(eligible)                  # deterministic nomination order

    mode = "lexical"
    cands: Optional[Dict[str, List[Tuple[str, float]]]] = None
    embedder = em.get_embedder(rt.load_config(amg_root))
    if embedder is not None:
        vecs = em.node_embeddings(embedder, eligible,
                                  amg_root / "cache" / "embeddings.json")
        cands = _embedding_candidates(order, vecs, eligible, linked, top_k, min_sim)
        if cands is not None:
            mode = "embeddings"
    if cands is None:
        cands = _lexical_candidates(order, eligible, linked, top_k)

    hubs = sorted((nid for nid, n in nodes.items()
                   if n.get("type") in ("hub", "overview")))
    hub_rows = [{"id": h, "summary": nodes[h].get("summary", "")} for h in hubs]

    entries = [{"id": nid, "type": eligible[nid].get("type"),
                "source_path": eligible[nid].get("source_path"),
                "summary": eligible[nid].get("summary", ""),
                "candidates": [
                    {"id": other, "type": eligible[other].get("type"),
                     "source_path": eligible[other].get("source_path"),
                     "summary": eligible[other].get("summary", ""), "sim": sim}
                    for other, sim in cands.get(nid, [])]}
               for nid in order if cands.get(nid)]

    work = amg_root / "work"
    for old in work.glob("link-batch-*.json") if work.exists() else []:
        try:
            old.unlink()                      # a re-run replaces the batch set
        except OSError:
            pass     # transiently held (AV/indexer on Windows): must not kill the pass
    batches = 0
    for start in range(0, len(entries), batch_nodes):
        batches += 1
        gs.atomic_write_text(
            work / f"link-batch-{batches:03d}.json",
            json.dumps({"part": batches, "mode": mode, "hubs": hub_rows,
                        "nodes": entries[start:start + batch_nodes]},
                       ensure_ascii=False, indent=2))
    return {"mode": mode, "eligible": len(eligible), "with_candidates": len(entries),
            "batches": batches, "hubs": len(hub_rows),
            "skipped_stale": sum(1 for n in nodes.values() if n.get("status") == "stale")}


def _hub_slug(topic: str) -> str:
    s = re.sub(r"[^\w]+", "-", topic.strip().lower()).strip("-")
    return s[:48] or "root"


def hub_candidates(amg_root: Path) -> Dict[str, Any]:
    """Deterministic hub anchors from the directory structure: every
    source subtree with enough file-backed nodes suggests one stable hub id, so the
    synthesis names/refines a taxonomy that stays put across rebuilds instead of
    inventing new hub ids every run. Writes work/hub-candidates.json."""
    nodes = rt.load_nodes(amg_root)
    groups: Dict[str, List[str]] = defaultdict(list)
    for nid, n in sorted(nodes.items()):
        sp = n.get("source_path")
        if sp:
            groups[subtree_key(str(sp), 2)].append(nid)
    rows = [{"topic_dir": key, "suggested_id": f"hub:{_hub_slug(key)}",
             "members": len(ids), "sample": ids[:8]}
            for key, ids in sorted(groups.items()) if len(ids) >= _MIN_HUB_MEMBERS]
    existing = sorted(nid for nid, n in nodes.items()
                      if n.get("type") in ("hub", "overview"))
    out = {"candidates": rows, "existing_hubs": existing}
    gs.atomic_write_text(amg_root / "work" / "hub-candidates.json",
                         json.dumps(out, ensure_ascii=False, indent=2))
    return {"candidates": len(rows), "existing_hubs": len(existing)}


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    overrides: Dict[str, Any] = {}
    for flag, key in (("--top", "top_k"), ("--batch", "batch_nodes"),
                      ("--min-sim", "min_sim")):
        if flag in args:
            i = args.index(flag)
            overrides[key] = float(args[i + 1]) if key == "min_sim" else int(args[i + 1])
            del args[i:i + 2]
    hubs_mode = "--hubs" in args
    if hubs_mode:
        args.remove("--hubs")
    project_root = Path(args[0]).resolve() if args else Path.cwd()
    amg_root = gs.resolve_amg_root(cli_root, project_root)
    if hubs_mode:
        print(json.dumps(hub_candidates(amg_root), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(build_batches(project_root, amg_root, overrides),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
