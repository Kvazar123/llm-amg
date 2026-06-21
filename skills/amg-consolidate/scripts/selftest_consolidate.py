#!/usr/bin/env python3
"""
selftest_consolidate.py — proves the consolidation loop is correct and safe.

Checks:
  1. weights : co-activated edges strengthen, unused edges decay, faded edges are
               pruned, part_of stays normalized, and the co-activation log is rotated
               into the archive (not lost).
  2. plan    : a branch over its budget is detected.
  3. apply   : merge / summarize_episodes / introduce_subhub / shorten / retire are
               applied transactionally; originals are ARCHIVED (reversible); inbound
               edges are redirected; the store verifies clean.
  4. safety  : a safe compaction does NOT reduce retrieval recall (measured with the
               eval harness before and after) — compaction is gated by the number.

Run:  python selftest_consolidate.py
"""
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                                          # consolidate, graph_store
sys.path.insert(0, str(HERE.parents[1] / "amg-retrieve" / "scripts"))  # retrieve, eval_retrieval

import consolidate as C
import graph_store as gs
import retrieve as R
import eval_retrieval as E


def setup_project() -> Path:
    proj = Path(tempfile.mkdtemp(prefix="amg-cons-"))
    amg = proj / ".claude" / "amg"
    amg.mkdir(parents=True)
    E.build_demo_store(amg)                 # nodes + config.yml + cases.json
    return proj


def edit_node(store, nid, mutate):
    nodes = C.load_nodes(store)
    n = nodes[nid]
    body = n.get("_body", "")
    mutate(n)
    gs.atomic_write_text(store.abspath(n["_path"]), C.serialize(n, body))


def edge_of(store, nid, to):
    n = C.load_nodes(store)[nid]
    for e in n.get("edges") or []:
        if e.get("to") == to:
            return e
    return None


def write_node(store, nid, kind, meta_extra, body=""):
    # same slug rule as reconcile.node_relpath / consolidate.newpath (filename-safe)
    slug = re.sub(r"[^\w.-]+", "_", nid.split(":", 1)[-1]).strip("_")[:48] or "node"
    h = gs.sha256_text(nid)[:8]
    meta = {"id": nid, "status": "active", "updated": "2026-01-01T00:00:00"}
    meta.update(meta_extra)
    gs.atomic_write_text(store.root / "nodes" / kind / f"{slug}-{h}.md",
                         C.serialize(meta, body))


def gold_recall(proj, amg):
    case = json.loads((amg / "cases.json").read_text())[0]
    cfg = R.load_config(amg)
    return E.evaluate_case(amg, case, cfg)["amg"]["recall"]


# --------------------------------------------------------------------------- #

