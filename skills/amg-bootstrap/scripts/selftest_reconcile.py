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
  8. part_of  : multiple derivation items on one node accumulate memberships;
                the same topic takes the newest weight; an over-simplex sum is
                renormalized (part_of_renormalize).
  9. gating   : an edges-/part_of-only item leaves a derived_from_file node
                stale (and re-queued); only a new summary flips it active.
 10. imports  : an in-project import resolves to the module node id (the
                dotted-name map); stdlib imports stay dangling dotted names.
 11. moved    : a file move migrates earned fields (summary, semantic edges,
                derived_from_hash) onto the new ids, rewrites same-file edge
                targets and the primary membership, redirects inbound
                references, and re-derives nothing for current nodes.
 12. fences   : a '# ...' line inside a markdown code fence is code, not a
                heading — it must not create a section.
 13. pack     : the retrieval pack renders operational code pointers as
                path:lineno, never path:None.
 14. root     : the store root resolves per the 4.9 chain — explicit root,
                AMG_AGENT_DIR, config search upward, the default .claude.

Run:  python selftest_reconcile.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import extract_structure as ES
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
    # a legacy grammar kind must converge to the extraction kind on drift
    box = graph_nodes(proj)[BOX]
    meta = {k: v for k, v in box.items() if not k.startswith("_")}
    meta["type"] = "class_declaration"
    gs.atomic_write_text(proj / ".claude" / "amg" / box["_path"],
                         RC.serialize_node(meta, ""))
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
    assert graph_nodes(proj)[BOX]["type"] == "class", "type must follow extraction"
    print("PASS  drift: shift refreshes lineno only; type follows extraction")


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


def case_multi_item_part_of(proj: Path) -> None:
    work = proj / ".claude" / "amg" / "work"
    out = work / "derived-po.json"
    # A create item plus two update items on the SAME node in one derivation:
    # memberships must accumulate, not overwrite each other (audit 1.6).
    out.write_text(json.dumps([
        {"id": "hub:po", "type": "hub", "summary": "membership testbed"},
        {"id": "hub:po", "part_of": [{"topic": "hub:a", "w": 0.6}]},
        {"id": "hub:po", "part_of": [{"topic": "hub:b", "w": 0.3}]},
    ]), encoding="utf-8")
    RC.apply_derivation(proj, out)
    po = {p["topic"]: p["w"] for p in graph_nodes(proj)["hub:po"]["part_of"]}
    assert po == {"hub:a": 0.6, "hub:b": 0.3}, po

    # Same topic again -> the newest weight wins; the other membership stays.
    out.write_text(json.dumps([{"id": "hub:po",
                                "part_of": [{"topic": "hub:a", "w": 0.2}]}]),
                   encoding="utf-8")
    RC.apply_derivation(proj, out)
    po = {p["topic"]: p["w"] for p in graph_nodes(proj)["hub:po"]["part_of"]}
    assert po == {"hub:a": 0.2, "hub:b": 0.3}, po

    # Over-simplex sum (0.9 + 0.3) is scaled back, ratios preserved.
    out.write_text(json.dumps([{"id": "hub:po",
                                "part_of": [{"topic": "hub:a", "w": 0.9}]}]),
                   encoding="utf-8")
    RC.apply_derivation(proj, out)
    po = {p["topic"]: p["w"] for p in graph_nodes(proj)["hub:po"]["part_of"]}
    assert abs(sum(po.values()) - 1.0) < 0.001, po
    assert abs(po["hub:a"] / po["hub:b"] - 3.0) < 0.01, po
    print("PASS  part_of: items accumulate, same topic updates, simplex renormalized")


def case_active_gating(proj: Path) -> None:
    n = graph_nodes(proj)[GET]
    assert n["status"] == "stale", "precondition: GET is stale after the edits above"

    work = proj / ".claude" / "amg" / "work"
    out = work / "derived-gate.json"
    out.write_text(json.dumps([{"id": GET, "edges": [
        {"rel": "relates_to", "to": GUIDE, "w": 0.3}]}]), encoding="utf-8")
    RC.apply_derivation(proj, out)
    n = graph_nodes(proj)[GET]
    assert n["status"] == "stale", "edges-only item must not flip a node active"
    assert n["derived_from_hash"] != n["source_hash"], n
    RC.plan(proj)
    assert GET in queue_items(proj), "still under-derived -> must stay queued"

    out.write_text(json.dumps([{"id": GET, "summary": "S get"}]), encoding="utf-8")
    RC.apply_derivation(proj, out)
    n = graph_nodes(proj)[GET]
    assert n["status"] == "active" and n["derived_from_hash"] == n["source_hash"], n
    print("PASS  gating: edges-only keeps stale and queued; a summary flips active")


