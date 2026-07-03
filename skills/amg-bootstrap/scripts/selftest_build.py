#!/usr/bin/env python3
"""
selftest_build.py — stage 19 regression: the FULL build pipeline on a mini project
with a deterministic STUB builder instead of the LLM (roadmap stage 19, task 9).
Proves, without a single model call:

  1. skeleton   : bootstrap emits the deterministic backbone (defines / inherits)
                  and resolver-bound cross-file calls; builtins and external
                  attribute chains produce no edges (audits 1.40 / 1.41).
  2. stub apply : a stub derivation — template summaries + edges, including
                  deliberately malformed items (swapped confidence/edges, missing
                  id, non-list part_of, a bare string) and non-canonical ids
                  (missing path prefix, doubled category prefix) — applies with
                  per-item repair/skip and never aborts the batch (audit 1.43);
                  a mis-prefixed target re-binds to the canonical id (audit 1.42).
  3. metrics    : the connectivity gate reports ONE component, zero internal
                  dangling edges, zero isolated nodes and no undocumented doc
                  nodes on the fully built graph (audit 1.44); external `imports`
                  are counted separately, not as defects.
  4. idempotency: a re-run bootstrap is a strict no-op (nothing re-queued, no
                  edge rewrites).
  5. cache      : a wipe-and-rebuild restores every per-unit derivation VERBATIM
                  from cache/derivations/ without a single "model" call (practical
                  determinism, audit 1.46); a changed working_language misses the
                  cache by key instead of returning foreign-language summaries.

The fixture is hermetic: config.yml is mandatory (extraction exits without one)
and the `agent_dir` key is deliberately ABSENT so the machine's global defaults
config is never merged in (stage 18 layering rule).

Run:  python selftest_build.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import graph_store as gs
import reconcile as RC

CORE = "code:src/pkg/core.py"
UTIL = "code:src/pkg/util.py"
INIT = "code:src/pkg/__init__.py"
GUIDE = "doc:doc/guide.md::guide"
RECORD = "data:data/settings.json::limits"


def build_project() -> Path:
    proj = Path(tempfile.mkdtemp(prefix="amg-build-"))
    amg = proj / ".claude" / "amg"
    amg.mkdir(parents=True)
    (amg / "config.yml").write_text(
        "active: true\nworking_language: en\nmirror_path: [src, doc, data]\n",
        encoding="utf-8")
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
        "        return self.ping() + helper(0)\n",
        encoding="utf-8")
    (proj / "doc").mkdir()
    (proj / "doc" / "guide.md").write_text(
        "# Guide\n\nHow Widget renders and why Base exists.\n", encoding="utf-8")
    (proj / "data").mkdir()
    (proj / "data" / "settings.json").write_text(
        json.dumps({"limits": {"max": 5, "min": 1}}), encoding="utf-8")
    return proj


def stub_derivation(queue_units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The deterministic stand-in for the amg-builder: a template summary per queued
    unit (echoing its content_sha, like the real builder), cross-domain edges written
    the way a model plausibly writes them (a doc target WITHOUT its leading dirs), a
    data->code link, and a tail of deliberately malformed / non-canonical items that
    exercise the per-item validation (audit 1.43)."""
    items: List[Dict[str, Any]] = []
    sha = {u["id"]: u["content_sha"] for u in queue_units}
    for u in queue_units:
        item: Dict[str, Any] = {"id": u["id"], "content_sha": u["content_sha"],
                                "summary": f"Summary of {u['id']}"}
        if u["id"] == GUIDE:
            # the mis-prefixed target the resolver must re-bind (audit 1.42)
            item["edges"] = [{"rel": "documents", "to": "code:pkg/core.py::Widget",
                              "w": 0.9}]
        if u["id"] == RECORD:
            item["edges"] = [{"rel": "relates_to", "to": CORE, "w": 0.5}]
        items.append(item)
    items += [
        # swapped fields: the edge list under `confidence`, the float under `edges`
        {"id": f"{CORE}::top_fn", "content_sha": sha[f"{CORE}::top_fn"],
         "confidence": [{"rel": "depends_on", "to": f"{UTIL}::helper", "w": 0.6}],
         "edges": 0.77},
        # missing id -> skipped, batch continues
        {"summary": "an item with no id"},
        # non-list part_of -> the field drops, the summary still applies (a second
        # item on the same sha also proves multi-item cache entries restore in order)
        {"id": UTIL, "content_sha": sha[UTIL], "part_of": "not-a-list",
         "summary": "Utility helpers module."},
        # not an object at all -> skipped
        "just-a-string",
        # doubled category prefix on a create item -> collapses to overview:build
        {"id": "hub:overview:build", "type": "overview",
         "summary": "Overview of the build fixture.",
         "part_of": [{"topic": "hub:build", "w": 1.0}]},
    ]
    return items


def synth_stub() -> List[Dict[str, Any]]:
    """The deterministic stand-in for amg-synth: one hub adopting the core module."""
    return [{"id": "hub:build", "type": "hub", "summary": "Build subsystem hub.",
             "edges": [{"rel": "documents", "to": CORE, "w": 0.8}]}]