def test_weights(proj):
    """The improved rule (Stage 14). off (default): only accumulate coact, w untouched,
    usage.log left intact. on: an edge CO-USED in an accepted session is REWARDED by the
    discriminative headroom rate*(1-w); an edge merely SURFACED (co-activation) but not
    used DECAYS (and prunes if it falls below threshold); an edge neither surfaced nor
    used is UNTOUCHED; usage.log is consumed (archived)."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    cfg_path = amg / "config.yml"
    original_cfg = cfg_path.read_text(encoding="utf-8")
    work = store.root / "work"
    work.mkdir(exist_ok=True)

    # A source node with three edges: A (will be co-used -> reward), B (surfaced but
    # unused -> decay/prune; starts low so one decay prunes it), C (neither -> untouched).
    src = "notes:wtest/src"
    A, Bn, Cn = "notes:wtest/a", "notes:wtest/b", "notes:wtest/c"
    write_node(store, src, "notes", {"type": "note", "summary": "weights test source",
        "edges": [{"rel": "relates_to", "to": A, "w": 0.5, "coact": 0},
                  {"rel": "relates_to", "to": Bn, "w": 0.06, "coact": 0},
                  {"rel": "relates_to", "to": Cn, "w": 0.5, "coact": 0}]})
    for t in (A, Bn, Cn):
        write_node(store, t, "notes", {"type": "note", "summary": t})

    def write_logs():
        (work / "coactivation.log").write_text(           # A and B are surfaced together
            json.dumps({"coactivated": [[src, A], [src, Bn]]}) + "\n")
        (work / "usage.log").write_text(                  # only A is co-used (accepted)
            json.dumps({"outcome": "completed", "used": [src, A]}) + "\n")

    # Phase 1 — off (default): coact accumulates on the surfaced edges, w untouched,
    # coactivation rotated, usage.log left intact (it is the substrate, read only when on).
    write_logs()
    res = C.fold_weights(proj)
    assert res["hebbian_applied"] is False, res
    assert edge_of(store, src, A)["coact"] == 1 and edge_of(store, src, Bn)["coact"] == 1
    assert edge_of(store, src, A)["w"] == 0.5 and edge_of(store, src, Bn)["w"] == 0.06, "w untouched off"
    assert not (work / "coactivation.log").exists(), "coactivation rotated (consumed)"
    assert (work / "usage.log").exists(), "usage.log left intact while off (substrate)"

    # Phase 2 — on: A rewarded (headroom), B surfaced-unused -> decays below prune, C untouched.
    cfg_path.write_text(original_cfg + "\nweights:\n  apply_hebbian: true\n", encoding="utf-8")
    try:
        write_logs()
        res = C.fold_weights(proj)
        assert res["hebbian_applied"] is True, res
        assert res["rewarded_edges"] == 1 and res["decayed_edges"] == 1, res
        wa = edge_of(store, src, A)["w"]
        assert abs(wa - 0.55) < 1e-6, ("reward = 0.5 + 0.1*(1-0.5)", wa)
        assert edge_of(store, src, Bn) is None, "surfaced-unused faded edge pruned (0.06-0.02<0.05)"
        assert edge_of(store, src, Cn)["w"] == 0.5, "neither surfaced nor used -> untouched"
        assert not (work / "usage.log").exists(), "usage.log consumed when the rule applies"
        assert list((store.root / "archive").glob("usage-*.log")), "usage.log archived"
    finally:
        cfg_path.write_text(original_cfg, encoding="utf-8")
    assert list((store.root / "archive").glob("coactivation-*.log")), "rotated coactivation archived"
    print("PASS  weights: off accumulates coact only; on = outcome reward (headroom) + "
          "exposure decay/prune; untouched edge stable; usage consumed")


def test_plan(proj):
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    edit_node(store, "hub:billing", lambda n: n.__setitem__("branch_budget", 2))
    C.make_plan(proj)
    plan = json.loads((amg / "work" / "consolidation-plan.json").read_text())
    hubs = [b["hub"] for b in plan["over_budget_branches"]]
    assert "hub:billing" in hubs, "over-budget branch should be flagged"
    print(f"PASS  plan: detected over-budget branch (members > budget)")


def test_near_dup_scope(proj):
    """near_duplicates is restricted to episodic, non-source-derived nodes (§1.27):
    two source-derived code nodes with identical summaries are NOT flagged (merging
    them is futile — reconcile recreates them), while two authored notes ARE."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    dup = "identical duplicate summary text for the near dup scope test"
    write_node(store, "code:nd/a.py::f", "code",
               {"type": "function", "source_kind": "derived_from_file", "summary": dup})
    write_node(store, "code:nd/b.py::g", "code",
               {"type": "function", "source_kind": "derived_from_file", "summary": dup})
    write_node(store, "notes:nd/a", "notes",
               {"type": "note", "source_kind": "authored", "summary": dup})
    write_node(store, "notes:nd/b", "notes",
               {"type": "note", "source_kind": "authored", "summary": dup})
    C.make_plan(proj)
    plan = json.loads((amg / "work" / "consolidation-plan.json").read_text())
    pairs = {frozenset((d["a"], d["b"])) for d in plan["near_duplicates"]}
    assert frozenset(("notes:nd/a", "notes:nd/b")) in pairs, "episodic notes must be flagged"
    assert frozenset(("code:nd/a.py::f", "code:nd/b.py::g")) not in pairs, \
        "source-derived code nodes must NOT be near-dup candidates"
    print("PASS  near-dup: candidates restricted to episodic non-derived nodes (§1.27)")


