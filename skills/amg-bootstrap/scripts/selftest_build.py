#!/usr/bin/env python3
"""
selftest_build.py — regression: the FULL build pipeline on a mini project
with a deterministic STUB builder instead of the LLM.
Proves, without a single model call:

  1. skeleton   : bootstrap emits the deterministic backbone (defines / inherits)
                  and resolver-bound cross-file calls; builtins and external
                  attribute chains produce no edges (audits 1.40 / 1.41).
  2. stub apply : a stub derivation — template summaries + edges, including
                  deliberately malformed items (swapped confidence/edges, missing
                  id, non-list part_of, a bare string) and non-canonical ids
                  (missing path prefix, doubled category prefix) — applies with
                  per-item repair/skip and never aborts the batch; a mis-prefixed
                  target re-binds to the canonical id; an update aimed at a
                  nonexistent node is skipped AND sampled (missing_sample); the
                  apply refreshes work/queue.json down to what still awaits
                  derivation (status/sync-defer read it raw).
  3. metrics    : the connectivity gate reports ONE component, zero dangling
                  edges, zero isolated nodes and no undocumented doc FILES on the
                  fully built graph; external `imports` are counted separately;
                  dangling targets split by responsibility — a structural miss
                  gates, a model-written one is reported but never flips the gate.
  4. idempotency: a re-run bootstrap is a strict no-op (nothing re-queued, no
                  edge rewrites).
  5. cache      : a wipe-and-rebuild restores every per-unit derivation VERBATIM
                  from cache/derivations/ without a single "model" call (practical
                  determinism); a changed working_language misses the
                  cache by key instead of returning foreign-language summaries.
  6. candidates : link_candidates nominates unlinked cross-domain pairs by summary
                  similarity (the lexical fallback path — no embedding backend
                  needed), skips already-linked pairs and stale nodes, batches
                  deterministically with the hub list attached, and --hubs writes
                  stable directory-anchored hub suggestions.
  7. batch door : apply-derived consumes every work/derived-*.json (checkpoint
                  parts included) in ONE call with one aggregated result, moves the
                  consumed files to work/applied/ and quarantines a torn (invalid
                  JSON) part in work/invalid/; a re-run is a cheap no-op — the
                  resume path (the batched apply door).
  8. synth sheet: --synth-input writes the whole summary layer as one grouped file
                  (with the deterministic gap material), so the synthesis agent
                  reads a single input instead of scanning nodes/.

The fixture is hermetic: config.yml is mandatory (extraction exits without one)
and the `agent_dir` key is deliberately ABSENT so the machine's global defaults
config is never merged in (the config layering rule).

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
import link_candidates as LC
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
    # embeddings off: the candidate check below must exercise the LEXICAL fallback
    # deterministically, whatever backends this machine has installed; the trivial
    # shortcut is ON — helper and Widget.render are its targets below
    (amg / "config.yml").write_text(
        "active: true\nworking_language: en\nmirror_path: [src, doc, data]\n"
        "trivial_unit_max_lines: 3\n"
        "retrieval:\n  embeddings:\n    enabled: off\n",
        encoding="utf-8")
    pkg = proj / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from pkg.core import Widget\n", encoding="utf-8")
    # the module docstring keeps the MODULE's content hash distinct from the helper
    # slice's (a one-function file would otherwise share one sha and one cache entry)
    (pkg / "util.py").write_text(
        '"""Utility helpers."""\n\n\ndef helper(x):\n    return x\n', encoding="utf-8")
    (pkg / "core.py").write_text(
        "from pkg.util import helper\n"
        "import json\n\n\n"
        "def top_fn(a):\n"
        "    if isinstance(a, str):\n"
        "        return helper(json.dumps(a))\n"
        "    return helper(a)\n\n\n"
        "class Base:\n"
        "    def ping(self):\n"
        "        \"\"\"Health probe.\"\"\"\n"
        "        self.checked = True\n"
        "        return 1\n\n\n"
        "class Widget(Base):\n"
        "    def render(self):\n"
        "        return self.ping() + helper(0)\n\n\n"
        "class Caller:\n"
        "    def __call__(self):\n"
        "        return helper(1)\n",
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
    exercise the per-item validation."""
    items: List[Dict[str, Any]] = []
    sha = {u["id"]: u["content_sha"] for u in queue_units}
    for u in queue_units:
        item: Dict[str, Any] = {"id": u["id"], "content_sha": u["content_sha"],
                                "summary": f"Summary of {u['id']}"}
        if u["id"] == GUIDE:
            # rare shared tokens with Base.ping below -> a lexical link candidate
            item["summary"] = ("Guide covering Widget rendering and the heartbeat "
                               "health probe.")
            # two repairable target shapes: a path missing its leading dirs, and a
            # BARE symbol name with no path at all (unique qualname re-binds it)
            item["edges"] = [{"rel": "documents", "to": "code:pkg/core.py::Widget",
                              "w": 0.9},
                             {"rel": "relates_to", "to": "code:top_fn", "w": 0.4}]
        if u["id"] == f"{CORE}::Base.ping":
            item["summary"] = "Returns the heartbeat health probe value."
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
        # an update for an id that names no node (a model-invented target) ->
        # skipped_missing, with the id surfaced in missing_sample
        {"id": "code:ghost.py::nowhere", "content_sha": "deadbeef",
         "summary": "an update aimed at a node that does not exist"},
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
        # 1. skeleton: deterministic backbone + resolver-bound calls; trivial units
        # (helper, Widget.render — <=3 lines) auto-summarized out of the queue
        s = RC.plan(proj, amg)
        assert s["added"] == 13 and s["queued_for_semantic"] == 11, s
        assert s["auto_summarized"] == 2, s
        nodes = RC.load_nodes(gs.GraphStore(amg))

        def rels(nid: str) -> set:
            return {(e["rel"], e["to"]) for e in nodes[nid]["edges"]}

        assert {("defines", f"{CORE}::top_fn"), ("defines", f"{CORE}::Widget")} <= rels(CORE)
        assert ("inherits", f"{CORE}::Base") in rels(f"{CORE}::Widget")
        assert {(r, t) for r, t in rels(f"{CORE}::top_fn") if r == "calls"} \
            == {("calls", f"{UTIL}::helper")}, rels(f"{CORE}::top_fn")
        assert ("imports", CORE) in rels(INIT)
        print("PASS  skeleton: backbone + resolved calls, no builtin edges")

        # a trivial function is derived by code — active, its
        # one-line source as the summary, structural edges intact, NOT in the queue;
        # a protocol dunder (__call__) and a 4-line body still go to the model
        helper_node = nodes[f"{UTIL}::helper"]
        assert helper_node["status"] == "active", helper_node
        assert helper_node["summary"].startswith("def helper(x): return x"), helper_node
        assert helper_node["derived_from_hash"] == helper_node["source_hash"]
        assert nodes[f"{CORE}::Widget.render"]["status"] == "active"
        queued_ids = {u["id"] for u in json.loads(
            (amg / "work" / "queue.json").read_text(encoding="utf-8"))["units"]}
        assert f"{UTIL}::helper" not in queued_ids and f"{CORE}::Widget.render" not in queued_ids
        assert f"{CORE}::Caller.__call__" in queued_ids, "protocol dunder must reach the model"
        assert f"{CORE}::Base.ping" in queued_ids, "a 4-line body is not trivial"
        print("PASS  trivial: auto-summary derives dunder-sized units; guard + size respected")

        # the queue carries each unit's own text + line_end,
        # so the builder summarizes from the queue without re-opening sources
        q0 = {u["id"]: u for u in json.loads(
            (amg / "work" / "queue.json").read_text(encoding="utf-8"))["units"]}
        assert "def top_fn(a):" in q0[f"{CORE}::top_fn"]["text"], q0[f"{CORE}::top_fn"]
        assert q0[f"{CORE}::top_fn"]["line_end"] > q0[f"{CORE}::top_fn"]["lineno"]
        assert "How Widget renders" in q0[GUIDE]["text"], q0[GUIDE]
        assert q0[RECORD]["text"], "a JSON record carries its serialized fragment"
        assert all(u.get("text") for u in q0.values()), "every fixture unit fits the cap"
        # the cap: an oversized unit falls back to the pointer (no text in the item)
        big = RC._queue_item({"id": "x", "kind": "file", "source_path": "x", "category":
                              "doc", "content_sha": "s", "text": "y" * 30001}, 30000)
        assert "text" not in big and big["line_end"] is None, big
        print("PASS  queue: unit text + line_end inlined; oversized falls back to pointer")

        # lazy-aware: only the two auto-summarized nodes are linkable yet (and they
        # are already linked structurally, so no batch); stale nodes are skipped
        lc0 = LC.build_batches(proj, amg)
        assert lc0["eligible"] == 2 and lc0["batches"] == 0, lc0
        assert lc0["skipped_stale"] == 11, lc0
        print("PASS  candidates: stale (underived) nodes are skipped, not linked")

        # 2. stub builder -> apply-derived (the batch door, ONE call): checkpoint
        # parts consumed together, malformed items repaired/skipped, a torn part
        # quarantined, consumed files moved to work/applied/, re-run = no-op
        queue = json.loads((amg / "work" / "queue.json").read_text(encoding="utf-8"))
        stub = stub_derivation(queue["units"])
        (amg / "work" / "derived-stub-p01.json").write_text(
            json.dumps(stub[:7], ensure_ascii=False), encoding="utf-8")
        (amg / "work" / "derived-stub-p02.json").write_text(
            json.dumps(stub[7:], ensure_ascii=False), encoding="utf-8")
        (amg / "work" / "derived-torn-p01.json").write_text(
            '[{"id": "code:x", "summ', encoding="utf-8")   # a checkpoint torn mid-write
        r = RC.apply_derived(proj, amg)
        assert r["files"] == 2, r
        assert r.get("malformed_files") == ["derived-torn-p01.json"], r
        assert r["skipped_invalid"] == 2, r          # the no-id item + the bare string
        assert r["created"] == 1 and r["applied"] == 11 + 2, r  # units + swap + util extra
        assert r["skipped_missing"] == 1, r          # the ghost-target update
        assert r["missing_sample"] == ["code:ghost.py::nowhere"], r
        # the apply refreshed work/queue.json: every queued unit just got its summary,
        # so the file must be empty NOW, not after the next plan (status/sync-defer
        # read it raw, and a stale queue lied to them in the field)
        assert r["queue_remaining"] == 0, r
        q_after = json.loads((amg / "work" / "queue.json").read_text(encoding="utf-8"))
        assert q_after["units"] == [], "apply must drop derived units from the queue"
        assert not list((amg / "work").glob("derived-*.json")), "consumed files must move"
        assert (amg / "work" / "applied" / "derived-stub-p01.json").exists()
        assert (amg / "work" / "invalid" / "derived-torn-p01.json").exists()
        r2 = RC.apply_derived(proj, amg)
        assert r2["files"] == 0 and r2["applied"] == 0, r2   # resume re-run: cheap no-op
        print("PASS  batch door: one apply-derived call, parts consumed, torn part "
              "quarantined, queue refreshed, ghost target sampled")
        nodes = RC.load_nodes(gs.GraphStore(amg))
        assert "overview:build" in nodes and "hub:overview:build" not in nodes, \
            "the doubled category prefix must collapse"
        guide_tos = {e["to"] for e in nodes[GUIDE]["edges"]}
        assert f"{CORE}::Widget" in guide_tos, guide_tos      # mis-prefix re-bound
        assert f"{CORE}::top_fn" in guide_tos, guide_tos      # bare qualname re-bound
        top = nodes[f"{CORE}::top_fn"]
        assert top["confidence"] == 0.77, top.get("confidence")
        assert ("depends_on", f"{UTIL}::helper") in rels(f"{CORE}::top_fn")
        print("PASS  apply: swap repaired, prefix + bare name re-bound, 2 skipped, batch intact")

        # 3. synth stub -> the same batch door -> connectivity gate
        (amg / "work" / "derived-synth.json").write_text(
            json.dumps(synth_stub(), ensure_ascii=False), encoding="utf-8")
        rs = RC.apply_derived(proj, amg)
        assert rs["files"] == 1 and rs["created"] == 1, rs
        m = RC.graph_metrics(RC.load_nodes(gs.GraphStore(amg)))
        assert m["components"] == 1, m
        assert m["largest_component_share"] == 1.0, m
        assert m["dangling_structural"] == 0 and m["dangling_semantic"] == 0, m
        assert m["isolated_nodes"] == 0, m
        assert m["doc_files_without_documents"] == 0, m
        assert m["dangling_external_imports"] == 1, m         # import json — legitimate
        assert m["gate"] == "ok", m
        print("PASS  metrics: one component, 0 dangling, gate ok "
              f"(external imports={m['dangling_external_imports']})")

        # session-dump files are excluded from the doc metric by source-path
        # prefix (dialogue turns have no subject; they must not drown the doc signal)
        fake = {
            "doc:s.md::m1": {"_path": "nodes/doc/a.md", "status": "active",
                             "source_path": ".claude/amg/sessions/s.md",
                             "edges": [], "part_of": []},
            "doc:real.md::intro": {"_path": "nodes/doc/b.md", "status": "active",
                                   "source_path": "doc/real.md",
                                   "edges": [], "part_of": []},
        }
        mm = RC.graph_metrics(fake, None, session_prefix=".claude/amg/sessions/")
        assert mm["doc_files_without_documents"] == 1, mm
        assert mm["doc_files_without_documents_sample"] == ["doc/real.md"], mm
        assert RC.graph_metrics(fake, None)["doc_files_without_documents"] == 2
        assert RC.session_source_prefix(proj, {}, amg) == ".claude/amg/sessions/"
        print("PASS  metrics: session-dump doc files excluded from the doc metric")

        # dangling edges split by responsibility: a model-written target that names
        # no node is reported (count + sample) but never flips the gate — it is
        # inert and has no automatic remedy; a structural miss is the engine's own
        # correctness and still gates at max_dangling_internal (default 0)
        pairfx = {
            "code:a.py": {"_path": "nodes/code/a.md", "status": "active",
                          "edges": [{"rel": "defines", "to": "code:a.py::f", "w": 1.0,
                                     "origin": "structural"},
                                    {"rel": "relates_to", "to": "doc:ghost.md::x",
                                     "w": 0.5, "origin": "semantic"}],
                          "part_of": []},
            "code:a.py::f": {"_path": "nodes/code/f.md", "status": "active",
                             "edges": [], "part_of": []},
        }
        ms = RC.graph_metrics(pairfx, None)
        assert ms["dangling_semantic"] == 1 and ms["dangling_structural"] == 0, ms
        assert ms["dangling_semantic_sample"] == \
            ["code:a.py -relates_to-> doc:ghost.md::x"], ms
        assert ms["gate"] == "ok", "model-written dangling must not flip the gate"
        pairfx["code:a.py"]["edges"].append(
            {"rel": "calls", "to": "code:gone.py::h", "w": 0.7, "origin": "structural"})
        ms2 = RC.graph_metrics(pairfx, None)
        assert ms2["dangling_structural"] == 1 and ms2["gate"] == "attention", ms2
        # an ADR-style file: subsections document nothing themselves — the FILE
        # anchors through its subject section, so it is not counted undocumented
        adrfx = {
            "doc:adr.md::decision": {"_path": "nodes/doc/c.md", "status": "active",
                                     "source_path": "doc/adr.md",
                                     "edges": [{"rel": "documents", "to": "code:a.py",
                                                "w": 1.0}], "part_of": []},
            "doc:adr.md::context": {"_path": "nodes/doc/d.md", "status": "active",
                                    "source_path": "doc/adr.md",
                                    "edges": [], "part_of": []},
        }
        assert RC.graph_metrics(adrfx, None)["doc_files_without_documents"] == 0
        print("PASS  metrics: dangling split by origin (semantic never gates); "
              "doc metric aggregates per file")

        # 3b. the audit sweep: the built store is clean; planted anomalies —
        # a duplicate id (silent overwrite class), a path/id mismatch, an
        # unparsable file, an active-but-lagging node, a queue unit for a missing
        # node — are each surfaced with samples
        a = RC.audit(proj, amg)
        assert a["verdict"] == "clean" and a["anomalies"] == 0, a
        assert a["nodes_files"] == a["nodes_unique_ids"], a
        bad = Path(tempfile.mkdtemp(prefix="amg-audit-"))
        try:
            bamg = bad / ".claude" / "amg"
            (bamg / "nodes" / "code").mkdir(parents=True)
            (bamg / "journal").mkdir()
            (bamg / "config.yml").write_text("active: true\n", encoding="utf-8")
            meta_bad = {"id": "code:src/a.py::f", "type": "function",
                        "source_kind": "derived_from_file", "policy": "mirror",
                        "source_hash": "s1", "derived_from_hash": "s0",
                        "status": "active", "summary": "x",
                        "verification": {"status": "unverified", "method": "none"},
                        "provenance": {"kind": "code"}, "edges": [], "part_of": []}
            right = bamg / RC.node_relpath("code:src/a.py::f", "code")
            right.write_text(RC.serialize_node(dict(meta_bad), ""), encoding="utf-8")
            (bamg / "nodes" / "code" / "dup.md").write_text(
                RC.serialize_node(dict(meta_bad), ""), encoding="utf-8")
            (bamg / "nodes" / "code" / "torn.md").write_text("---\n:bad", encoding="utf-8")
            (bamg / "work").mkdir()
            (bamg / "work" / "queue.json").write_text(json.dumps(
                {"generated": "t", "units": [{"id": "code:gone.py", "content_sha": "z"}]}),
                encoding="utf-8")
            a2 = RC.audit(bad, bamg)
            assert a2["verdict"] == "attention", a2
            assert a2["duplicate_ids"]["count"] == 1, a2["duplicate_ids"]
            assert a2["path_mismatch"]["count"] == 1, a2["path_mismatch"]
            assert a2["unparsable"]["count"] == 1, a2["unparsable"]
            assert a2["status_inconsistent"]["count"] >= 1, a2["status_inconsistent"]
            assert a2["queue_lag"]["count"] == 1, a2["queue_lag"]
        finally:
            shutil.rmtree(bad, ignore_errors=True)
        print("PASS  audit: built store clean; planted anomalies surfaced with samples")

        # the one-file synthesis sheet: grouped summary rows + deterministic gap
        # material, so amg-synth never scans nodes/ in its own context
        si = LC.synth_input(amg)
        assert si["nodes"] == 15 and si["truncated"] is False, si
        sheet = json.loads((amg / "work" / "synth-input.json").read_text(encoding="utf-8"))
        sgroups = {g["subtree"]: g for g in sheet["groups"]}
        assert sgroups["src/pkg"]["count"] == 11 and "_hubs" in sgroups, list(sgroups)
        assert all(row["summary"] for row in sgroups["src/pkg"]["nodes"]), \
            "summaries ride inline"
        gaps = sheet["gaps"]
        # documented: CORE (by hub:build) and Widget (by the guide) — 9 of 11 remain
        assert gaps["undocumented_code_total"] == 9, gaps
        assert CORE not in gaps["undocumented_code"], gaps["undocumented_code"]
        assert f"{CORE}::Widget" not in gaps["undocumented_code"], gaps["undocumented_code"]
        assert gaps["drifted_doc_refs"] == [] and gaps["contradiction_pairs"] == [], gaps
        assert si["parts"] == 1 and not list((amg / "work").glob("synth-input-p*.json")), \
            "under the cap: one sheet, no part files"
        print("PASS  synth-input: one-file sheet with grouped summaries + gap material")

        # over the cap the sheet splits into WHOLE-GROUP parts (a small-window model
        # reads complete rows part by part); gaps ride in part 1 only, headers carry
        # {part, parts, groups_total}, and the plain sheet is removed
        cfgp = amg / "config.yml"
        cfg_text = cfgp.read_text(encoding="utf-8")
        cfgp.write_text(cfg_text + "\nlinker:\n  synth_input_max_chars: 1500\n",
                        encoding="utf-8")
        try:
            sp = LC.synth_input(amg)
            assert sp["parts"] > 1, sp
            parts = sorted((amg / "work").glob("synth-input-p*.json"))
            assert len(parts) == sp["parts"], (len(parts), sp)
            assert not (amg / "work" / "synth-input.json").exists(), \
                "no stale single sheet next to the parts"
            p1 = json.loads(parts[0].read_text(encoding="utf-8"))
            p2 = json.loads(parts[1].read_text(encoding="utf-8"))
            assert p1["part"] == 1 and p1["parts"] == sp["parts"] \
                and p1["groups_total"] == sp["groups"], p1
            assert "gaps" in p1 and "gaps" not in p2, "gap material rides in part 1 only"
            covered = [g["subtree"] for p in parts
                       for g in json.loads(p.read_text(encoding="utf-8"))["groups"]]
            assert sorted(covered) == sorted(sgroups), \
                "the parts together cover every group exactly once"
        finally:
            cfgp.write_text(cfg_text, encoding="utf-8")
        si = LC.synth_input(amg)                      # back under the default cap
        assert si["parts"] == 1 and (amg / "work" / "synth-input.json").exists()
        assert not list((amg / "work").glob("synth-input-p*.json")), \
            "stale parts are cleared when the sheet fits again"
        print("PASS  synth-input parts: whole-group split over the cap, part-1 gaps, clean revert")

        # 4. idempotency: the re-run bootstrap is a strict no-op
        s = RC.plan(proj, amg)
        assert s["added"] == s["changed"] == s["requeued_stale"] == 0, s
        assert s["edges_refreshed"] == 0 and s["queued_for_semantic"] == 0, s
        assert "leftover_derived" not in s, s
        print("PASS  idempotency: re-run bootstrap is a no-op")

        # an unapplied checkpoint part is surfaced by plan (the resume nudge: one
        # apply-derived call consumes it) and cleared once applied
        (amg / "work" / "derived-leftover-p01.json").write_text("[]", encoding="utf-8")
        s = RC.plan(proj, amg)
        assert s.get("leftover_derived") == 1, s
        r = RC.apply_derived(proj, amg)
        assert r["files"] == 1 and r["applied"] == 0, r
        assert "leftover_derived" not in RC.plan(proj, amg)
        print("PASS  leftover guard: plan surfaces unapplied parts; apply-derived clears")

        # 5. link candidates over the built graph (lexical fallback: embeddings off)
        lc = LC.build_batches(proj, amg)
        assert lc["mode"] == "lexical" and lc["batches"] >= 1, lc
        pairs = set()
        hubs_seen = set()
        for f in sorted((amg / "work").glob("link-batch-*.json")):
            batch = json.loads(f.read_text(encoding="utf-8"))
            hubs_seen |= {h["id"] for h in batch["hubs"]}
            for n in batch["nodes"]:
                for c in n["candidates"]:
                    pairs.add((n["id"], c["id"]))
        ping = f"{CORE}::Base.ping"
        assert (GUIDE, ping) in pairs or (ping, GUIDE) in pairs, \
            "the unlinked cross-domain pair with shared rare tokens must be nominated"
        assert (GUIDE, f"{CORE}::Widget") not in pairs \
            and (f"{CORE}::Widget", GUIDE) not in pairs, \
            "an already-linked pair must never be re-nominated"
        assert {"hub:build", "overview:build"} <= hubs_seen, hubs_seen
        hc = LC.hub_candidates(amg)
        assert hc["candidates"] >= 1 and hc["existing_hubs"] == 2, hc
        hdata = json.loads((amg / "work" / "hub-candidates.json").read_text(encoding="utf-8"))
        assert any(r["suggested_id"] == "hub:src-pkg" and r["members"] == 11
                   for r in hdata["candidates"]), hdata["candidates"]
        assert "hub:build" in hdata["existing_hubs"], hdata
        print("PASS  candidates: cross-domain pair nominated, linked pair excluded, "
              "hubs anchored to directories")

        # 5b. judged-pair memory: a linker part's {"judged": [...]} record retires a
        # FULLY covered batch to work/judged/, whose pairs are never re-nominated
        # (rejections remembered -> the pass converges); an under-covered (crashed)
        # batch stays pending and re-nominates — the safe direction.
        batch_file = amg / "work" / "link-batch-001.json"
        batch_ids = [n["id"] for n in
                     json.loads(batch_file.read_text(encoding="utf-8"))["nodes"]]
        (amg / "work" / "derived-links-001-p01.json").write_text(
            json.dumps([{"judged": batch_ids}]), encoding="utf-8")
        r = RC.apply_derived(proj, amg)
        assert r.get("judged_batches") == ["link-batch-001.json"], r
        assert r["applied"] == 0 and r["skipped_invalid"] == 0, r   # record != graph item
        assert (amg / "work" / "judged" / "link-batch-001.json").exists()
        assert not batch_file.exists()
        lc_after = LC.build_batches(proj, amg)
        assert lc_after["batches"] == 0, lc_after   # rejected pairs stay rejected
        shutil.rmtree(amg / "work" / "judged")      # re-open the judgments
        lc_reopen = LC.build_batches(proj, amg)
        assert lc_reopen["batches"] == 1, lc_reopen
        (amg / "work" / "derived-links-001-p01.json").write_text(
            json.dumps([{"judged": batch_ids[:1]}]), encoding="utf-8")
        r = RC.apply_derived(proj, amg)             # partial coverage: batch survives
        assert "judged_batches" not in r and batch_file.exists(), r
        assert LC.build_batches(proj, amg)["batches"] == 1, "crashed batch re-nominates"
        print("PASS  judged memory: full coverage retires the batch, partial re-nominates")

        # 5c. the scoped stray re-check (--isolated, the /amg relink path): a stray
        # with no resolved relation is re-nominated even after its pairs were judged
        # (rejections re-opened for strays only), and the SOURCES are limited to the
        # strays — connected nodes stay retired.
        batch_ids = [n["id"] for n in
                     json.loads(batch_file.read_text(encoding="utf-8"))["nodes"]]
        (amg / "work" / "derived-links-001-p01.json").write_text(
            json.dumps([{"judged": batch_ids}]), encoding="utf-8")
        RC.apply_derived(proj, amg)                 # retire the pending batch fully
        assert LC.build_batches(proj, amg)["batches"] == 0, "normal pass converged"
        store5 = gs.GraphStore(amg)
        guide_summary = RC.load_nodes(store5)[GUIDE]["summary"]
        with store5.lock():                         # a stray sharing GUIDE's tokens
            tx = store5.transaction()
            tx.write(RC.node_relpath("note:stray", "notes"), RC.serialize_node(
                {"id": "note:stray", "type": "note", "source_kind": "authored",
                 "policy": "authored", "source_hash": None, "derived_from_hash": None,
                 "summary": guide_summary, "status": "active",
                 "part_of": [], "edges": []}, ""))
            tx.commit()
        lc_new = LC.build_batches(proj, amg)        # a NEW node nominates normally
        assert lc_new["batches"] == 1, lc_new
        stray_batch = amg / "work" / "link-batch-001.json"
        stray_ids = [n["id"] for n in
                     json.loads(stray_batch.read_text(encoding="utf-8"))["nodes"]]
        (amg / "work" / "derived-links-001-p01.json").write_text(
            json.dumps([{"judged": stray_ids}]), encoding="utf-8")
        RC.apply_derived(proj, amg)                 # ... and gets judged away
        assert LC.build_batches(proj, amg)["batches"] == 0, "stray judged -> converged again"
        iso = LC.build_batches(proj, amg, isolated_only=True)
        assert iso["batches"] == 1 and iso.get("isolated_sources", 0) >= 1, iso
        iso_nodes = [n for f in sorted((amg / "work").glob("link-batch-*.json"))
                     for n in json.loads(f.read_text(encoding="utf-8"))["nodes"]]
        assert {n["id"] for n in iso_nodes} == {"note:stray"}, \
            "sources limited to the strays (connected nodes stay retired)"
        assert any(c["id"] == GUIDE for n in iso_nodes for c in n["candidates"]), \
            "the stray's judged pair is re-opened"
        shutil.rmtree(amg / "work" / "judged", ignore_errors=True)
        for f in (amg / "work").glob("link-batch-*.json"):
            f.unlink()                              # leave no pending batches behind
        print("PASS  isolated re-check: strays re-nominated past judged memory, sources scoped")

        # 6. derivation cache: wipe the graph, rebuild, restore verbatim
        summaries_before = {nid: n["summary"] for nid, n in
                            RC.load_nodes(gs.GraphStore(amg)).items()
                            if n.get("source_kind") == "derived_from_file"}
        shutil.rmtree(amg / "nodes")
        shutil.rmtree(amg / "journal")
        shutil.rmtree(amg / "work", ignore_errors=True)
        s = RC.plan(proj, amg)                       # fresh skeleton, all stale
        assert s["added"] == 13 and s["queued_for_semantic"] == 11, s
        assert s["auto_summarized"] == 2, s          # trivial units re-derive by code
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

        # a trivial unit with an EARNED cached derivation queues instead of the
        # template: the cache restores the judged summary verbatim (cache wins)
        sha_helper = RC.load_nodes(gs.GraphStore(amg))[f"{UTIL}::helper"]["source_hash"]
        gs.atomic_write_text(
            RC._derivation_cache_path(amg, sha_helper),
            json.dumps({"contract": RC.DERIVATION_CONTRACT, "lang": "en",
                        "items": [{"id": f"{UTIL}::helper", "content_sha": sha_helper,
                                   "summary": "Cached judged summary."}]}))
        shutil.rmtree(amg / "nodes")
        shutil.rmtree(amg / "journal")
        shutil.rmtree(amg / "work", ignore_errors=True)
        s = RC.plan(proj, amg)
        assert s["auto_summarized"] == 1 and s["queued_for_semantic"] == 12, s
        c = RC.apply_cached(proj, amg)
        assert c["restored_units"] == 12 and c["remaining"] == 0, c
        nodes = RC.load_nodes(gs.GraphStore(amg))
        assert nodes[f"{UTIL}::helper"]["summary"] == "Cached judged summary.", \
            "the earned cached derivation must beat the trivial template"
        print("PASS  trivial+cache: an earned cached summary wins over the template")

        # a changed working language must MISS by key, never restore foreign summaries
        (amg / "config.yml").write_text(
            "active: true\nworking_language: ru\nmirror_path: [src, doc, data]\n"
            "trivial_unit_max_lines: 3\n",
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