def case_imports_resolver(proj: Path) -> None:
    (proj / "src" / "util.py").write_text("def helper2(x):\n    return x\n",
                                          encoding="utf-8")
    app = proj / "src" / "app.py"
    app.write_text("import util\n" + app.read_text(encoding="utf-8"), encoding="utf-8")
    s = RC.plan(proj)
    assert s["added"] == 2, s                      # util module + helper2
    imports = {e["to"]: e for e in graph_nodes(proj)[MODULE]["edges"]
               if e["rel"] == "imports"}
    assert "code:src/util.py" in imports, imports  # in-project import resolved
    assert imports["code:src/util.py"].get("origin") == "structural", imports
    assert "code:json" in imports, imports         # stdlib stays a dangling name
    print("PASS  imports: in-project import resolves to the module node id")


def case_move_migrates_earned(proj: Path) -> None:
    (proj / "src" / "core").mkdir()
    (proj / "src" / "app.py").rename(proj / "src" / "core" / "app.py")
    s = RC.plan(proj)
    assert s["moved"] == 5 and s["added"] == 0 and s["deleted"] == 0, s

    nodes = graph_nodes(proj)
    new_top = "code:src/core/app.py::top"
    new_get = "code:src/core/app.py::Box.get"
    assert TOP not in nodes and GET not in nodes, "old ids must be gone"
    t = nodes[new_top]
    assert t["summary"] == f"S {TOP}" and t["status"] == "active", t
    assert t["source_path"] == "src/core/app.py" and t["part_of"][0]["topic"] == "src/core", t

    tos = {(e["rel"], e["to"]): e for e in nodes[new_get]["edges"]}
    assert ("calls", "code:src/core/app.py::helper") in tos, tos   # same-file target rewritten
    assert ("relates_to", ROUTING) in tos and ("relates_to", GUIDE) in tos, tos
    assert ("imports", "code:src/util.py") in {(e["rel"], e["to"])
            for e in nodes["code:src/core/app.py"]["edges"]}       # resolver after move

    hub = nodes["hub:test"]
    assert any(e.get("to") == "code:src/core/app.py" for e in hub["edges"]), \
        "inbound edge must be redirected to the moved id"

    q = queue_items(proj)
    assert new_top not in q and new_get not in q, \
        "a pure move of derived nodes must not cost model calls"
    print("PASS  moved: earned fields migrate, references redirect, no re-derivation")


def case_markdown_fences() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="amg-md-"))
    try:
        md = tmp / "doc.md"
        md.write_text(
            "# Guide\n\n"
            "```bash\n# install deps\npip install x\n```\n\n"
            "~~~text\n# tilde heading\n```\n# nested backticks\n```\n~~~\n\n"
            "## Real\n\ndone\n",
            encoding="utf-8")
        quals = [u["qualname"] for u in ES._markdown_units(md, "doc.md", "mirror")]
        assert quals == ["guide", "real"], quals
        print("PASS  fences: headings inside code fences do not create sections")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_lineno_in_pack(proj: Path) -> None:
    sys.path.insert(0, str(HERE.parents[1] / "amg-retrieve" / "scripts"))
    import retrieve as RT
    store = proj / ".claude" / "amg"
    res = RT.retrieve(store, "top code src app", write_pack=False, log_coactivation=False)
    node = RT.load_nodes(store)["code:src/core/app.py::top"]
    loc = f"{node['source_path']}:{node['lineno']}"
    assert ":None" not in res["pack"], res["pack"]
    assert loc in res["pack"], (loc, res["pack"])
    print(f"PASS  pack: operational code pointer renders {loc} (tmp fixture), no :None")


def case_root_resolution(proj: Path) -> None:
    # 1. explicit root wins over everything
    assert gs.resolve_amg_root(cli_root=proj / "custom") == \
        (proj / "custom" / "amg").resolve()
    # 2. AMG_AGENT_DIR comes next
    os.environ["AMG_AGENT_DIR"] = str(proj / "envdir")
    try:
        assert gs.resolve_amg_root(start=proj) == (proj / "envdir" / "amg").resolve()
    finally:
        del os.environ["AMG_AGENT_DIR"]
    # 3. config search upward from inside the project finds .claude/amg
    assert gs.resolve_amg_root(start=proj / "src" / "core") == \
        (proj / ".claude" / "amg").resolve()
    # 4/5. with no config anywhere up from a bare dir: the engine's own amg/
    # wins IF it exists (dev layout), else the default <start>/.claude —
    # assert whichever state this environment is in (assumes no global AMG
    # config in the temp dir's ancestors).
    bare = Path(tempfile.mkdtemp(prefix="amg-bare-"))
    try:
        engine_amg = Path(gs.__file__).resolve().parents[3] / "amg"
        expected = engine_amg if engine_amg.is_dir() else (bare / ".claude" / "amg").resolve()
        assert gs.resolve_amg_root(start=bare) == expected, \
            (gs.resolve_amg_root(start=bare), expected)
    finally:
        shutil.rmtree(bare, ignore_errors=True)
    print("PASS  root: cli > env > config-upward > engine dir > default .claude")


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
        case_multi_item_part_of(proj)
        case_active_gating(proj)
        case_imports_resolver(proj)
        case_move_migrates_earned(proj)
        case_markdown_fences()
        case_lineno_in_pack(proj)
        case_root_resolution(proj)
        print("\nALL RECONCILE CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
