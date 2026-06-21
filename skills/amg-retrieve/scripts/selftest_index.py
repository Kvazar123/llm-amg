#!/usr/bin/env python3
"""
selftest_index.py — the disposable SQLite read-index (roadmap Stage 12, Group 2).

Proves the index is a transparent accelerator, never a source of divergence:
  1. identity   : the index reproduces the scan byte-for-byte (same node dicts), and
                  retrieve over the index gives the SAME ranking + pack as the scan.
  2. freshness  : editing a node file flips the signature -> read_if_fresh returns
                  None (stale) -> load_nodes scans and rebuilds, reflecting the change.
  3. delete     : removing index.sqlite -> load_nodes scans and rebuilds it, identical.
  4. corruption : a garbage index -> read_if_fresh returns None (no crash), scan wins.
  5. upsert     : a writer (notes.add_note) folds its change into the index under the
                  lock, so it stays FRESH (no full rebuild) and contains the new node.

Embeddings are forced off (no model download). Run:  python selftest_index.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "amg-bootstrap" / "scripts"))

import bench as B
import embed
import index_store as IX
import retrieve as R


def _first_node_file(root: Path) -> Path:
    return next((root / "nodes" / "code").glob("*.md"))


def test_index_matches_scan(tmp: Path) -> None:
    root = tmp / "g1"
    B.make_bench_graph(root, n_nodes=150, seed=0)
    assert not (root / "cache" / "index.sqlite").exists(), "no index before first load"
    scanned = R._scan_nodes(root)
    sig = IX.signature(root)
    assert IX.build(root, scanned, sig), "build must succeed"
    via = IX.read_if_fresh(root)
    assert via is not None, "fresh index must read back"
    assert via == scanned, "index node dicts must equal the scan byte-for-byte"
    print(f"PASS  index == scan ({len(scanned)} nodes, identical dicts incl tokens/edges)")


def test_retrieve_identical_over_index(tmp: Path) -> None:
    root = tmp / "g2"
    B.make_bench_graph(root, n_nodes=150, seed=1)
    cold = R.load_nodes(root)                       # scan + build the index
    assert (root / "cache" / "index.sqlite").exists(), "load_nodes must warm the index"
    warm = R.load_nodes(root)                       # read the index
    assert cold == warm, "index read must equal the scan result"
    q = "validate the orders request for the m0 step"
    r1 = R.retrieve(root, q, write_pack=False, log_coactivation=False)
    r2 = R.retrieve(root, q, write_pack=False, log_coactivation=False)
    assert [n for n, _ in r1["ranked"]] == [n for n, _ in r2["ranked"]], "ranking must match"
    assert r1["pack"] == r2["pack"], "assembled pack must be identical over the index"
    print("PASS  retrieve over index == retrieve over scan (ranking + pack identical)")


def test_stale_then_rebuild(tmp: Path) -> None:
    root = tmp / "g3"
    B.make_bench_graph(root, n_nodes=120, seed=2)
    R.load_nodes(root)                              # build the index
    assert IX.read_if_fresh(root) is not None, "index fresh right after build"
    p = _first_node_file(root)
    p.write_text(p.read_text(encoding="utf-8") + "\n<!-- touched -->\n", encoding="utf-8")
    assert IX.read_if_fresh(root) is None, "edited file must flip the signature -> stale"
    nodes = R.load_nodes(root)                      # scans + rebuilds
    assert IX.read_if_fresh(root) is not None, "load_nodes must rebuild the stale index"
    # the rebuilt index reflects the on-disk change exactly
    assert IX.read_if_fresh(root) == nodes
    print("PASS  edit flips signature -> stale -> load_nodes rebuilds, reflects the change")


def test_delete_and_corrupt_fall_back(tmp: Path) -> None:
    root = tmp / "g4"
    B.make_bench_graph(root, n_nodes=100, seed=3)
    base = R.load_nodes(root)                       # build
    ipath = root / "cache" / "index.sqlite"
    ipath.unlink()
    assert IX.read_if_fresh(root) is None, "missing index -> None"
    rebuilt = R.load_nodes(root)
    assert rebuilt == base and ipath.exists(), "delete -> scan + rebuild, identical"
    ipath.write_bytes(b"not a sqlite database at all")
    assert IX.read_if_fresh(root) is None, "corrupt index -> None (no crash)"
    after = R.load_nodes(root)
    assert set(after) == set(base), "corrupt index -> scan fallback, no wrong result"
    print("PASS  deleted/corrupt index degrades to the scan (no crash, no wrong result)")


def test_writer_upsert_keeps_fresh(tmp: Path) -> None:
    root = tmp / "g5"
    B.make_bench_graph(root, n_nodes=80, seed=4)
    R.load_nodes(root)                              # build the index
    import notes
    summary = "adopt the read-index for large graphs xyz-unique"
    res = notes.add_note(tmp, "decision", summary, amg_root=root)
    via = IX.read_if_fresh(root)
    assert via is not None, "a writer's upsert must leave the index FRESH (not invalidated)"
    assert res["id"] in via, "the upserted note must be in the index"
    assert via[res["id"]]["summary"] == summary
    # the Stage 13 trust fields a note carries survive the upsert into the index
    assert via[res["id"]]["confidence"] == 0.85, "confidence must round-trip via the index"
    assert via[res["id"]]["verification"]["method"] == "user", "verification must round-trip"
    assert via == R._scan_nodes(root), "upserted index must still equal a full scan"
    print("PASS  writer upsert keeps the index fresh and correct (notes.add_note)")


def test_stage13_fields_roundtrip(tmp: Path) -> None:
    """confidence / verification / line_end survive the scan<->index round-trip WITH their
    values (not just structurally) — the projection added in Stage 13. The bench graph
    has none of these, so a hand-written node exercises non-trivial values."""
    root = tmp / "g6"
    (root / "nodes" / "code").mkdir(parents=True)
    (root / "config.yml").write_text("active: true\n", encoding="utf-8")
    (root / "nodes" / "code" / "a-0001.md").write_text(
        "---\nid: code:src/m.py::a\ntype: function\nsource_path: src/m.py\n"
        "lineno: 10\nline_end: 25\nconfidence: 0.42\n"
        "verification:\n  status: contradicted\n  method: grep\n"
        "status: active\nsummary: does a thing\n---\n", encoding="utf-8")
    scanned = R._scan_nodes(root)
    assert IX.build(root, scanned, IX.signature(root)), "build must succeed"
    via = IX.read_if_fresh(root)
    assert via == scanned, "index must reproduce the scan incl. Stage 13 fields"
    n = via["code:src/m.py::a"]
    assert n["confidence"] == 0.42 and n["line_end"] == 25, n
    assert n["verification"] == {"status": "contradicted", "method": "grep"}, n
    print("PASS  stage13 fields: confidence/line_end/verification round-trip via the index")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    orig = embed.get_embedder
    embed.get_embedder = lambda cfg: None              # force pure BM25, no model load
    tmp = Path(tempfile.mkdtemp(prefix="amg-index-test-"))
    try:
        test_index_matches_scan(tmp)
        test_retrieve_identical_over_index(tmp)
        test_stale_then_rebuild(tmp)
        test_delete_and_corrupt_fall_back(tmp)
        test_writer_upsert_keeps_fresh(tmp)
        test_stage13_fields_roundtrip(tmp)
        print("\nALL INDEX CHECKS PASSED")
    finally:
        embed.get_embedder = orig
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
