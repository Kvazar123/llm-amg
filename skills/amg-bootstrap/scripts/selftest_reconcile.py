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
 15. vetoes   : an amg/-named engine checkout never resolves as the store (even
                after a stray run created nodes/+journal/ in it); the installed
                preset store next to it wins; the home agent dir (the global
                defaults config layer) is not a store; retrieve._default_store
                mirrors the same rules (Stage 18, audits 1.39/1.37).
 16. missing  : a missing config.yml exits with an "AMG is not installed"
                diagnostic naming the resolved root (audit 1.50), no traceback.
 17. resolver : stage 19 (audits 1.40/1.41) — the deterministic resolver emits the
                defines backbone and inherits edges, binds cross-file calls through
                the file's own imports, and emits NO edge for builtins/unresolvable
                receivers (nothing dangles).
 18. repair   : stage 19 (audit 1.42) — a judgment edge whose target was written
                without its leading directories re-binds to the unique canonical id
                (at apply and by the bootstrap sweep); an ambiguous suffix stays
                untouched; a second bootstrap is a strict no-op (idempotent sweep).

Run:  python selftest_reconcile.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List

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


def queue_items(proj: Path) -> Dict[str, Any]:
    qpath = proj / ".claude" / "amg" / "work" / "queue.json"
    q = json.loads(qpath.read_text(encoding="utf-8"))
    return {u["id"]: u for u in q["units"]}


def graph_nodes(proj: Path) -> Dict[str, Any]:
    return RC.load_nodes(gs.GraphStore(proj / ".claude" / "amg"))


def derive_all(proj: Path, ids: Iterable[str]) -> None:
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
    # 3b. the .agents preset is probed too (1.32): a non-.claude project resolves
    ag = Path(tempfile.mkdtemp(prefix="amg-agents-"))
    try:
        (ag / ".agents" / "amg").mkdir(parents=True)
        (ag / ".agents" / "amg" / "config.yml").write_text("active: true\n", encoding="utf-8")
        assert gs.resolve_amg_root(start=ag) == (ag / ".agents" / "amg").resolve()
    finally:
        shutil.rmtree(ag, ignore_errors=True)
    # 4/5. with no config anywhere up from a bare dir: the engine's own amg/
    # wins IF it is an initialized store (dev layout; Stage 18 tightened the mere
    # is_dir test), else the default <start>/.claude — assert whichever state this
    # environment is in (assumes no AMG config in the temp dir's ancestors).
    bare = Path(tempfile.mkdtemp(prefix="amg-bare-"))
    try:
        engine_amg = Path(gs.__file__).resolve().parents[3] / "amg"
        expected = (engine_amg if gs._initialized_store(engine_amg)
                    and not gs._looks_like_engine_checkout(engine_amg)
                    else (bare / ".claude" / "amg").resolve())
        assert gs.resolve_amg_root(start=bare) == expected, \
            (gs.resolve_amg_root(start=bare), expected)
    finally:
        shutil.rmtree(bare, ignore_errors=True)
    print("PASS  root: cli > env > config-upward (.claude/.agents) > engine dir > default .claude")


