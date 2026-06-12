#!/usr/bin/env python3
"""
selftest_reconcile.py — proves the Stage 0 reconcile guarantees (tasks 1–2).

Checks:
  1. fields   : created nodes carry lineno/qualname in frontmatter (lang stays the
                summary working language); queue items carry qualname/lineno/lang
                (lang = SOURCE language) for the builder.
  2. requeue  : a node whose derivation lags (derived_from_hash != source_hash or
                status == stale) is re-queued by EVERY plan() run, so a crash
                between the node transaction and the queue write self-heals — and
                a repeated plan() never overwrites the queue with an empty one.
  3. hash gate: derived nodes are NOT re-queued (re-runs stay free).
  4. drift    : an edit above a unit shifts it without changing its content hash;
                lineno is refreshed quietly, without re-derivation.
  5. changed  : a content change updates lineno/qualname and keeps the earned
                summary (status flips to stale until re-derived).
  6. edges    : changed re-extracts structural edges (a new call appears, a
                dropped call disappears), a persisting edge inherits its earned
                w/coact, legacy edges without `origin` are treated as structural
                when their rel is imports/calls, and semantic edges survive.
  7. origin   : edges are stamped structural at extraction, semantic at apply
                (update items), synthesized on created hub nodes.

Run:  python selftest_reconcile.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import graph_store as gs
import reconcile as RC

PY_SRC = """import json


def top(a):
    return json.dumps(a)


class Box:
    def get(self):
        return top(1)
"""

MD_SRC = """# Guide

Intro text.

## Routing

