#!/usr/bin/env python3
"""
selftest_bench.py — smoke test for bench.py.

It is a benchmark tool, so there is no "correct timing" to assert; instead this
proves the bench does not bitrot: the synthetic generator produces a graph the
engine can load, bench_store returns the expected timing keys, and the measurement
stays READ-ONLY (no pack / co-activation written) — the property the eval gate and
the upcoming index both rely on.

Run:  python selftest_bench.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench as B
import embed
import retrieve as R


def test_make_graph_is_loadable(tmp: Path) -> None:
    root = tmp / "g"
    built = B.make_bench_graph(root, n_nodes=200, seed=0)
    assert built["functions"] == 200, f"generator must hit the node target: {built}"
    nodes = R.load_nodes(root)
    assert len(nodes) == built["nodes_written"], "every written node must load"
    # hubs, modules and a depends_on chain exist -> adjacency is non-trivial.
    adj = R.build_adjacency(nodes, B._embeddings_off(R.load_config(root)))
    assert any(adj.values()), "synthetic graph must have edges (adjacency non-empty)"
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    assert cases and all(c.get("gold_ids") for c in cases), "cases must carry gold ids"
    print(f"PASS  generator: {built['nodes_written']} loadable nodes, "
          f"{len(cases)} cases, non-empty adjacency")


def test_bench_keys_and_determinism(tmp: Path) -> None:
    root = tmp / "g2"
    B.make_bench_graph(root, n_nodes=150, seed=1)
    res = B.bench_store(root, repeats=1, n_queries=3)
    for k in ("scan_s", "index_read_s", "index_build_s", "build_adjacency_s",
              "retrieve_s_per_query", "eval_s"):
        assert isinstance(res[k], float), f"{k} must be a float second count, got {res[k]!r}"
    # speedup direction is NOT asserted: the index wins from ~dozens of nodes (measured:
    # ~13x at 52, ~15x at 1900), but on this small fixture the absolute times are a few ms,
    # so a single timed run is noise-prone on a loaded CI box — bench just reports both.
    assert res["n_nodes"] == len(R.load_nodes(root)), "n_nodes must match the graph"
    assert res["eval_cases"] >= 1 and res["queries"] >= 1
    # the generator is deterministic: same (nodes, seed) -> same node set.
    root2 = tmp / "g2b"
    B.make_bench_graph(root2, n_nodes=150, seed=1)
    assert set(R.load_nodes(root)) == set(R.load_nodes(root2)), "generator must be deterministic"
    print("PASS  bench_store returns timing keys; generator deterministic by seed")


def test_bench_is_read_only(tmp: Path) -> None:
    root = tmp / "g3"
    B.make_bench_graph(root, n_nodes=120, seed=2)
    B.bench_store(root, repeats=1, n_queries=2)
    assert not (root / "cache" / "pack.md").exists(), "bench must not write a pack"
    assert not (root / "work" / "coactivation.log").exists(), \
        "bench must not write the co-activation log (would pollute the Hebbian signal)"
    print("PASS  bench is read-only (no pack, no co-activation log)")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    orig = embed.get_embedder
    embed.get_embedder = lambda cfg: None              # force pure BM25, no model load
    tmp = Path(tempfile.mkdtemp(prefix="amg-bench-test-"))
    try:
        test_make_graph_is_loadable(tmp)
        test_bench_keys_and_determinism(tmp)
        test_bench_is_read_only(tmp)
        print("\nALL BENCH CHECKS PASSED")
    finally:
        embed.get_embedder = orig
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
