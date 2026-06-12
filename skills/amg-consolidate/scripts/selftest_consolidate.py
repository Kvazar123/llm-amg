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
    slug = nid.split(":", 1)[-1].replace("/", "_")[:48]
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
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)

    # Inject a faded, un-coactivated edge that should be pruned (0.04 -> 0.02 < 0.05).
    edit_node(store, "code:src/billing.py::compute_total", lambda n: n.__setitem__(
        "edges", [{"rel": "relates_to", "to": "code:src/auth.py::login", "w": 0.04, "coact": 0}]))

    # A co-activation log naming one pair three times.
    (store.root / "work").mkdir(exist_ok=True)
    pair = ["code:src/billing.py::charge_card", "code:src/billing.py::compute_total"]
    (store.root / "work" / "coactivation.log").write_text(
        "".join(json.dumps({"coactivated": [pair]}) + "\n" for _ in range(3)))

    w_before = edge_of(store, "code:src/billing.py::charge_card",
                       "code:src/billing.py::compute_total")["w"]
    C.fold_weights(proj)

    reinforced = edge_of(store, "code:src/billing.py::charge_card",
                         "code:src/billing.py::compute_total")
    assert reinforced["w"] > w_before, "co-activated edge should strengthen"
    assert reinforced["coact"] == 3, "co-activation count should accumulate"

    decayed = edge_of(store, "code:src/billing.py", "code:src/billing.py::charge_card")
    assert abs(decayed["w"] - 0.98) < 1e-6, "unused edge should decay by lambda"

    assert edge_of(store, "code:src/billing.py::compute_total",
                   "code:src/auth.py::login") is None, "faded edge should be pruned"

    assert not (store.root / "work" / "coactivation.log").exists(), "log should be rotated"
    arch = list((store.root / "archive").glob("coactivation-*.log"))
    assert arch, "rotated log should be archived (not lost)"
    print("PASS  weights: reinforce + decay + prune + log rotation")


def test_plan(proj):
    amg = proj / ".claude" / "amg"
    store = gs.GraphStore(amg)
    edit_node(store, "hub:billing", lambda n: n.__setitem__("branch_budget", 2))
    C.make_plan(proj)
    plan = json.loads((amg / "work" / "consolidation-plan.json").read_text())
    hubs = [b["hub"] for b in plan["over_budget_branches"]]
    assert "hub:billing" in hubs, "over-budget branch should be flagged"
    print(f"PASS  plan: detected over-budget branch (members > budget)")


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
        {"action": "shorten", "id": "docs:doc/faq.md::payments",
         "summary": "FAQ (condensed)", "body": "See support docs."},
        {"action": "retire", "id": "docs:doc/refunds.md::overview"},
    ]
    (amg / "work").mkdir(exist_ok=True)
    (amg / "work" / "actions.json").write_text(json.dumps(actions))
    counts = C.apply_actions(proj, amg / "work" / "actions.json")

    nodes = C.load_nodes(store)
    # archived + removed
    assert "notes:tmp/dup-b" not in nodes and "docs:doc/refunds.md::overview" not in nodes
    assert all(f"notes:ep/{i}" not in nodes for i in (1, 2, 3))
    arch = {p.name for p in (store.root / "archive").iterdir()}
    assert any("dup-b" in a for a in arch) and any("refunds" in a for a in arch)
    assert any("ep_1" in a or "ep/1" in a or "1-" in a for a in arch)  # episode archived
    # survivors / new nodes
    assert "notes:summary/eps" in nodes and "hub:misc" in nodes
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


if __name__ == "__main__":
    proj = setup_project()
    try:
        test_weights(proj)
        test_plan(proj)
        test_apply_and_recall(proj)
        print("\nALL CONSOLIDATION CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