def test_apply_and_recall(proj):
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)

    recall_before = gold_recall(proj, amg)

    # Throwaway nodes to exercise every handler without touching gold.
    write_node(store, "notes:tmp/dup-a", "notes", {"type": "note", "summary": "logging config"})
    write_node(store, "notes:tmp/dup-b", "notes", {"type": "note", "summary": "logging config dup"})
    for i in (1, 2, 3):
        write_node(store, f"notes:ep/{i}", "notes", {"type": "note", "summary": f"episode {i}"})
    # A node pointing at an episode -> its edge must be redirected on summarize.
    write_node(store, "notes:tmp/ref", "notes",
               {"type": "note", "summary": "refers to ep1",
                "edges": [{"rel": "relates_to", "to": "notes:ep/1", "w": 0.5}]})

    actions = [
        {"action": "merge", "keep_id": "notes:tmp/dup-a",
         "drop_ids": ["notes:tmp/dup-b"], "summary": "merged duplicates"},
        {"action": "summarize_episodes", "new_id": "notes:summary/eps",
         "summary": "three episodes condensed", "body": "Summary of eps 1-3.",
         "archive_ids": ["notes:ep/1", "notes:ep/2", "notes:ep/3"]},
        {"action": "introduce_subhub", "hub_id": "hub:misc", "summary": "misc notes",
         "member_ids": ["notes:tmp/dup-a"]},
        {"action": "shorten", "id": "doc:doc/faq.md::payments",
         "summary": "FAQ (condensed)", "body": "See support docs."},
        {"action": "retire", "id": "doc:doc/refunds.md::overview"},
    ]
    (amg / "work").mkdir(exist_ok=True)
    (amg / "work" / "actions.json").write_text(json.dumps(actions))
    counts = C.apply_actions(proj, amg / "work" / "actions.json")

    nodes = C.load_nodes(store)
    # archived + removed
    assert "notes:tmp/dup-b" not in nodes and "doc:doc/refunds.md::overview" not in nodes
    assert all(f"notes:ep/{i}" not in nodes for i in (1, 2, 3))
    arch = {p.name for p in (store.root / "archive").iterdir()}
    assert any("dup-b" in a for a in arch) and any("refunds" in a for a in arch)
    assert any("ep_1" in a or "ep/1" in a or "1-" in a for a in arch)  # episode archived
    # survivors / new nodes; created nodes use the normalized source_kind
    # taxonomy (derived_from_file / synthesized / authored — never `derived`)
    assert "notes:summary/eps" in nodes and "hub:misc" in nodes
    assert nodes["notes:summary/eps"]["source_kind"] == "synthesized"
    assert nodes["hub:misc"]["source_kind"] == "synthesized"
    # inbound edge redirected to the summary node
    ref = nodes["notes:tmp/ref"]
    assert any(e.get("to") == "notes:summary/eps" for e in ref["edges"]), "edge redirected"
    # subhub membership repointed
    cc = nodes["notes:tmp/dup-a"]
    assert any(p.get("topic") == "hub:misc" for p in cc.get("part_of") or []), "membership repointed"
    # shorten archived the full version
    assert any("faq" in a and a.endswith(".full") for a in arch), "full text archived"

    # store integrity
    problems = store.verify(repair=False)
    assert all(not v for v in problems.values()), f"store not clean: {problems}"

    # SAFETY: compaction did not reduce recall
    recall_after = gold_recall(proj, amg)
    assert recall_after >= recall_before, \
        f"compaction hurt recall: {recall_before} -> {recall_after}"
    print(f"PASS  apply: {counts}")
    print(f"PASS  safety: recall preserved across compaction "
          f"({recall_before:.2f} -> {recall_after:.2f}); store verifies clean")