How requests reach controllers.
"""

MODULE = "code:src/app.py"
TOP = "code:src/app.py::top"
BOX = "code:src/app.py::Box"
GET = "code:src/app.py::Box.get"
HELPER = "code:src/app.py::helper"
GUIDE = "doc:src/guide.md::guide"
ROUTING = "doc:src/guide.md::routing"
ALL_IDS = {MODULE, TOP, BOX, GET, GUIDE, ROUTING}


def setup_project() -> Path:
    proj = Path(tempfile.mkdtemp(prefix="amg-rec-"))
    amg = proj / ".claude" / "amg"
    amg.mkdir(parents=True)
    (amg / "config.yml").write_text(
        "active: true\nworking_language: ru\nmirror_path: src\n", encoding="utf-8")
    src = proj / "src"
    src.mkdir()
    (src / "app.py").write_text(PY_SRC, encoding="utf-8")
    (src / "guide.md").write_text(MD_SRC, encoding="utf-8")
    return proj


def queue_items(proj: Path) -> dict:
    qpath = proj / ".claude" / "amg" / "work" / "queue.json"
    q = json.loads(qpath.read_text(encoding="utf-8"))
    return {u["id"]: u for u in q["units"]}


def graph_nodes(proj: Path) -> dict:
    return RC.load_nodes(gs.GraphStore(proj / ".claude" / "amg"))


def derive_all(proj: Path, ids) -> None:
    work = proj / ".claude" / "amg" / "work"
    work.mkdir(exist_ok=True)
    items = [{"id": uid, "summary": f"S {uid}"} for uid in ids]
    out = work / "derived-test.json"
    out.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    RC.apply_derivation(proj, out)


def case_created_fields(proj: Path) -> None:
    s = RC.plan(proj)
    assert s["added"] == 6 and s["queued_for_semantic"] == 6, s

    q = queue_items(proj)
    assert set(q) == ALL_IDS, set(q)
    t = q[TOP]
    assert (t["qualname"], t["lineno"], t["lang"]) == ("top", 4, "python"), t
    r = q[ROUTING]
    assert (r["qualname"], r["lineno"], r["lang"]) == ("routing", 5, "markdown"), r

    n = graph_nodes(proj)[TOP]
    assert n["qualname"] == "top" and n["lineno"] == 4, n
    assert n["lang"] == "ru", "node lang must stay the summary working language"
    assert n["status"] == "stale" and n["derived_from_hash"] is None
    print("PASS  created: lineno/qualname in frontmatter; qualname/lineno/lang in queue")


def case_requeue_after_crash(proj: Path) -> None:
    # Simulate the 1.1 crash: nodes committed, queue.json never written (or lost).
    (proj / ".claude" / "amg" / "work" / "queue.json").unlink()
    s = RC.plan(proj)
    assert s["added"] == 0 and s["requeued_stale"] == 6, s
    assert set(queue_items(proj)) == ALL_IDS, "queue must be rebuilt from graph state"
    print("PASS  requeue: lost queue.json is rebuilt by the next bootstrap")


def case_no_empty_overwrite(proj: Path) -> None:
    # The 1.1 regression: a repeated plan() used to rewrite the queue EMPTY.
    s = RC.plan(proj)
    assert s["requeued_stale"] == 6, s
    assert set(queue_items(proj)) == ALL_IDS, "repeated plan() must not empty the queue"
    print("PASS  requeue: repeated plan() does not overwrite the queue with empty")


def case_derived_not_requeued(proj: Path) -> None:
    derive_all(proj, ALL_IDS)
    s = RC.plan(proj)
    assert s["unchanged"] == 6 and s["requeued_stale"] == 0, s
    assert queue_items(proj) == {}, "derived nodes must not be re-queued"
    assert graph_nodes(proj)[TOP]["status"] == "active"
    print("PASS  hash gate: derived nodes are unchanged, queue is empty")


def case_pointer_drift(proj: Path) -> None:
    app = proj / "src" / "app.py"
    app.write_text("import os\n" + app.read_text(encoding="utf-8"), encoding="utf-8")
    s = RC.plan(proj)
    # module content changed; top/Box/Box.get only shifted (same hash, new lineno)
    assert s["changed"] == 1 and s["pointer_refreshed"] == 3, s
    assert s["requeued_stale"] == 0 and s["unchanged"] == 2, s
    assert set(queue_items(proj)) == {MODULE}, "shifted units must not be re-queued"
    n = graph_nodes(proj)[TOP]
    assert n["lineno"] == 5, n["lineno"]
    assert n["status"] == "active" and n["summary"] == f"S {TOP}", \
        "drift refresh must not touch status or summary"
    print("PASS  drift: shift without content change refreshes lineno only")


def case_changed_updates_pointer_keeps_summary(proj: Path) -> None:
    app = proj / "src" / "app.py"
    app.write_text(app.read_text(encoding="utf-8").replace(
        "return json.dumps(a)", "return json.dumps(a, sort_keys=True)"),
        encoding="utf-8")
    s = RC.plan(proj)
    assert s["changed"] == 2, s                      # module + top (module is stale too)
    assert set(queue_items(proj)) == {MODULE, TOP}, set(queue_items(proj))
    n = graph_nodes(proj)[TOP]
    assert n["lineno"] == 5 and n["qualname"] == "top", n
    assert n["status"] == "stale" and n["summary"] == f"S {TOP}", \
        "changed must keep the earned summary until re-derived"
    print("PASS  changed: pointer fields updated, earned summary kept (stale)")


def case_structural_edges_on_change(proj: Path) -> None:
    derive_all(proj, [MODULE, TOP])                   # clean slate: everything active

    # A semantic edge earned by the judgment layer must survive re-extraction.
    work = proj / ".claude" / "amg" / "work"
    out = work / "derived-sem.json"
    out.write_text(json.dumps([{"id": GET, "edges": [
        {"rel": "relates_to", "to": ROUTING, "w": 0.4}]}]), encoding="utf-8")
    RC.apply_derivation(proj, out)
    n = graph_nodes(proj)[GET]
    sem = [e for e in n["edges"] if e["rel"] == "relates_to"]
    assert sem and sem[0].get("origin") == "semantic", n["edges"]

    # Simulate an EARNED structural edge (Hebbian w/coact) in the legacy form
    # without `origin` — the refresh must treat it as structural and re-stamp it.
    store = gs.GraphStore(proj / ".claude" / "amg")
    n = RC.load_nodes(store)[GET]
    path, body = n.pop("_path"), n.pop("_body", "")
    for e in n["edges"]:
        if e["rel"] == "calls":
            e["w"], e["coact"] = 0.9, 5
            e.pop("origin", None)
    gs.atomic_write_text(store.abspath(path), RC.serialize_node(n, body))

    # Box.get now ALSO calls helper (a new function appended to the file).
    app = proj / "src" / "app.py"
    app.write_text(app.read_text(encoding="utf-8").replace(
        "        return top(1)", "        return helper(top(1))")
        + "\n\ndef helper(x):\n    return x\n", encoding="utf-8")
    s = RC.plan(proj)
    assert s["added"] == 1 and s["changed"] == 3, s   # helper; module + Box + Box.get

    edges = {(e["rel"], e["to"]): e for e in graph_nodes(proj)[GET]["edges"]}
    kept = edges[("calls", TOP)]
    assert kept["w"] == 0.9 and kept["coact"] == 5, kept       # earned signal inherited
    assert kept.get("origin") == "structural", kept            # legacy edge re-stamped
    assert edges[("calls", HELPER)].get("origin") == "structural", edges
    assert ("relates_to", ROUTING) in edges, edges             # semantic edge survived
    print("PASS  edges: changed re-extracts structural (earned w/coact kept), semantic kept")


def case_structural_edge_removed(proj: Path) -> None:
    app = proj / "src" / "app.py"
    app.write_text(app.read_text(encoding="utf-8").replace(
        "        return helper(top(1))", "        return helper(1)"), encoding="utf-8")
    RC.plan(proj)
    edges = {(e["rel"], e["to"]) for e in graph_nodes(proj)[GET]["edges"]}
    assert ("calls", TOP) not in edges, edges          # dropped call loses its edge
    assert ("calls", HELPER) in edges
    assert ("relates_to", ROUTING) in edges
    print("PASS  edges: a dropped call removes its structural edge")


def case_origin_stamps(proj: Path) -> None:
    imports = [e for e in graph_nodes(proj)[MODULE]["edges"] if e["rel"] == "imports"]
    assert imports and all(e.get("origin") == "structural" for e in imports), imports

    work = proj / ".claude" / "amg" / "work"
    out = work / "derived-hub.json"
    out.write_text(json.dumps([{"id": "hub:test", "type": "hub", "summary": "T",
                                "edges": [{"rel": "relates_to", "to": MODULE, "w": 0.5}]}]),
                   encoding="utf-8")
    RC.apply_derivation(proj, out)
    h = graph_nodes(proj)["hub:test"]
    assert h["edges"][0].get("origin") == "synthesized", h["edges"]
    print("PASS  origin: structural at extraction, semantic at apply, synthesized on hubs")


if __name__ == "__main__":
    proj = setup_project()
    try:
        case_created_fields(proj)
        case_requeue_after_crash(proj)
        case_no_empty_overwrite(proj)
        case_derived_not_requeued(proj)
        case_pointer_drift(proj)
        case_changed_updates_pointer_keeps_summary(proj)
        case_structural_edges_on_change(proj)
        case_structural_edge_removed(proj)
        case_origin_stamps(proj)
        print("\nALL RECONCILE CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
