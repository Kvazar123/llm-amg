#!/usr/bin/env python3
"""
bench.py — performance ruler for AMG at scale. Read-only with respect to any graph
it measures; it never mutates the nodes of a store you point it at.

Purpose: turn "is the graph still fast at N nodes?" into a number, and make the
speed-up from the generated read-index (roadmap Stage 12, Group 2) a measured
before/after — run this BEFORE the index for a baseline, then again after.

It times the hot paths the index targets, all over existing engine modules:
  * load (scan)     — a full nodes/*.md scan + yaml.safe_load of every file: the
                      per-query cost BEFORE the index (06-retrieval). THE headline.
  * load (index)    — an index-backed read of the same nodes AFTER the generated
                      SQLite read-index (index_store); the scan/index speedup is
                      reported side by side, plus the index build/rebuild cost.
  * build_adjacency — symmetric structural conductance over all edges.
  * retrieve        — one end-to-end query (seed -> PPR -> pack), writes OFF so the
                      measurement never pollutes the pack or the co-activation log.
  * eval            — eval_retrieval.run over the labeled cases (the measurement suite).
  * bootstrap       — reconcile.plan over a real source tree (only with --project),
                      timed into a throwaway store so no real graph is touched.

Embeddings are forced OFF: bench measures GRAPH operations, not a model download
(the semantic seed is a separate, backend-bound cost). Each op is timed best-of-N
to cut noise (the minimum is the least-perturbed run).

A self-contained synthetic generator makes the bench reproducible offline and in CI;
it is NOT coupled to ../amg-bigtest (that stand has its own role — the Hebbian
measurement). Point --store at any real graph (incl. amg-bigtest) for real numbers.

CLI:
  python bench.py --make-bench /tmp/amg-bench --nodes 5000 [--seed 0]
  python bench.py --store <.../amg> [--queries N] [--repeats R] [--out bench.json]
  python bench.py --make-bench /tmp/amg-bench --nodes 20000 --project <src tree>
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retrieve as R                          # same-dir engine modules
import eval_retrieval as E

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #

def _best(fn: Callable[[], Any], repeats: int) -> float:
    """Run `fn` `repeats` times; return the BEST (minimum) wall time in seconds.
    The minimum is the run least perturbed by GC / OS scheduling, so it is the
    fairest single number for a deterministic, read-only operation."""
    best = float("inf")
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def _embeddings_off(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Force the semantic seed off so retrieve/eval measure graph ops, not a model
    load. Copies the cfg (does not mutate the caller's)."""
    return {**cfg, "embeddings": {**(cfg.get("embeddings") or {}), "enabled": "off"}}


# --------------------------------------------------------------------------- #
# Measurement over an existing store
# --------------------------------------------------------------------------- #

def _sample_queries(nodes: Dict[str, Dict[str, Any]], k: int) -> List[str]:
    """A handful of realistic queries drawn from node summaries — enough lexical
    overlap to seed BM25 and drive a real spread. Deterministic (sorted ids)."""
    out: List[str] = []
    for nid in sorted(nodes):
        summ = (nodes[nid].get("summary") or "").strip()
        if summ:
            out.append(" ".join(summ.split()[:8]))
        if len(out) >= k:
            break
    return out or ["overview"]