def test_protect_and_force(proj):
    """A protected type (decision/adr) is not shortened/retired without force (1.11)."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    write_node(store, "notes:dec/keep", "notes",
               {"type": "decision", "summary": "keep me", "status": "active"},
               body="Important decision rationale.")
    (amg / "work").mkdir(exist_ok=True)

    (amg / "work" / "a1.json").write_text(json.dumps(
        [{"action": "shorten", "id": "notes:dec/keep", "summary": "x", "body": "y"}]))
    counts = C.apply_actions(proj, amg / "work" / "a1.json")
    assert counts.get("skipped_protected") == 1, counts
    n = C.load_nodes(store)["notes:dec/keep"]
    assert n["_body"].strip() == "Important decision rationale.", "decision body must be intact"

    (amg / "work" / "a2.json").write_text(json.dumps(
        [{"action": "shorten", "id": "notes:dec/keep", "summary": "x", "body": "y",
          "force": True}]))
    counts = C.apply_actions(proj, amg / "work" / "a2.json")
    n = C.load_nodes(store)["notes:dec/keep"]
    assert counts.get("shorten") == 1 and n["_body"].strip() == "y", n
    print("PASS  protect: decision not shortened without force; force overrides")


def test_centrality_protect():
    """A node at max degree centrality (> protect_min_centrality) is protected in code."""
    cmp_cfg = C.DEFAULTS["compaction"]
    degree = {"n1": 4, "n2": 1}
    assert C._is_protected({"id": "n1", "type": "note"}, degree, 4, cmp_cfg), \
        "max-centrality node must be protected even as a plain note"
    assert not C._is_protected({"id": "n2", "type": "note"}, degree, 4, cmp_cfg)
    print("PASS  protect: high-centrality node is protected by code")


def test_enabled_gate(proj):
    """compaction.enabled:false blocks compression actions and over-budget flagging (1.8)."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    cfg_path = amg / "config.yml"
    original = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(original + "\ncompaction:\n  enabled: false\n", encoding="utf-8")
    try:
        write_node(store, "notes:tmp/long", "notes",
                   {"type": "note", "summary": "long one"}, body="A B C D E F.")
        (amg / "work").mkdir(exist_ok=True)
        (amg / "work" / "ag.json").write_text(json.dumps(
            [{"action": "shorten", "id": "notes:tmp/long", "summary": "x", "body": "y"}]))
        counts = C.apply_actions(proj, amg / "work" / "ag.json")
        assert counts.get("skipped_disabled") == 1, counts
        n = C.load_nodes(store)["notes:tmp/long"]
        assert n["_body"].strip() == "A B C D E F.", "compression must be blocked when disabled"

        edit_node(store, "hub:billing", lambda nd: nd.__setitem__("branch_budget", 1))
        C.make_plan(proj)
        plan = json.loads((amg / "work" / "consolidation-plan.json").read_text())
        assert plan["over_budget_branches"] == [], "disabled compaction flags no branches"
    finally:
        cfg_path.write_text(original, encoding="utf-8")
    print("PASS  enabled: enabled:false blocks compression and over-budget flagging")