def main() -> int:
    proj = build_project()
    amg = proj / ".claude" / "amg"
    try:
        # 1. skeleton: deterministic backbone + resolver-bound calls
        s = RC.plan(proj, amg)
        assert s["added"] == 11 and s["queued_for_semantic"] == 11, s
        nodes = RC.load_nodes(gs.GraphStore(amg))

        def rels(nid: str) -> set:
            return {(e["rel"], e["to"]) for e in nodes[nid]["edges"]}

        assert {("defines", f"{CORE}::top_fn"), ("defines", f"{CORE}::Widget")} <= rels(CORE)
        assert ("inherits", f"{CORE}::Base") in rels(f"{CORE}::Widget")
        assert {(r, t) for r, t in rels(f"{CORE}::top_fn") if r == "calls"} \
            == {("calls", f"{UTIL}::helper")}, rels(f"{CORE}::top_fn")
        assert ("imports", CORE) in rels(INIT)
        print("PASS  skeleton: backbone + resolved calls, no builtin edges")

        # 2. stub builder -> apply: malformed items repaired/skipped, batch survives
        queue = json.loads((amg / "work" / "queue.json").read_text(encoding="utf-8"))
        (amg / "work" / "derived-stub.json").write_text(
            json.dumps(stub_derivation(queue["units"]), ensure_ascii=False),
            encoding="utf-8")
        r = RC.apply_derivation(proj, amg / "work" / "derived-stub.json", amg)
        assert r["skipped_invalid"] == 2, r          # the no-id item + the bare string
        assert r["created"] == 1 and r["applied"] == 11 + 2, r  # units + swap + util extra
        nodes = RC.load_nodes(gs.GraphStore(amg))
        assert "overview:build" in nodes and "hub:overview:build" not in nodes, \
            "the doubled category prefix must collapse"
        guide_tos = {e["to"] for e in nodes[GUIDE]["edges"]}
        assert f"{CORE}::Widget" in guide_tos, guide_tos      # mis-prefix re-bound
        top = nodes[f"{CORE}::top_fn"]
        assert top["confidence"] == 0.77, top.get("confidence")
        assert ("depends_on", f"{UTIL}::helper") in rels(f"{CORE}::top_fn")
        print("PASS  apply: swap repaired, prefix re-bound, 2 skipped, batch intact")

        # 3. synth stub -> apply -> connectivity gate over the finished graph
        (amg / "work" / "derived-synth.json").write_text(
            json.dumps(synth_stub(), ensure_ascii=False), encoding="utf-8")
        RC.apply_derivation(proj, amg / "work" / "derived-synth.json", amg)
        m = RC.graph_metrics(RC.load_nodes(gs.GraphStore(amg)))
        assert m["components"] == 1, m
        assert m["largest_component_share"] == 1.0, m
        assert m["dangling_internal"] == 0, m
        assert m["isolated_nodes"] == 0, m
        assert m["doc_without_documents"] == 0, m
        assert m["dangling_external_imports"] == 1, m         # import json — legitimate
        assert m["gate"] == "ok", m
        print("PASS  metrics: one component, 0 internal dangling, gate ok "
              f"(external imports={m['dangling_external_imports']})")

        # 4. idempotency: the re-run bootstrap is a strict no-op
        s = RC.plan(proj, amg)
        assert s["added"] == s["changed"] == s["requeued_stale"] == 0, s
        assert s["edges_refreshed"] == 0 and s["queued_for_semantic"] == 0, s
        print("PASS  idempotency: re-run bootstrap is a no-op")

        # 5. derivation cache: wipe the graph, rebuild, restore verbatim (audit 1.46)
        summaries_before = {nid: n["summary"] for nid, n in
                            RC.load_nodes(gs.GraphStore(amg)).items()
                            if n.get("source_kind") == "derived_from_file"}
        shutil.rmtree(amg / "nodes")
        shutil.rmtree(amg / "journal")
        shutil.rmtree(amg / "work", ignore_errors=True)
        s = RC.plan(proj, amg)                       # fresh skeleton, all stale
        assert s["added"] == 11 and s["queued_for_semantic"] == 11, s
        c = RC.apply_cached(proj, amg)
        assert c["restored_units"] == 11 and c["remaining"] == 0, c
        nodes = RC.load_nodes(gs.GraphStore(amg))
        restored = {nid: n["summary"] for nid, n in nodes.items()
                    if n.get("source_kind") == "derived_from_file"}
        assert restored == summaries_before, "cache must restore derivations verbatim"
        assert all(nodes[nid]["status"] == "active" for nid in restored), "all derived"
        assert nodes[f"{CORE}::top_fn"]["confidence"] == 0.77, \
            "a second cached item on the same sha must restore too"
        guide_tos = {e["to"] for e in nodes[GUIDE]["edges"]}
        assert f"{CORE}::Widget" in guide_tos, guide_tos   # re-normalized on restore
        q = json.loads((amg / "work" / "queue.json").read_text(encoding="utf-8"))
        assert q["units"] == [], "the restored units must leave the queue"
        # a changed working language must MISS by key, never restore foreign summaries
        (amg / "config.yml").write_text(
            "active: true\nworking_language: ru\nmirror_path: [src, doc, data]\n",
            encoding="utf-8")
        shutil.rmtree(amg / "nodes")
        shutil.rmtree(amg / "journal")
        RC.plan(proj, amg)
        c = RC.apply_cached(proj, amg)
        assert c["restored_units"] == 0 and c["remaining"] == 11, c
        print("PASS  cache: wipe+rebuild restores verbatim; language change misses")

        print("\nALL BUILD-PIPELINE CHECKS PASSED")
        return 0
    finally:
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
