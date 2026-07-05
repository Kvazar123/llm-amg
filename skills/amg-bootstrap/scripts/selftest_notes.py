#!/usr/bin/env python3
"""
selftest_notes.py — proves the safe note-capture API (notes.py).

Checks:
  1. fields    : each authored type (note/decision/adr/open_question/plan) is written
                 with source_kind=authored, policy=authored, status=captured (default),
                 created+updated, lang from config, into the nodes/notes/ bucket.
  2. identity  : id is `note:<slug>-<hash8>`, content-addressed — an identical re-capture
                 maps to the SAME node (created preserved, updated bumped), distinct
                 content to a distinct node.
  3. explicit  : --id keeps a stable node; re-add updates in place, merging part_of/edges
                 and preserving created. Authored edges are stamped origin=authored.
  4. crash     : a note write interrupted mid-commit is healed by recover() — the node
                 lands and the journal is left clean (capture survives a crash).
  5. survives  : an authored note is NOT purged by a reconcile bootstrap and is not
                 re-queued (the deletion/move passes only touch derived_from_file+mirror).
  6. retrieval : a captured note is reachable by its summary AND by a distinctive tag
                 (tags are folded into the BM25 bag).
  7. plan      : note/open_question/plan are episodic candidates in the consolidation
                 plan; decision/adr are NOT (they are protected commitments).

Run:  python selftest_notes.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import graph_store as gs
import reconcile as rc
import notes as NT

CONFIG = ("active: true\nworking_language: ru\nmirror_path: src\n"
          "retrieval:\n  embeddings:\n    enabled: off\n")
PY_SRC = "def charge(card):\n    return card\n"


def setup_project() -> Path:
    proj = Path(tempfile.mkdtemp(prefix="amg-notes-"))
    amg = proj / ".claude" / "amg"
    amg.mkdir(parents=True)
    (amg / "config.yml").write_text(CONFIG, encoding="utf-8")
    src = proj / "src"
    src.mkdir()
    (src / "app.py").write_text(PY_SRC, encoding="utf-8")
    return proj


def amg_root(proj: Path) -> Path:
    return proj / ".claude" / "amg"


def nodes_of(proj: Path) -> Dict[str, Any]:
    return rc.load_nodes(gs.GraphStore(amg_root(proj)))


def case_fields(proj: Path) -> Dict[str, Any]:
    ids = {}
    for t in NT.NOTE_TYPES:
        res = NT.add_note(proj, t, f"{t} about the billing subsystem",
                          amg_root=amg_root(proj))
        ids[t] = res["id"]
        assert res["created"] is True and res["status"] == "captured", res

    nodes = nodes_of(proj)
    n = nodes[ids["decision"]]
    assert n["source_kind"] == "authored" and n["policy"] == "authored", n
    assert n["status"] == "captured", n
    assert n["lang"] == "ru", "lang must come from working_language"
    assert n["created"] == n["updated"], "created==updated on a fresh capture"
    assert n["_path"].startswith("nodes/notes/"), n["_path"]
    assert n["id"].startswith("note:"), "namespace is note: for every type"
    print("PASS  fields: authored/captured, lang from config, notes/ bucket, note: id")
    return ids


def case_identity(proj: Path) -> None:
    r1 = NT.add_note(proj, "note", "idempotent capture probe", body="same",
                     tags=["probe"], amg_root=amg_root(proj))
    # backdate created to prove the update path preserves it
    nodes = nodes_of(proj)
    meta = {k: v for k, v in nodes[r1["id"]].items() if not k.startswith("_")}
    meta["created"] = "2020-01-01T00:00:00"
    gs.atomic_write_text(amg_root(proj) / nodes[r1["id"]]["_path"],
                         rc.serialize_node(meta, "same"))

    r2 = NT.add_note(proj, "note", "idempotent capture probe", body="same",
                     tags=["probe"], amg_root=amg_root(proj))
    assert r2["id"] == r1["id"] and r2["created"] is False, r2
    n = nodes_of(proj)[r1["id"]]
    assert n["created"] == "2020-01-01T00:00:00", "re-capture must preserve created"
    assert n["updated"] != "2020-01-01T00:00:00", "re-capture must bump updated"

    r3 = NT.add_note(proj, "note", "a different conclusion entirely",
                     amg_root=amg_root(proj))
    assert r3["id"] != r1["id"], "distinct content -> distinct node"
    print("PASS  identity: content-addressed id; re-capture updates one node (created kept)")


def case_explicit_id(proj: Path) -> None:
    NT.add_note(proj, "plan", "ship v1", node_id="note:living-plan",
                part_of=[{"topic": "src", "w": 1.0}], amg_root=amg_root(proj))
    nodes = nodes_of(proj)
    created0 = nodes["note:living-plan"]["created"]

    NT.add_note(proj, "plan", "ship v2", node_id="note:living-plan",
                edges=[{"rel": "relates_to", "to": "code:src/app.py", "w": 0.4}],
                amg_root=amg_root(proj))
    n = nodes_of(proj)["note:living-plan"]
    assert n["summary"] == "ship v2", n
    assert n["created"] == created0, "explicit-id update preserves created"
    assert any(p.get("topic") == "src" for p in n["part_of"]), "part_of kept"
    edge = next(e for e in n["edges"] if e["rel"] == "relates_to")
    assert edge["to"] == "code:src/app.py" and edge["origin"] == "authored", edge
    print("PASS  explicit id: stable node updates in place, part_of/edges merge, origin authored")


def case_crash_recovery(proj: Path) -> None:
    store = gs.GraphStore(amg_root(proj))
    nid = "note:crash-probe"
    meta = {"id": nid, "type": "note", "source_kind": "authored", "policy": "authored",
            "status": "captured", "tags": [], "part_of": [], "edges": [], "lang": "ru",
            "created": rc._now(), "updated": rc._now(), "summary": "survive a crash"}
    relpath = rc.node_relpath(nid, "notes")
    with store.lock():
        store.recover()
        tx = store.transaction()
        tx.write(relpath, rc.serialize_node(meta, ""))
        try:                              # crash mid-commit: intent durable, not committed
            tx.commit(_fault_after_apply_ops=0)
            assert False, "fault hook must raise"
        except gs._SimulatedCrash:
            pass
    assert list(store.journal_dir.iterdir()), "an uncommitted transaction must remain"

    with store.lock():
        store.recover()
    assert nid in nodes_of(proj), "recover must land the interrupted note"
    assert not list(store.journal_dir.iterdir()), "recover must leave the journal clean"
    print("PASS  crash: a note write interrupted mid-commit is healed by recover()")


def case_survives_bootstrap(proj: Path) -> Dict[str, Any]:
    res = NT.add_note(proj, "decision", "this decision must outlive a bootstrap",
                      amg_root=amg_root(proj))
    rc.plan(proj, amg_root(proj))         # reconcile against src
    nodes = nodes_of(proj)
    assert res["id"] in nodes, "authored note must NOT be purged by a source diff"
    assert nodes[res["id"]]["status"] == "captured", "bootstrap must not touch its status"
    q = json.loads((amg_root(proj) / "work" / "queue.json").read_text(encoding="utf-8"))
    assert res["id"] not in {u["id"] for u in q["units"]}, "a note must not be queued"
    print("PASS  survives: authored note coexists with bootstrap, never purged or queued")
    return res


def case_retrieval(proj: Path) -> None:
    sys.path.insert(0, str(HERE.parents[1] / "amg-retrieve" / "scripts"))
    import retrieve as RT
    res = NT.add_note(proj, "note",
                      "central dispatch table maps requests to handlers",
                      tags=["zzdistinctivetag"], amg_root=amg_root(proj))
    store = amg_root(proj)
    by_summary = RT.retrieve(store, "central dispatch table requests",
                             write_pack=False, log_coactivation=False)
    assert res["id"] in [nid for nid, _ in by_summary["ranked"][:5]], "found by summary"
    by_tag = RT.retrieve(store, "zzdistinctivetag",
                         write_pack=False, log_coactivation=False)
    assert res["id"] in by_tag["seeds"], "tag must seed the note (tags are indexed)"
    print("PASS  retrieval: a captured note is found by summary and by its tag")


def case_provenance(proj: Path) -> None:
    """An authored decision/adr is the human's word (provenance.kind=user,
    verification verified/user); a note/open_question/plan is the model's inference
    (model_inference, unverified). confidence defaults per type; --kind/--confidence
    override."""
    dec = NT.add_note(proj, "decision", "use postgres for the ledger",
                      node_id="note:prov-dec", amg_root=amg_root(proj))
    note = NT.add_note(proj, "note", "the dispatcher caches handlers",
                       node_id="note:prov-note", amg_root=amg_root(proj))
    nodes = nodes_of(proj)
    d, n = nodes[dec["id"]], nodes[note["id"]]
    assert d["provenance"]["kind"] == "user", d["provenance"]
    assert d["verification"] == {"status": "verified", "method": "user"}, d["verification"]
    assert d["confidence"] == 0.85, d
    assert n["provenance"]["kind"] == "model_inference", n["provenance"]
    assert n["verification"]["status"] == "unverified", n["verification"]
    assert n["confidence"] == 0.6, n

    # overrides: a note the user explicitly states is user-kind; confidence is settable
    ov = NT.add_note(proj, "note", "remember the deploy window is friday",
                     node_id="note:prov-ov", kind="user", confidence=0.95,
                     amg_root=amg_root(proj))
    o = nodes_of(proj)[ov["id"]]
    assert o["provenance"]["kind"] == "user" and o["confidence"] == 0.95, o
    assert o["verification"]["method"] == "user", o
    print("PASS  provenance: decision=user/verified, note=model_inference/unverified, overrides honored")


def case_consolidation_plan(proj: Path, ids: Dict[str, Any]) -> None:
    sys.path.insert(0, str(HERE.parents[1] / "amg-consolidate" / "scripts"))
    import consolidate as CO
    CO.make_plan(proj, amg_root(proj))
    plan = json.loads((amg_root(proj) / "work" / "consolidation-plan.json")
                      .read_text(encoding="utf-8"))
    episodic = {e["id"] for e in plan["episodic_candidates"]}
    for t in ("note", "open_question", "plan"):
        assert ids[t] in episodic, f"{t} must be an episodic candidate"
    for t in ("decision", "adr"):
        assert ids[t] not in episodic, f"{t} is protected, not episodic"
    print("PASS  plan: note/open_question/plan are episodic; decision/adr are not")


if __name__ == "__main__":
    proj = setup_project()
    try:
        ids = case_fields(proj)
        case_identity(proj)
        case_explicit_id(proj)
        case_crash_recovery(proj)
        case_survives_bootstrap(proj)
        case_retrieval(proj)
        case_provenance(proj)
        case_consolidation_plan(proj, ids)
        print("\nALL NOTES CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