def test_shorten_idempotent(proj):
    """A repeated apply of the same shorten must not overwrite the archived original (1.10)."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    write_node(store, "notes:tmp/orig", "notes",
               {"type": "note", "summary": "orig summary"}, body="ORIGINAL FULL TEXT.")
    (amg / "work").mkdir(exist_ok=True)
    (amg / "work" / "s.json").write_text(json.dumps(
        [{"action": "shorten", "id": "notes:tmp/orig", "summary": "short", "body": "shortened."}]))
    C.apply_actions(proj, amg / "work" / "s.json")
    C.apply_actions(proj, amg / "work" / "s.json")          # apply a SECOND time
    full = list((store.root / "archive").glob("*orig*.full"))
    assert full, "full original must be archived"
    assert "ORIGINAL FULL TEXT." in full[0].read_text(encoding="utf-8"), \
        "repeated apply must not overwrite the archived original with the shortened body"
    print("PASS  shorten: repeated apply preserves the archived original (idempotent)")


def test_merge_quality(proj):
    """merge folds edges (max w, summed coact), combines part_of, drops self-edges,
    and dedups a neighbor's edges after redirect (task 9 + audit 1.22)."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    write_node(store, "notes:m/keep", "notes",
               {"type": "note", "summary": "keep",
                "edges": [{"rel": "relates_to", "to": "notes:m/x", "w": 0.3, "coact": 1}],
                "part_of": [{"topic": "hub:b", "w": 0.5}]})
    write_node(store, "notes:m/drop", "notes",
               {"type": "note", "summary": "drop",
                "edges": [{"rel": "relates_to", "to": "notes:m/x", "w": 0.7, "coact": 2},
                          {"rel": "relates_to", "to": "notes:m/keep", "w": 0.9, "coact": 4}],
                "part_of": [{"topic": "hub:a", "w": 0.5}]})
    write_node(store, "notes:m/x", "notes", {"type": "note", "summary": "x"})
    write_node(store, "notes:m/y", "notes",
               {"type": "note", "summary": "y",
                "edges": [{"rel": "relates_to", "to": "notes:m/keep", "w": 0.4, "coact": 1},
                          {"rel": "relates_to", "to": "notes:m/drop", "w": 0.6, "coact": 2}]})
    (amg / "work").mkdir(exist_ok=True)
    # force:true isolates merge mechanics from the protection gate (drop is
    # highly connected in this shared graph; protection is tested separately).
    (amg / "work" / "m.json").write_text(json.dumps(
        [{"action": "merge", "keep_id": "notes:m/keep", "drop_ids": ["notes:m/drop"],
          "summary": "merged", "force": True}]))
    C.apply_actions(proj, amg / "work" / "m.json")

    nodes = C.load_nodes(store)
    assert "notes:m/drop" not in nodes, "dropped node must be archived"
    keep = nodes["notes:m/keep"]
    ex = {(e["rel"], e["to"]): e for e in keep["edges"]}
    xe = ex[("relates_to", "notes:m/x")]
    assert xe["w"] == 0.7 and xe["coact"] == 3, xe          # max(0.3,0.7), 1+2
    assert all(e["to"] != "notes:m/keep" for e in keep["edges"]), "no self-edge"
    assert {p["topic"] for p in keep["part_of"]} == {"hub:a", "hub:b"}, keep["part_of"]
    ye = [e for e in nodes["notes:m/y"]["edges"] if e["to"] == "notes:m/keep"]
    assert len(ye) == 1 and ye[0]["w"] == 0.6 and ye[0]["coact"] == 3, ye  # deduped
    print("PASS  merge: max w + summed coact, part_of combined, no self-edge, neighbor deduped")