def case_store_resolution_vetoes() -> None:
    """Stage 18 (audit 1.39): an amg/-named ENGINE CHECKOUT never resolves as the
    store — neither alone (falls through to the default) nor when a stray run already
    'initialized' it (nodes/+journal/ inside a checkout), and a real installed store
    next to it wins via the preset probe. The home agent dir (the machine-wide
    defaults config layer, audit 1.37) is not a store either."""
    proj = Path(tempfile.mkdtemp(prefix="amg-veto-"))
    engine_amg = Path(gs.__file__).resolve().parents[3] / "amg"
    fallback_ok = (gs._initialized_store(engine_amg)
                   and not gs._looks_like_engine_checkout(engine_amg))
    default = engine_amg if fallback_ok else (proj / ".claude" / "amg").resolve()
    try:
        # an unpacked checkout: <proj>/amg carrying the template config + engine files
        co = proj / "amg"
        (co / "skills").mkdir(parents=True)
        (co / "config.yml").write_text("mirror_path: [src, doc]\n", encoding="utf-8")
        (co / "install.py").write_text("# installer\n", encoding="utf-8")
        # 1. checkout alone: vetoed -> the chain falls through past it
        assert gs.resolve_amg_root(start=proj) == default, gs.resolve_amg_root(start=proj)
        # 2. a stray run 'initialized' the checkout: nodes/+journal/ alone must not qualify
        (co / "nodes").mkdir()
        (co / "journal").mkdir()
        assert gs.resolve_amg_root(start=proj) == default, gs.resolve_amg_root(start=proj)
        # 3. checkout next to the real installed store: the preset wins
        real = proj / ".claude" / "amg"
        real.mkdir(parents=True)
        (real / "config.yml").write_text("active: true\n", encoding="utf-8")
        assert gs.resolve_amg_root(start=proj) == real.resolve()
        # 4. a bare legit store (initialized, no signature) still resolves — the cwd
        # sits inside the agent dir
        bare = Path(tempfile.mkdtemp(prefix="amg-barestore-"))
        try:
            st = bare / "amg"
            (st / "nodes").mkdir(parents=True)
            (st / "journal").mkdir()
            (st / "config.yml").write_text("active: true\n", encoding="utf-8")
            assert gs.resolve_amg_root(start=bare) == st.resolve()
        finally:
            shutil.rmtree(bare, ignore_errors=True)
        # 5. the home level is skipped: a global defaults config (~/.claude/amg/config.yml)
        # must not resolve as the store of an uninstalled project under home
        fake_home = Path(tempfile.mkdtemp(prefix="amg-home-"))
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        try:
            os.environ["HOME"] = str(fake_home)
            os.environ["USERPROFILE"] = str(fake_home)
            gcfg = fake_home / ".claude" / "amg"
            gcfg.mkdir(parents=True)
            (gcfg / "config.yml").write_text("models:\n  synthesis: opus\n", encoding="utf-8")
            inner = fake_home / "projects" / "fresh"
            inner.mkdir(parents=True)
            got = gs.resolve_amg_root(start=inner)
            assert got != gcfg.resolve(), got
            assert got == (engine_amg if fallback_ok
                           else (inner / ".claude" / "amg").resolve()), got
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            shutil.rmtree(fake_home, ignore_errors=True)
        print("PASS  root: engine checkout (even 'initialized') never hijacks the store; "
              "presets win; home defaults layer is not a store")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_default_store_mirror() -> None:
    """retrieve._default_store mirrors the Stage 18 resolver rules: the checkout is
    vetoed and the installed preset store is found from the project cwd."""
    sys.path.insert(0, str(HERE.parents[1] / "amg-retrieve" / "scripts"))
    import retrieve as RT
    proj = Path(tempfile.mkdtemp(prefix="amg-dsv-"))
    cwd = Path.cwd()
    try:
        co = proj / "amg"
        (co / "skills").mkdir(parents=True)
        (co / "config.yml").write_text("mirror_path: [src]\n", encoding="utf-8")
        real = proj / ".claude" / "amg"
        real.mkdir(parents=True)
        (real / "config.yml").write_text("active: true\n", encoding="utf-8")
        os.chdir(proj)
        assert Path(RT._default_store()).resolve() == real.resolve(), RT._default_store()
        print("PASS  root: retrieve._default_store skips the checkout, finds the preset store")
    finally:
        os.chdir(cwd)
        shutil.rmtree(proj, ignore_errors=True)


