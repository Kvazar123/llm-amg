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


if __name__ == "__main__":
    proj = setup_project()
    try:
        case_created_fields(proj)
        case_requeue_after_crash(proj)
        case_no_empty_overwrite(proj)
        case_derived_not_requeued(proj)
        case_pointer_drift(proj)
        case_changed_updates_pointer_keeps_summary(proj)
        print("\nALL RECONCILE CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