def test_subhub_keeps_memberships(proj):
    """introduce_subhub rewrites only the parent topic; other memberships survive (1.21)."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    write_node(store, "notes:sh/member", "notes",
               {"type": "note", "summary": "member",
                "part_of": [{"topic": "hub:parent", "w": 0.6},
                            {"topic": "hub:other", "w": 0.4}]})
    (amg / "work").mkdir(exist_ok=True)
    (amg / "work" / "sh.json").write_text(json.dumps(
        [{"action": "introduce_subhub", "hub_id": "hub:sub", "summary": "sub",
          "parent_topic": "hub:parent", "member_ids": ["notes:sh/member"]}]))
    C.apply_actions(proj, amg / "work" / "sh.json")
    topics = {p["topic"]: p["w"] for p in C.load_nodes(store)["notes:sh/member"]["part_of"]}
    assert "hub:sub" in topics and topics.get("hub:other") == 0.4, topics
    assert "hub:parent" not in topics, "parent topic must be replaced by the sub-hub"
    print("PASS  subhub: parent topic -> sub-hub, other memberships preserved")


def test_consolidation_nodes_schema(proj):
    """summarize_episodes / introduce_subhub nodes match the synthesized canon and
    land in _hubs (task 7, 2.8 p.5)."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    for i in (1, 2):
        write_node(store, f"notes:se/{i}", "notes", {"type": "note", "summary": f"ep{i}"})
    (amg / "work").mkdir(exist_ok=True)
    (amg / "work" / "se.json").write_text(json.dumps([
        {"action": "summarize_episodes", "new_id": "notes:se/sum", "summary": "sum",
         "body": "condensed", "archive_ids": ["notes:se/1", "notes:se/2"]},
        {"action": "introduce_subhub", "hub_id": "hub:se", "summary": "subhub"},
    ]))
    C.apply_actions(proj, amg / "work" / "se.json")
    nodes = C.load_nodes(store)
    for nid in ("notes:se/sum", "hub:se"):
        n = nodes[nid]
        assert n["source_kind"] == "synthesized" and n["policy"] == "authored", n
        assert n["source_hash"] is None and n["derived_from_hash"] is None, n
        assert n["lang"] == "ru", n                          # demo working_language
        assert n["_path"].startswith("nodes/_hubs/"), n["_path"]
    print("PASS  schema: consolidation nodes are synthesized/authored canon, in _hubs")


def test_grounded_inbound():
    """salience counts an inbound documents/implements/specifies edge as provenance (task 8)."""
    cfg = C.DEFAULTS
    node = {"id": "n", "type": "note", "source_kind": "authored", "edges": []}
    s_off = C.salience(node, 0, 1, cfg, grounded_inbound=False)
    s_on = C.salience(node, 0, 1, cfg, grounded_inbound=True)
    assert s_on > s_off, (s_off, s_on)            # grounded 0.4 -> 1.0 (weight 0.10)
    print("PASS  grounded: an inbound grounding edge raises salience (provenance)")


def test_branch_downward(proj):
    """A hub reaches its branch via containment edges even when leaf part_of is the
    directory string (not the hub) — so over_budget is computable on a real graph (1.20)."""
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    write_node(store, "hub:svc", "_hubs",
               {"type": "hub", "source_kind": "synthesized",
                "edges": [{"rel": "documents", "to": "code:svc/m.py", "w": 0.6}]})
    write_node(store, "code:svc/m.py", "code",
               {"type": "module", "source_path": "svc/m.py",
                "edges": [{"rel": "defines", "to": "code:svc/m.py::f", "w": 1.0},
                          {"rel": "defines", "to": "code:svc/m.py::g", "w": 1.0}],
                "part_of": [{"topic": "svc", "w": 1.0}]})    # primary home is the DIR string
    for fn in ("f", "g"):
        write_node(store, f"code:svc/m.py::{fn}", "code",
                   {"type": "function", "source_path": "svc/m.py",
                    "part_of": [{"topic": "svc", "w": 1.0}]})
    branch = set(C._branch_members(C.load_nodes(store)).get("hub:svc", []))
    assert branch >= {"code:svc/m.py", "code:svc/m.py::f", "code:svc/m.py::g"}, branch
    print("PASS  branch: hub reaches its branch downward via containment edges (1.20)")


def _armed_demo(on_fail):
    """A fresh demo store with the eval gate armed at an ABSOLUTE cases path (the demo
    writes resolvable cases.json into the graph root)."""
    proj = Path(tempfile.mkdtemp(prefix="amg-gate-"))
    amg = proj / ".claude" / "amg"
    amg.mkdir(parents=True)
    E.build_demo_store(amg)
    cp = amg / "config.yml"
    cp.write_text(cp.read_text() + "eval_gate:\n  enabled: true\n"
                  f"  cases: {(amg / 'cases.json').as_posix()}\n  on_fail: {on_fail}\n",
                  encoding="utf-8")
    return proj, amg