def case_uninstalled_diagnostic() -> None:
    """audit 1.50: extract_structure.load_config on a MISSING config.yml exits with a
    diagnostic naming the resolved root, instead of a FileNotFoundError traceback."""
    empty = Path(tempfile.mkdtemp(prefix="amg-nocfg-"))
    try:
        root = empty / ".claude" / "amg"
        try:
            ES.load_config(root)
            raise AssertionError("load_config must exit on a missing config.yml")
        except SystemExit as e:
            msg = str(e)
            assert "not installed" in msg and str(root) in msg, msg
        print("PASS  diagnostics: missing config.yml -> 'AMG is not installed' naming the root")
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def case_absorb_once() -> None:
    """absorb_once (Stage 11): ingested once, then FROZEN — a later source change is not
    re-derived (no requeue, no drift) and deletion never purges it, unlike absorb which
    re-derives on change."""
    proj = Path(tempfile.mkdtemp(prefix="amg-once-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        (amg / "config.yml").write_text(
            "active: true\nworking_language: en\nabsorb_once_path: snap\n", encoding="utf-8")
        snap = proj / "snap"
        snap.mkdir()
        f = snap / "note.txt"
        f.write_text("first version\n", encoding="utf-8")
        nid = "doc:snap/note.txt::b1"
        s = RC.plan(proj, amg)
        assert s["added"] == 1 and s["queued_for_semantic"] == 1, s
        n = RC.load_nodes(gs.GraphStore(amg))[nid]
        assert n["policy"] == "absorb_once" and n["status"] == "stale", n
        work = amg / "work"
        (work / "d.json").write_text(json.dumps([{"id": nid, "summary": "snapshot note"}]),
                                     encoding="utf-8")
        RC.apply_derivation(proj, work / "d.json", amg)
        assert RC.load_nodes(gs.GraphStore(amg))[nid]["status"] == "active"
        f.write_text("second version, edited\n", encoding="utf-8")     # change -> frozen
        s = RC.plan(proj, amg)
        assert s["frozen"] == 1 and s["requeued_stale"] == 0 and s["changed"] == 0, s
        n = RC.load_nodes(gs.GraphStore(amg))[nid]
        assert n["status"] == "active" and n["summary"] == "snapshot note", n
        assert json.loads((work / "queue.json").read_text())["units"] == [], "frozen not queued"
        f.unlink()                                                     # delete -> kept (absorb-like)
        s = RC.plan(proj, amg)
        assert s["deleted"] == 0 and nid in RC.load_nodes(gs.GraphStore(amg)), s
        print("PASS  absorb_once: ingested once then frozen (changes ignored, deletion kept)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_resume_freshness() -> None:
    """Resumable derivation (task 13): a derived item echoes content_sha; apply skips it
    when the source has changed since (the node's source_hash moved on), so a leftover
    derived-*.json never blindly derives against stale content. An item with NO
    content_sha still applies (back-compat / synthesized hub items)."""
    proj = Path(tempfile.mkdtemp(prefix="amg-resume-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        (amg / "config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: src\n", encoding="utf-8")
        src = proj / "src"
        src.mkdir()
        f = src / "m.py"
        f.write_text("def a():\n    return 1\n", encoding="utf-8")
        RC.plan(proj, amg)
        nid = "code:src/m.py::a"
        sha_v1 = RC.load_nodes(gs.GraphStore(amg))[nid]["source_hash"]
        work = amg / "work"
        work.mkdir(exist_ok=True)

        # fresh item (content_sha matches the current source) -> applied, active
        (work / "d1.json").write_text(json.dumps(
            [{"id": nid, "summary": "v1 summary", "content_sha": sha_v1}]), encoding="utf-8")
        r = RC.apply_derivation(proj, work / "d1.json", amg)
        assert r["applied"] == 1 and r["skipped_stale"] == 0, r
        assert RC.load_nodes(gs.GraphStore(amg))[nid]["status"] == "active"

        # change the source -> source_hash moves on; re-plan marks the node stale
        f.write_text("def a():\n    return 2  # changed\n", encoding="utf-8")
        RC.plan(proj, amg)
        n = RC.load_nodes(gs.GraphStore(amg))[nid]
        assert n["status"] == "stale" and n["source_hash"] != sha_v1, n

        # a LEFTOVER item with the OLD content_sha must be SKIPPED, not blindly applied
        r = RC.apply_derivation(proj, work / "d1.json", amg)
        assert r["skipped_stale"] == 1 and r["applied"] == 0, r
        assert RC.load_nodes(gs.GraphStore(amg))[nid]["status"] == "stale", \
            "a stale-sha item must not flip the node active"

        # a fresh item with the CURRENT content_sha applies
        sha_v2 = RC.load_nodes(gs.GraphStore(amg))[nid]["source_hash"]
        (work / "d2.json").write_text(json.dumps(
            [{"id": nid, "summary": "v2 summary", "content_sha": sha_v2}]), encoding="utf-8")
        r = RC.apply_derivation(proj, work / "d2.json", amg)
        assert r["applied"] == 1, r
        assert RC.load_nodes(gs.GraphStore(amg))[nid]["status"] == "active"

        # back-compat: an item with NO content_sha still applies (e.g. synthesized hubs)
        (work / "d3.json").write_text(json.dumps(
            [{"id": nid, "summary": "v3 no sha"}]), encoding="utf-8")
        r = RC.apply_derivation(proj, work / "d3.json", amg)
        assert r["applied"] == 1 and r["skipped_stale"] == 0, r
        print("PASS  resume: stale-sha item skipped; current-sha applies; no-sha back-compat (task 13)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_provenance_and_confidence() -> None:
    """Stage 13: ingest stamps provenance.kind + verification(unverified) + line_end on
    source-derived nodes; a content change voids a prior verification (back to
    unverified); the builder's confidence estimate is applied, and a summary that lands
    without one takes the default; a synthesized hub gets kind model_inference +
    derived_from."""
    proj = Path(tempfile.mkdtemp(prefix="amg-prov-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        (amg / "config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: src\n", encoding="utf-8")
        src = proj / "src"
        src.mkdir()
        (src / "m.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n",
                                  encoding="utf-8")
        (src / "g.md").write_text("# Title\n\nIntro.\n\n## Sec\n\nBody.\n", encoding="utf-8")
        RC.plan(proj, amg)
        nodes = RC.load_nodes(gs.GraphStore(amg))

        a = nodes["code:src/m.py::a"]
        assert a["provenance"]["kind"] == "code", a["provenance"]
        assert a["verification"] == {"status": "unverified", "method": "none"}, a["verification"]
        assert a["line_end"] and a["line_end"] >= a["lineno"], a            # real span
        sec = nodes["doc:src/g.md::sec"]
        assert sec["provenance"]["kind"] == "doc", sec["provenance"]
        assert sec["line_end"] > sec["lineno"], sec

        # a content change must void a prior verification (simulate a 'verified' stamp)
        meta = {k: v for k, v in a.items() if not k.startswith("_")}
        meta["verification"] = {"status": "verified", "method": "ast"}
        gs.atomic_write_text(amg / a["_path"], RC.serialize_node(meta, ""))
        (src / "m.py").write_text(
            "def a():\n    return 11  # changed\n\n\ndef b():\n    return 2\n", encoding="utf-8")
        RC.plan(proj, amg)
        a = RC.load_nodes(gs.GraphStore(amg))["code:src/m.py::a"]
        assert a["verification"]["status"] == "unverified", "change must void verification"

        # builder confidence: explicit value applied; a summary without one takes default
        work = amg / "work"
        work.mkdir(exist_ok=True)
        (work / "d.json").write_text(json.dumps([
            {"id": "code:src/m.py::a", "summary": "fn a", "confidence": 0.42,
             "content_sha": a["source_hash"]},
            {"id": "code:src/m.py::b", "summary": "fn b"}]), encoding="utf-8")
        RC.apply_derivation(proj, work / "d.json", amg)
        nn = RC.load_nodes(gs.GraphStore(amg))
        assert nn["code:src/m.py::a"]["confidence"] == 0.42, nn["code:src/m.py::a"]
        assert nn["code:src/m.py::b"]["confidence"] == RC.DEFAULT_CONFIDENCE, nn["code:src/m.py::b"]

        # synthesized hub: provenance.kind model_inference + derived_from carried through
        (work / "h.json").write_text(json.dumps([
            {"id": "hub:x", "type": "hub", "summary": "hub", "confidence": 0.6,
             "derived_from": ["code:src/m.py::a"]}]), encoding="utf-8")
        RC.apply_derivation(proj, work / "h.json", amg)
        h = RC.load_nodes(gs.GraphStore(amg))["hub:x"]
        assert h["provenance"]["kind"] == "model_inference", h["provenance"]
        assert h["provenance"]["derived_from"] == ["code:src/m.py::a"], h["provenance"]
        assert h["verification"]["status"] == "unverified", h
        print("PASS  provenance: ingest stamps kind/verification/line_end; change voids "
              "verification; confidence applied (Stage 13)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_resolver_backbone() -> None:
    """Stage 19 (audits 1.40/1.41): defines backbone, inherits, resolver-bound calls;
    builtins and external attribute chains produce NO edge at all."""
    proj = Path(tempfile.mkdtemp(prefix="amg-resolve-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        (amg / "config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: src\n", encoding="utf-8")
        pkg = proj / "src" / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("from pkg.core import Widget\n", encoding="utf-8")
        (pkg / "util.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
        (pkg / "core.py").write_text(
            "from pkg.util import helper\n"
            "import json\n\n\n"
            "def top_fn(a):\n"
            "    if isinstance(a, str):\n"
            "        return helper(json.dumps(a))\n"
            "    return helper(a)\n\n\n"
            "class Base:\n"
            "    def ping(self):\n"
            "        return 1\n\n\n"
            "class Widget(Base):\n"
            "    def render(self):\n"
            "        return self.ping() + helper(0) + Base.ping(self)\n",
            encoding="utf-8")
        s = RC.plan(proj, amg)
        assert s["added"] == 9, s            # 3 modules + helper + top_fn + Base(.ping) + Widget(.render)
        nodes = RC.load_nodes(gs.GraphStore(amg))
        core = "code:src/pkg/core.py"
        util_helper = "code:src/pkg/util.py::helper"

        def rels(nid: str) -> set:
            return {(e["rel"], e["to"]) for e in nodes[nid]["edges"]}

        # defines backbone: module -> top-level symbols, class -> its methods
        assert {("defines", f"{core}::top_fn"), ("defines", f"{core}::Base"),
                ("defines", f"{core}::Widget")} <= rels(core), rels(core)
        assert ("defines", f"{core}::Base.ping") in rels(f"{core}::Base"), \
            rels(f"{core}::Base")
        # imports resolve in-project; stdlib stays a dotted (external) name
        assert ("imports", "code:src/pkg/util.py") in rels(core), rels(core)
        assert ("imports", "code:json") in rels(core), rels(core)
        # calls: cross-file through the import binding; builtins/externals emit nothing
        top_calls = {(r, t) for r, t in rels(f"{core}::top_fn") if r == "calls"}
        assert top_calls == {("calls", util_helper)}, top_calls
        # inherits + same-file qualified ref + inherited self-method NOT guessed
        w_rels = rels(f"{core}::Widget")
        assert ("inherits", f"{core}::Base") in w_rels, w_rels
        assert ("calls", f"{core}::Base.ping") in w_rels, w_rels
        assert ("calls", util_helper) in w_rels, w_rels
        assert not any(t.endswith("::ping") and r == "calls" and "Base" not in t
                       for r, t in w_rels), w_rels    # self.ping (inherited) is not guessed
        # __init__ imports pkg.core -> the package module is connected, not an orphan
        assert ("imports", core) in rels("code:src/pkg/__init__.py")
        print("PASS  resolver: defines/inherits/cross-file calls bound; builtins emit no edge")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_prefix_repair_sweep() -> None:
    """Stage 19 (audit 1.42): canonical-id repair by unique path suffix — at apply
    for judgment edges, and by the bootstrap sweep for nodes written earlier; an
    ambiguous suffix is never repaired; the sweep is idempotent (second run no-op)."""
    proj = Path(tempfile.mkdtemp(prefix="amg-repair-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        (amg / "config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: src\n", encoding="utf-8")
        src = proj / "src"
        (src / "a").mkdir(parents=True)
        (src / "b").mkdir(parents=True)
        (src / "app.py").write_text("def top(x):\n    return x\n", encoding="utf-8")
        (src / "a" / "dup.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (src / "b" / "dup.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        RC.plan(proj, amg)
        top_id = "code:src/app.py::top"

        # apply-time repair: a documents target missing its src/ prefix re-binds
        work = amg / "work"
        work.mkdir(exist_ok=True)
        (work / "d.json").write_text(json.dumps([
            {"id": "hub:t", "type": "hub", "summary": "T",
             "edges": [{"rel": "documents", "to": "code:app.py::top", "w": 0.9},
                       {"rel": "documents", "to": "code:dup.py::f", "w": 0.5}]}]),
            encoding="utf-8")
        RC.apply_derivation(proj, work / "d.json", amg)
        hub = RC.load_nodes(gs.GraphStore(amg))["hub:t"]
        tos = {e["to"] for e in hub["edges"]}
        assert top_id in tos, tos                      # unique suffix -> repaired
        assert "code:dup.py::f" in tos, tos            # ambiguous (a/ vs b/) -> untouched

        # sweep repair: a node written with a broken target BEFORE the resolver era
        store = gs.GraphStore(amg)
        broken = dict(hub)
        broken.pop("_path"), broken.pop("_body", "")
        broken["id"] = "hub:legacy"
        broken["edges"] = [{"rel": "documents", "to": "code:app.py::top", "w": 0.7,
                            "coact": 0, "origin": "synthesized"}]
        with store.lock():
            store.transaction().write(
                RC.node_relpath("hub:legacy", "_hubs"),
                RC.serialize_node(broken, "")).commit()
        s = RC.plan(proj, amg)
        assert s["edges_refreshed"] >= 1, s
        legacy = RC.load_nodes(store)["hub:legacy"]
        assert legacy["edges"][0]["to"] == top_id, legacy["edges"]
        s = RC.plan(proj, amg)                         # idempotent: nothing left to fix
        assert s["edges_refreshed"] == 0, s
        print("PASS  repair: unique suffix re-binds (apply + sweep), ambiguity untouched, "
              "sweep idempotent")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_merge_conflict_resilience() -> None:
    """Stage 16: a node file left with git merge-conflict markers must NOT crash the read
    paths (its YAML no longer parses) — every load_nodes skips it — and find_conflict_markers
    surfaces it so status/repair/bootstrap can flag it for the user."""
    sys.path.insert(0, str(HERE.parents[1] / "amg-retrieve" / "scripts"))
    import retrieve as RT
    proj = Path(tempfile.mkdtemp(prefix="amg-conflict-"))
    try:
        store = gs.GraphStore(proj / ".claude" / "amg")
        store.init()
        good = ("---\nid: code:src/a.py::f\ntype: function\n"
                "source_kind: derived_from_file\nstatus: active\nsummary: ok\n---\n")
        store.transaction().write("nodes/code/good-1.md", good).commit()
        # a same-node git conflict leaves literal markers inside the frontmatter, so the
        # YAML no longer parses.
        bad = ("---\nid: code:src/b.py::g\n<<<<<<< HEAD\nsummary: mine\n=======\n"
               "summary: theirs\n>>>>>>> feature\ntype: function\n---\n")
        gs.atomic_write_text(store.root / "nodes" / "code" / "bad-2.md", bad)

        rc_nodes = RC.load_nodes(store)
        assert list(rc_nodes) == ["code:src/a.py::f"], \
            f"reconcile.load_nodes must skip the conflicted node, got {list(rc_nodes)}"
        rt_nodes = RT.load_nodes(store.root)
        assert "code:src/a.py::f" in rt_nodes and "code:src/b.py::g" not in rt_nodes, \
            "retrieve.load_nodes must skip the conflicted node too (no crash)"
        assert RC.find_conflict_markers(store) == ["nodes/code/bad-2.md"], \
            RC.find_conflict_markers(store)
        print("PASS  conflict: a merge-conflicted node is skipped by every load_nodes and "
              "reported by find_conflict_markers")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


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
        case_store_resolution_vetoes()
        case_default_store_mirror()
        case_uninstalled_diagnostic()
        case_absorb_once()
        case_resume_freshness()
        case_provenance_and_confidence()
        case_merge_conflict_resilience()
        case_resolver_backbone()
        case_prefix_repair_sweep()
        print("\nALL RECONCILE CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