def _load_cases(store: Path) -> List[Dict[str, Any]]:
    """Labeled cases for the eval timing: the store's own cases.json if present,
    else []. Correctness is irrelevant here — cases only give retrieve real work."""
    f = store / "cases.json"
    if not f.exists():
        return []
    try:
        cases = json.loads(f.read_text(encoding="utf-8"))
        return cases if isinstance(cases, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def bench_store(store: Path, repeats: int = 3, n_queries: int = 8,
                project_root: Optional[Path] = None) -> Dict[str, Any]:
    """Time the read-only hot paths on the graph at `store`. Returns a dict of
    seconds per operation plus the node count. Mutates nothing under `store` except
    the disposable cache/index.sqlite (built/measured here; it is regenerated cache)."""
    cfg = _embeddings_off(R.load_config(store))

    # The headline before/after: a full nodes/*.md scan vs an index-backed read.
    t_scan = _best(lambda: R._scan_nodes(store), repeats)
    nodes: Dict[str, Dict[str, Any]] = R._scan_nodes(store)
    n = len(nodes)
    out: Dict[str, Any] = {"n_nodes": n, "scan_s": round(t_scan, 4)}

    try:                                               # index timings (Group 2)
        import index_store as IX
        sig = IX.signature(store)
        t_build = _best(lambda: IX.build(store, nodes, sig), repeats)
        IX.build(store, nodes, sig)                     # leave it fresh for retrieve below
        t_index = _best(lambda: IX.read_if_fresh(store), repeats)
        out["index_build_s"] = round(t_build, 4)
        out["index_read_s"] = round(t_index, 4)
        out["speedup"] = round(t_scan / t_index, 1) if t_index > 0 else None
    except Exception:
        out["index_build_s"] = out["index_read_s"] = out["speedup"] = None

    t_adj = _best(lambda: R.build_adjacency(nodes, cfg), repeats) if nodes else None

    queries = _sample_queries(nodes, n_queries)

    def _retr() -> None:                               # retrieve now reads the warm index
        for q in queries:
            R.retrieve(store, q, config=cfg, write_pack=False, log_coactivation=False)
    t_retr = (_best(_retr, repeats) / len(queries)) if nodes else None

    cases = _load_cases(store)
    # eval is heavier (a retrieve per case); time it once, not best-of-N.
    t_eval = _best(lambda: E.run(store, cases, cfg), 1) if (cases and nodes) else None

    out.update({
        "build_adjacency_s": round(t_adj, 4) if t_adj is not None else None,
        "retrieve_s_per_query": round(t_retr, 4) if t_retr is not None else None,
        "eval_s": round(t_eval, 4) if t_eval is not None else None,
        "queries": len(queries),
        "eval_cases": len(cases),
    })
    if project_root is not None:
        out["bootstrap"] = _bench_bootstrap(project_root)
    return out


def _bench_bootstrap(project_root: Path) -> Dict[str, Any]:
    """Time a full reconcile.plan (bootstrap) over a real source tree, into a
    THROWAWAY store so no real graph is touched. reconcile lives in the sibling
    amg-bootstrap skill — soft-import via sys.path, the established cross-skill
    pattern; report a skip if it is not importable."""
    boot_dir = HERE.parents[1] / "amg-bootstrap" / "scripts"
    if str(boot_dir) not in sys.path:
        sys.path.insert(0, str(boot_dir))
    try:
        import reconcile as rc
    except Exception:
        return {"skipped": "reconcile not importable"}
    tmp = Path(tempfile.mkdtemp(prefix="amg-bench-boot-"))
    try:
        amg = tmp / "amg"
        amg.mkdir(parents=True)
        (amg / "config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: .\n", encoding="utf-8")
        t0 = time.perf_counter()
        summary = rc.plan(project_root, amg)
        dt = time.perf_counter() - t0
        return {"bootstrap_s": round(dt, 4),
                "added": summary.get("added", 0),
                "queued": summary.get("queued_for_semantic", 0)}
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Self-contained synthetic graph (offline, reproducible)
# --------------------------------------------------------------------------- #

_VERBS = ["validate", "compute", "resolve", "apply", "build", "fetch", "record",
          "dispatch", "settle", "reserve", "render", "sign", "verify", "aggregate",
          "normalize", "schedule", "retry", "cancel", "refund", "charge"]
_NOUNS = ["request", "amount", "token", "policy", "record", "balance", "label",
          "session", "invoice", "rate", "quantity", "receipt", "entry", "report",
          "discount", "credential", "shipment", "posting", "message", "metric"]
_DOMAINS = ["api", "orders", "payments", "billing", "ledger", "inventory",
            "shipping", "users", "notify", "reporting", "search", "catalog"]


def make_bench_graph(root: Path, n_nodes: int, seed: int = 0,
                     funcs_per_module: int = 5, modules_per_domain: int = 6
                     ) -> Dict[str, Any]:
    """Write a synthetic graph of about `n_nodes` nodes directly into root/nodes/.

    Shape mirrors a real codebase graph so the hot paths do representative work:
    domain hubs; module nodes (imports edges); function nodes (calls to siblings +
    a cross-module depends_on chain for real multi-hop reach) with source_path /
    lineno; doc sections (documents edges); part_of from members to the domain hub.
    Summaries share a vocabulary so BM25 has realistic term frequency / idf. Bodies
    are empty except hubs (as in a real graph — code bodies are pointers). Embeddings
    are off. Deterministic given (n_nodes, seed). Also writes a small cases.json."""
    rng = random.Random(seed)
    nd = root / "nodes"
    for sub in ("code", "doc", "_hubs"):
        (nd / sub).mkdir(parents=True, exist_ok=True)
    (root / "config.yml").write_text(
        "active: true\nworking_language: en\nretrieval:\n  embeddings:\n    enabled: off\n",
        encoding="utf-8")

    # 1. Lay out the structure: ids first, so edges can reference any final node.
    func_ids: List[str] = []
    func_meta: List[Dict[str, Any]] = []        # (id, domain, module_id, lineno, summary)
    module_ids: List[str] = []
    hub_ids: List[str] = []
    di = 0
    while len(func_ids) < n_nodes:
        domain = f"{_DOMAINS[di % len(_DOMAINS)]}{di // len(_DOMAINS)}"
        hub_ids.append(f"hub:{domain}")
        for mi in range(modules_per_domain):
            mod_path = f"src/{domain}/m{mi}.py"
            mid = f"code:{mod_path}"
            module_ids.append(mid)
            for fi in range(funcs_per_module):
                v = _VERBS[rng.randrange(len(_VERBS))]
                n1 = _NOUNS[rng.randrange(len(_NOUNS))]
                fid = f"{mid}::{v}_{n1}_{fi}"
                summ = (f"{v.capitalize()} the {domain} {n1} and "
                        f"{_VERBS[rng.randrange(len(_VERBS))]} its "
                        f"{_NOUNS[rng.randrange(len(_NOUNS))]} for the m{mi} step.")
                func_ids.append(fid)
                func_meta.append({"id": fid, "domain": domain, "module": mid,
                                  "lineno": 1 + fi * 8, "path": mod_path, "summary": summ})
                if len(func_ids) >= n_nodes:
                    break
            if len(func_ids) >= n_nodes:
                break
        di += 1

    hub_ids = sorted(set(hub_ids))
    module_ids = sorted(set(module_ids))
    written = 0

    def _w(bucket: str, fname: str, text: str) -> None:
        (nd / bucket / fname).write_text(text, encoding="utf-8")

    # 2. Hubs (with a short overview body — the few non-empty bodies, as in reality).
    for h in hub_ids:
        topic = h.split(":", 1)[1]
        _w("_hubs", f"{topic}.md", E._node(
            h, "hub", f"{topic} subsystem: modules and functions for the {topic} domain.",
            body=f"Overview of the {topic} subsystem and its responsibilities."))
        written += 1

    # 3. Modules (imports to the next module; part_of their domain hub).
    for i, mid in enumerate(module_ids):
        domain = mid.split("/")[1]
        nxt = module_ids[(i + 1) % len(module_ids)]
        _w("code", f"mod_{i}.md", E._node(
            mid, "module", f"Module {mid.split('/')[-1]} of the {domain} domain.",
            source_path=mid.split(":", 1)[1], lineno=1,
            edges=[{"rel": "imports", "to": nxt, "w": 0.6}],
            part_of=[{"topic": f"hub:{domain}", "w": 1.0}]))
        written += 1

    # 4. Functions (calls to a sibling + a depends_on chain to the next function).
    for i, fm in enumerate(func_meta):
        nxt = func_ids[(i + 1) % len(func_ids)]
        sib = func_ids[(i + 7) % len(func_ids)]
        _w("code", f"fn_{i}.md", E._node(
            fm["id"], "function", fm["summary"],
            source_path=fm["path"], lineno=fm["lineno"],
            edges=[{"rel": "calls", "to": sib, "w": 0.7},
                   {"rel": "depends_on", "to": nxt, "w": 0.5}],
            part_of=[{"topic": f"hub:{fm['domain']}", "w": 1.0}]))
        written += 1

    # 5. A doc section per domain documenting its first module (cross-domain edge).
    for i, h in enumerate(hub_ids):
        topic = h.split(":", 1)[1]
        target = next((m for m in module_ids if f"/{topic}/" in m), None)
        if target is None:
            continue
        _w("doc", f"doc_{i}.md", E._node(
            f"doc:doc/{topic}.md::overview", "section",
            f"How the {topic} subsystem is structured and used.",
            body=f"Guide to the {topic} subsystem.",
            edges=[{"rel": "documents", "to": target, "w": 0.9}]))
        written += 1

    # 6. A few eval cases: query a function summary; gold = it + 2 chain neighbors.
    cases: List[Dict[str, Any]] = []
    step = max(1, len(func_meta) // 6)
    for j in range(0, len(func_meta), step):
        if len(cases) >= 6:
            break
        fm = func_meta[j]
        gold = [func_ids[(j + k) % len(func_ids)] for k in range(3)]
        cases.append({"id": f"case-{j}", "query": " ".join(fm["summary"].split()[:8]),
                      "gold_ids": gold})
    (root / "cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
    return {"nodes_written": written, "functions": len(func_ids),
            "modules": len(module_ids), "hubs": len(hub_ids), "cases": len(cases)}


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def print_report(res: Dict[str, Any]) -> None:
    def fmt(x: Optional[float]) -> str:
        return "     n/a" if x is None else f"{x * 1000:8.1f} ms"
    sp = res.get("speedup")
    print(f"  nodes               : {res['n_nodes']}")
    print(f"  load: scan  (before): {fmt(res.get('scan_s'))}   full nodes/*.md parse")
    print(f"  load: index (after) : {fmt(res.get('index_read_s'))}   SQLite read"
          + ("" if sp is None else f"   -> {sp}x faster"))
    print(f"  index build/rebuild : {fmt(res.get('index_build_s'))}")
    print(f"  build_adjacency     : {fmt(res.get('build_adjacency_s'))}")
    print(f"  retrieve (per query): {fmt(res.get('retrieve_s_per_query'))}"
          f"   over {res.get('queries')} queries (warm index)")
    print(f"  eval (all cases)    : {fmt(res.get('eval_s'))}"
          f"   over {res.get('eval_cases')} cases")
    boot = res.get("bootstrap")
    if isinstance(boot, dict) and "bootstrap_s" in boot:
        print(f"  bootstrap (plan)    : {fmt(boot['bootstrap_s'])}"
              f"   added={boot.get('added')} queued={boot.get('queued')}")
    elif isinstance(boot, dict):
        print(f"  bootstrap           : skipped ({boot.get('skipped')})")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _arg(args: List[str], flag: str, default: Optional[str] = None) -> Optional[str]:
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default


def main(argv: List[str]) -> int:
    args = argv[1:]
    if not args or "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    repeats = int(_arg(args, "--repeats", "3") or 3)
    n_queries = int(_arg(args, "--queries", "8") or 8)
    project = _arg(args, "--project")
    project_root = Path(project).resolve() if project else None

    if "--make-bench" in args:
        root = Path(str(_arg(args, "--make-bench"))).resolve()
        n_nodes = int(_arg(args, "--nodes", "5000") or 5000)
        seed = int(_arg(args, "--seed", "0") or 0)
        root.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        built = make_bench_graph(root, n_nodes, seed)
        gen_s = time.perf_counter() - t0
        print(f"built synthetic graph at {root}  "
              f"({built['nodes_written']} nodes in {gen_s:.2f}s)\n")
        store = root
    elif "--store" in args:
        store = Path(str(_arg(args, "--store"))).resolve()
    else:
        print(__doc__)
        return 2

    res = bench_store(store, repeats=repeats, n_queries=n_queries,
                      project_root=project_root)
    print(f"AMG bench  (best of {repeats}, embeddings off)")
    print_report(res)
    out = _arg(args, "--out")
    if out:
        Path(out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