def _write_actions(amg, actions):
    (amg / "work").mkdir(exist_ok=True)
    (amg / "work" / "a.json").write_text(json.dumps(actions))
    return amg / "work" / "a.json"


def test_eval_gate():
    """A harmful compaction is rejected on the graph CLONE before the real graph is
    touched; a safe one applies; on_fail:warn applies anyway and records the drop."""
    gold = "code:src/billing.py::compute_total"      # a multi-hop gold node in the demo

    # (1) harmful: retire a gold node -> pack recall drops -> rejected, graph intact
    proj, amg = _armed_demo("reject")
    try:
        res = C.apply_actions(proj, _write_actions(amg, [
            {"action": "retire", "id": gold, "force": True}]))
        assert res.get("gate") == "rejected", res
        assert gold in C.load_nodes(gs.GraphStore(amg)), "reject must not touch the real graph"
        rep = json.loads((amg / "work" / "eval-gate-report.json").read_text())
        assert rep["status"] == "rejected" and rep["recall_delta"] < 0, rep
        assert any(gold in r["lost_gold"] for r in rep["regressions"]), rep
        assert any("retire" in c["actions"]
                   for r in rep["regressions"] for c in r["attribution"]), rep
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    # (2) safe: retire a NON-gold distractor -> recall holds -> applied, gate ok
    proj, amg = _armed_demo("reject")
    try:
        res = C.apply_actions(proj, _write_actions(amg, [
            {"action": "retire", "id": "doc:doc/refunds.md::overview"}]))
        assert res.get("gate") == "ok" and res.get("retire") == 1, res
        assert "doc:doc/refunds.md::overview" not in C.load_nodes(gs.GraphStore(amg))
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    # (3) on_fail: warn -> applied despite the drop, regression recorded
    proj, amg = _armed_demo("warn")
    try:
        res = C.apply_actions(proj, _write_actions(amg, [
            {"action": "retire", "id": gold, "force": True}]))
        assert res.get("gate") == "warn" and res.get("retire") == 1, res
        assert gold not in C.load_nodes(gs.GraphStore(amg)), "warn applies anyway"
        rep = json.loads((amg / "work" / "eval-gate-report.json").read_text())
        assert rep["status"] == "warn" and any(gold in r["lost_gold"] for r in rep["regressions"])
    finally:
        shutil.rmtree(proj, ignore_errors=True)
    print("PASS  eval-gate: harmful rejected (graph intact, attributed); safe applied; warn applies + records")


def test_gate_robust():
    """No/dead cases must SKIP the gate (apply proceeds) — never a false reject. The
    default cases path is project-relative and unresolved in a bare temp project."""
    proj = Path(tempfile.mkdtemp(prefix="amg-rob-"))
    amg = proj / ".claude" / "amg"
    amg.mkdir(parents=True)
    E.build_demo_store(amg)
    cp = amg / "config.yml"
    cp.write_text(cp.read_text() + "eval_gate:\n  enabled: true\n"
                  "  cases: .claude/skills/amg-retrieve/evals/cases.json\n  on_fail: reject\n",
                  encoding="utf-8")
    gold = "code:src/billing.py::compute_total"
    try:
        res = C.apply_actions(proj, _write_actions(amg, [
            {"action": "retire", "id": gold, "force": True}]))
        assert res.get("gate") == "skipped", res
        assert gold not in C.load_nodes(gs.GraphStore(amg)), "skip must not block compaction"
    finally:
        shutil.rmtree(proj, ignore_errors=True)
    print("PASS  eval-gate: unresolved cases -> skip + apply (no false reject when disarmed)")


