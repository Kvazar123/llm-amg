#!/usr/bin/env python3
"""
selftest_verify.py — verify_claims.py: lightweight verification against live source (Stage 13).

Checks:
  1. verified    : an unchanged source-derived node verifies against its source (python
                   -> method ast).
  2. stale       : editing the source WITHOUT re-bootstrap -> the node's stored
                   source_hash no longer matches the re-chunk -> stale (drift caught
                   before reconcile even sees it; the unique value of a live check).
  3. symbol gone : removing the function from the source -> contradicted.
  4. file gone   : deleting the source file -> contradicted.
  5. skipped     : an authored note has no backing file -> skipped.
  6. write       : --write persists the verdict into the node's verification block
                   (status/method/last_verified_at); the default read-only run does not.

Run:  python selftest_verify.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "amg-bootstrap" / "scripts"))

import graph_store as gs          # noqa: E402
import reconcile as rc            # noqa: E402
import notes as NT                # noqa: E402
import verify_claims as VC        # noqa: E402

PY = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
ALPHA = "code:src/m.py::alpha"
BETA = "code:src/m.py::beta"


def amg_of(proj: Path) -> Path:
    return proj / ".claude" / "amg"


def setup() -> Path:
    """A tiny mirror graph: bootstrap + derive so the nodes are active and source-derived
    (verification starts `unverified` at ingest)."""
    proj = Path(tempfile.mkdtemp(prefix="amg-verify-"))
    amg = amg_of(proj)
    amg.mkdir(parents=True)
    (amg / "config.yml").write_text(
        "active: true\nworking_language: en\nmirror_path: src\n", encoding="utf-8")
    (proj / "src").mkdir()
    (proj / "src" / "m.py").write_text(PY, encoding="utf-8")
    rc.plan(proj, amg)
    work = amg / "work"
    work.mkdir(exist_ok=True)
    nodes = rc.load_nodes(gs.GraphStore(amg))
    items = [{"id": nid, "summary": f"S {nid}", "content_sha": n["source_hash"]}
             for nid, n in nodes.items() if n.get("source_kind") == "derived_from_file"]
    (work / "d.json").write_text(json.dumps(items), encoding="utf-8")
    rc.apply_derivation(proj, work / "d.json", amg)
    return proj


def case_verified(proj: Path) -> None:
    r = VC.verify(amg_of(proj), proj, scope="code")["results"]
    assert r[ALPHA]["status"] == "verified" and r[BETA]["status"] == "verified", r
    assert r[ALPHA]["method"] == "ast", r            # python is parsed by ast
    print("PASS  verified: unchanged source-derived nodes verify against source")


def case_stale(proj: Path) -> None:
    (proj / "src" / "m.py").write_text(
        "def alpha():\n    return 111  # edited\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8")
    r = VC.verify(amg_of(proj), proj, ids=[ALPHA, BETA])["results"]
    assert r[ALPHA]["status"] == "stale", r          # body changed since the summary
    assert r[BETA]["status"] == "verified", r        # untouched sibling still verifies
    print("PASS  stale: an edited source (pre-reconcile) is caught as stale")


def case_symbol_gone(proj: Path) -> None:
    (proj / "src" / "m.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    r = VC.verify(amg_of(proj), proj, ids=[ALPHA])["results"]
    assert r[ALPHA]["status"] == "contradicted", r
    print("PASS  contradicted: a removed symbol is contradicted")


def case_file_gone(proj: Path) -> None:
    (proj / "src" / "m.py").unlink()
    r = VC.verify(amg_of(proj), proj, ids=[BETA])["results"]
    assert r[BETA]["status"] == "contradicted", r
    print("PASS  contradicted: a missing source file is contradicted")


def case_authored_skipped(proj: Path) -> None:
    NT.add_note(proj, "decision", "use ast for python verification",
                node_id="note:v-dec", amg_root=amg_of(proj))
    r = VC.verify(amg_of(proj), proj, ids=["note:v-dec"])["results"]
    assert r["note:v-dec"]["status"] == "skipped", r   # no backing file
    print("PASS  skipped: an authored node has no backing file")


def case_write_persists() -> None:
    """--write stamps the verification block; the default read-only run touches nothing."""
    proj = setup()
    try:
        (proj / "src" / "m.py").write_text(
            "def alpha():\n    return 9  # e\n\n\ndef beta():\n    return 2\n", encoding="utf-8")
        before = rc.load_nodes(gs.GraphStore(amg_of(proj)))[ALPHA]["verification"]
        assert before["status"] == "unverified", before

        VC.verify(amg_of(proj), proj, ids=[ALPHA], write=False)    # read-only: no change
        mid = rc.load_nodes(gs.GraphStore(amg_of(proj)))[ALPHA]["verification"]
        assert mid["status"] == "unverified", "read-only run must not write to the graph"

        res = VC.verify(amg_of(proj), proj, ids=[ALPHA], write=True)
        assert res["written"] == 1, res
        after = rc.load_nodes(gs.GraphStore(amg_of(proj)))[ALPHA]["verification"]
        assert after["status"] == "stale" and after["method"] == "ast", after
        assert after.get("last_verified_at"), "write must stamp last_verified_at"
        print("PASS  write: --write persists the verdict; read-only leaves it untouched")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    proj = setup()
    try:
        case_verified(proj)
        case_stale(proj)
        case_symbol_gone(proj)
        case_file_gone(proj)
        case_authored_skipped(proj)
    finally:
        shutil.rmtree(proj, ignore_errors=True)
    case_write_persists()
    print("\nALL VERIFY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
