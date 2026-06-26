#!/usr/bin/env python3
"""
selftest_queue.py — partition_queue + inspect_queue helpers (stage 12, tasks 5-6).

Checks the queue split groups units by subtree and round-trips them, and that the
inspect summary counts categories / kinds / pre-extracted text correctly.

Run:  python selftest_queue.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import inspect_queue as IQ
import partition_queue as PQ


def _make_queue(amg: Path) -> List[Dict[str, Any]]:
    (amg / "work").mkdir(parents=True)
    units: List[Dict[str, Any]] = [
        {"id": "code:src/billing/a.py::f", "kind": "function",
         "source_path": "src/billing/a.py", "category": "code"},
        {"id": "code:src/billing/b.py::g", "kind": "function",
         "source_path": "src/billing/b.py", "category": "code"},
        {"id": "code:src/auth/c.py::h", "kind": "function",
         "source_path": "src/auth/c.py", "category": "code"},
        {"id": "doc:doc/guide.md::intro", "kind": "section",
         "source_path": "doc/guide.md", "category": "doc"},
        {"id": "data:data/x.csv", "kind": "sheet",
         "source_path": "data/x.csv", "category": "data", "text": "table description"},
        {"id": "data:root.json::k", "kind": "record",
         "source_path": "root.json", "category": "data"},
    ]
    (amg / "work" / "queue.json").write_text(
        json.dumps({"generated": "t", "units": units}), encoding="utf-8")
    return units


def test_subtree_key() -> None:
    assert PQ.subtree_key("src/billing/a.py", 2) == "src/billing"
    assert PQ.subtree_key("src/a.py", 2) == "src"
    assert PQ.subtree_key("root.json", 2) == "_root"      # root file -> no directory
    assert PQ.subtree_key("src/billing/a.py", 1) == "src"
    print("PASS  subtree_key: depth grouping + root file")


def test_partition(tmp: Path) -> None:
    amg = tmp / "amg1"
    units = _make_queue(amg)
    counts = PQ.partition(amg, depth=2)
    assert counts["src/billing"] == 2 and counts["src/auth"] == 1, counts
    assert counts["_root"] == 1, counts                   # root.json
    assert sum(counts.values()) == len(units)
    # round-trip: every batch file holds the units that belong to its subtree
    total = 0
    for f in (amg / "work").glob("queue-*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        assert "part" in d and "units" in d, d
        total += len(d["units"])
        for u in d["units"]:
            assert PQ.subtree_key(u["source_path"], 2).replace("/", "_") == f.stem[len("queue-"):]
    assert total == len(units), "every unit must land in exactly one batch"
    print(f"PASS  partition: {len(counts)} batches by subtree, units round-trip")


def test_inspect(tmp: Path) -> None:
    amg = tmp / "amg2"
    units = _make_queue(amg)
    s = IQ.summarize(amg)
    assert s["total"] == len(units)
    assert s["by_category"]["code"] == 3 and s["by_category"]["doc"] == 1, s
    assert s["by_category"]["data"] == 2, s
    assert s["by_kind"]["function"] == 3, s
    assert s["with_text"] == 1, "only the CSV unit carries pre-extracted text"
    assert IQ.summarize(tmp / "nope") == {"queue": None}, "missing queue -> None"
    print("PASS  inspect: counts by category/kind, with_text, missing-queue -> None")


def test_priority(tmp: Path) -> None:
    """Lazy derivation (Stage 17): the priority split derives the structural MAP first
    (module/class/package/file) and defers leaf detail; a used node (usage.log) is
    promoted into the priority batch on the background pass."""
    amg = tmp / "amg3"
    (amg / "work").mkdir(parents=True)
    units = [
        {"id": "code:src/m.py", "kind": "module", "source_path": "src/m.py", "category": "code"},
        {"id": "code:src/m.py::C", "kind": "class", "source_path": "src/m.py", "category": "code"},
        {"id": "code:src/m.py::f", "kind": "function", "source_path": "src/m.py", "category": "code"},
        {"id": "doc:doc/g.md::s", "kind": "section", "source_path": "doc/g.md", "category": "doc"},
    ]
    (amg / "work" / "queue.json").write_text(
        json.dumps({"generated": "t", "units": units}), encoding="utf-8")

    # no usage: structural map (module/class) is priority, leaf detail (function/section) deferred
    counts = PQ.priority_split(amg, use_usage=False)
    assert counts == {"priority": 2, "deferred": 2}, counts
    pri = json.loads((amg / "work" / "queue-priority.json").read_text(encoding="utf-8"))
    dfr = json.loads((amg / "work" / "queue-deferred.json").read_text(encoding="utf-8"))
    assert {u["id"] for u in pri["units"]} == {"code:src/m.py", "code:src/m.py::C"}, pri
    assert {u["id"] for u in dfr["units"]} == {"code:src/m.py::f", "doc:doc/g.md::s"}, dfr

    # phase C: a USED leaf (usage.log) is promoted into the priority batch
    (amg / "work" / "usage.log").write_text(
        json.dumps({"used": ["code:src/m.py::f"], "outcome": "completed"}) + "\n", encoding="utf-8")
    counts2 = PQ.priority_split(amg, use_usage=True)
    assert counts2 == {"priority": 3, "deferred": 1}, counts2
    pri2 = json.loads((amg / "work" / "queue-priority.json").read_text(encoding="utf-8"))
    assert "code:src/m.py::f" in {u["id"] for u in pri2["units"]}, "used leaf must be promoted"
    print("PASS  priority_split: map first, leaf deferred; usage.log promotes a used node")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    tmp = Path(tempfile.mkdtemp(prefix="amg-queue-test-"))
    try:
        test_subtree_key()
        test_partition(tmp)
        test_inspect(tmp)
        test_priority(tmp)
        print("\nALL QUEUE-HELPER CHECKS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