def test_hebbian_demo():
    """Mechanism correctness of the improved rule (NOT a default-on justification): a gold
    node behind a WEAK edge is missed with static weights (hop-recall 0) and recovered when
    the CO-USED pair is recorded in usage.log (outcome-gated reward) while the unused sibling
    is only surfaced in coactivation.log (decays) — apply_hebbian on (hop-recall 1). Negative
    control: on the hand-optimal demo weights, the rule must not reduce recall."""
    seed = "code:src/jobs.py::nightly_charge"
    gold = "notes:decisions/backoff"
    sib = "code:src/jobs.py::send_receipt"
    # positive control
    proj = Path(tempfile.mkdtemp(prefix="amg-heb-"))
    amg = proj / ".claude" / "amg"
    amg.mkdir(parents=True)
    try:
        cases = E.build_hebbian_demo(amg)
        off = E.run(amg, cases, R.load_config(amg))["aggregate"]
        assert off["amg"]["hop_recall"] == 0.0, off       # weak edge: gold not reached
        work = amg / "work"
        work.mkdir(exist_ok=True)
        cp = amg / "config.yml"
        cp.write_text(cp.read_text() + "weights:\n  apply_hebbian: true\n", encoding="utf-8")
        applied = False
        for _ in range(5):                                 # each fold consumes the logs
            (work / "coactivation.log").write_text(        # both edges surfaced
                json.dumps({"coactivated": [[seed, gold], [seed, sib]]}) + "\n")
            (work / "usage.log").write_text(               # only seed+gold co-USED (accepted)
                json.dumps({"outcome": "completed", "used": [seed, gold]}) + "\n")
            applied = C.fold_weights(proj)["hebbian_applied"] or applied
        assert applied is True
        on = E.run(amg, cases, R.load_config(amg))["aggregate"]
        assert on["amg"]["hop_recall"] == 1.0, on          # reward recovered it; sibling faded
    finally:
        shutil.rmtree(proj, ignore_errors=True)

    # negative control: the rule must not hurt recall on already-good weights
    proj2 = Path(tempfile.mkdtemp(prefix="amg-negheb-"))
    amg2 = proj2 / ".claude" / "amg"
    amg2.mkdir(parents=True)
    try:
        cases2 = E.build_demo_store(amg2)
        before = E.run(amg2, cases2, R.load_config(amg2))["aggregate"]["amg"]["recall"]
        R.retrieve(amg2, cases2[0]["query"], config=R.load_config(amg2),
                   write_pack=False, log_coactivation=True)       # real co-activation (exposure)
        (amg2 / "work").mkdir(exist_ok=True)
        (amg2 / "work" / "usage.log").write_text(                  # reward the (already strong) gold
            json.dumps({"outcome": "completed", "used": cases2[0]["gold_ids"]}) + "\n")
        cp2 = amg2 / "config.yml"
        cp2.write_text(cp2.read_text() + "weights:\n  apply_hebbian: true\n", encoding="utf-8")
        C.fold_weights(proj2)
        after = E.run(amg2, cases2, R.load_config(amg2))["aggregate"]["amg"]["recall"]
        assert after >= before, (before, after)
    finally:
        shutil.rmtree(proj2, ignore_errors=True)
    print("PASS  hebbian: weak-edge gold recovered off->on (hop 0->1) via usage reward; "
          "good weights keep recall")


if __name__ == "__main__":
    proj = setup_project()
    try:
        test_weights(proj)
        test_plan(proj)
        test_near_dup_scope(proj)
        test_apply_and_recall(proj)
        test_protect_and_force(proj)
        test_centrality_protect()
        test_enabled_gate(proj)
        test_shorten_idempotent(proj)
        test_merge_quality(proj)
        test_subhub_keeps_memberships(proj)
        test_consolidation_nodes_schema(proj)
        test_grounded_inbound()
        test_branch_downward(proj)
        test_eval_gate()
        test_gate_robust()
        test_hebbian_demo()
        print("\nALL CONSOLIDATION CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
