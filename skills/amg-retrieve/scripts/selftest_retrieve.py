#!/usr/bin/env python3
"""
selftest_retrieve.py - regression for Stage 2 retrieval stabilization.

Headless and deterministic: embeddings are forced off (stubbed to None), so every
check is pure BM25 + PPR with no model download. Covers:

  1. status prior   : superseded is pushed down (x0.2) so it never outranks an
                      otherwise-equal active node; stale is NOT penalized.
  2. stale mark     : a stale node is flagged in the pack (open the source).
  3. tier + body    : decision/adr land in the strategic tier and render their
                      body inline regardless of tier (authored rulings).
  4. config merge   : config.yml overlays defaults KEY-BY-KEY (an incomplete
                      relation_priors / status_prior / token_budget keeps the rest).
  5. inspect bucket : --bucket filters by the real on-disk directory (notes/_hubs
                      too), not a guessed id prefix (roadmap 1.26).
  6. explain        : --explain attributes a multi-hop node's activation to the
                      incoming edge that carried the mass (grounds explainability).

Run:  python selftest_retrieve.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import embed
import inspect_graph as IG
import retrieve as R

try:
    import yaml
except ImportError:                                   # pragma: no cover
    sys.stderr.write("needs PyYAML\n"); raise


def _write(store: Path, bucket: str, fname: str, meta: Dict[str, Any], body: str = "") -> None:
    d = store / "nodes" / bucket
    d.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    (d / fname).write_text(f"---\n{fm}\n---\n{body}".rstrip() + "\n", encoding="utf-8")


def _node(nid: str, typ: str, summary: str, status: str = "active",
          **extra: Any) -> Dict[str, Any]:
    return {"id": nid, "type": typ, "summary": summary, "status": status,
            "edges": extra.get("edges", []), "part_of": extra.get("part_of", []),
            **{k: v for k, v in extra.items() if k not in ("edges", "part_of")}}


def test_status_prior_unit() -> None:
    nodes: Dict[str, Dict[str, Any]] = {"a": {"status": "active"}, "s": {"status": "superseded"},
                                        "t": {"status": "stale"}, "n": {"status": None}}
    act = {"a": 1.0, "s": 1.0, "t": 1.0, "n": 1.0}
    cfg = {"status_prior": {"active": 1.0, "stale": 1.0, "superseded": 0.2}}
    out = R._apply_status_prior(act, nodes, cfg)
    assert out["a"] == 1.0 and out["t"] == 1.0, "active/stale must not be penalized"
    assert abs(out["s"] - 0.2) < 1e-9, f"superseded must scale by 0.2, got {out['s']}"
    assert out["n"] == 1.0, "unknown/None status defaults to 1.0"
    print("PASS  status prior (unit): superseded x0.2, active/stale/None x1.0")


def test_superseded_ranks_below_active(tmp: Path) -> None:
    store = tmp / "supersede"
    # Two isolated nodes, identical summary -> identical seed/PPR; only status differs.
    summ = "retry policy on payment gateway failure"
    _write(store, "notes", "active.md", _node("notes:retry-active", "note", summ))
    _write(store, "notes", "old.md", _node("notes:retry-old", "note", summ,
                                           status="superseded"))
    res = R.retrieve(store, "retry policy gateway failure",
                     write_pack=False, log_coactivation=False)
    rank = {nid: a for nid, a in res["ranked"]}
    a, s = rank["notes:retry-active"], rank["notes:retry-old"]
    assert a > s, f"active ({a}) must outrank superseded ({s})"
    assert s / a < 0.5, f"superseded should be strongly downweighted, ratio {s / a:.3f}"
    assert res["ranked"][0][0] == "notes:retry-active", "active must be top"
    print(f"PASS  superseded ranks below active (ratio {s / a:.2f} ~ 0.2)")


def test_stale_is_flagged(tmp: Path) -> None:
    store = tmp / "stale"
    _write(store, "doc", "s.md", _node("doc:guide.md::sync", "section",
                                       "How background sync reconciles state",
                                       status="stale"))
    res = R.retrieve(store, "background sync reconcile state",
                     write_pack=False, log_coactivation=False)
    assert R._STALE_TEXT in res["pack"], "stale node must be flagged in the pack"
    print("PASS  stale node flagged in pack")


def test_trust_marks(tmp: Path) -> None:
    """Stage 13 pack marking: an unverified code node, a contradicted one, and a
    low-confidence one are each flagged (so the model confirms before relying), and a
    code pointer renders the line RANGE when line_end is known. Marks never downrank —
    they are annotations."""
    store = tmp / "marks"
    _write(store, "code", "u.md", _node(           # no verification field -> unverified
        "code:src/u.py::f", "function", "validate the incoming request payload",
        source_path="src/u.py", lineno=5, line_end=12))
    _write(store, "code", "c.md", _node(           # explicitly contradicted
        "code:src/c.py::g", "function", "validate the request schema thoroughly",
        source_path="src/c.py", lineno=3,
        verification={"status": "contradicted", "method": "grep"}))
    _write(store, "code", "l.md", _node(           # verified but low confidence
        "code:src/l.py::h", "function", "validate request fields and types",
        source_path="src/l.py", lineno=8,
        verification={"status": "verified", "method": "ast"}, confidence=0.2))
    res = R.retrieve(store, "validate request payload schema fields",
                     write_pack=False, log_coactivation=False)
    pack = res["pack"]
    assert "unverified: confirm this code claim" in pack, pack
    assert "contradicted: source check failed" in pack, pack
    assert "low confidence 0.20" in pack, pack
    assert "src/u.py:5-12" in pack, "code pointer renders the line range when line_end is set"
    print("PASS  trust marks: unverified/contradicted/low-confidence flagged; line range rendered")


def test_decision_strategic_with_body(tmp: Path) -> None:
    store = tmp / "decision"
    body = "We adopt query-biased Personalized PageRank over flat top-k for retrieval."
    _write(store, "notes", "d.md",
           _node("notes:decisions/retrieval-method", "decision",
                 "Retrieval uses PPR, not flat top-k"), body=body)
    _write(store, "code", "f.md",
           _node("code:src/r.py::run", "function", "Run retrieval over the graph",
                 source_path="src/r.py", lineno=3))
    res = R.retrieve(store, "retrieval method PPR top-k",
                     write_pack=False, log_coactivation=False)
    assert "notes:decisions/retrieval-method" in res["tiers"]["strategic"], \
        "decision must land in the strategic tier"
    assert body in res["pack"], "decision body must render inline (authored ruling)"
    print("PASS  decision -> strategic tier, body rendered inline")


def test_config_deep_merge(tmp: Path) -> None:
    store = tmp / "cfg"
    (store / "nodes").mkdir(parents=True, exist_ok=True)
    (store / "config.yml").write_text(
        "active: true\n"
        "retrieval:\n"
        "  relation_priors:\n"
        "    calls: 0.95\n"
        "  status_prior:\n"
        "    superseded: 0.1\n"
        "  token_budget:\n"
        "    strategic: 9999\n", encoding="utf-8")
    cfg = R.load_config(store)
    assert cfg["relation_priors"]["calls"] == 0.95, "override applied"
    assert cfg["relation_priors"]["documents"] == 0.9, "unlisted prior kept from defaults"
    assert cfg["relation_priors"]["refines"] == 0.6, "forward prior kept from defaults"
    assert cfg["status_prior"]["superseded"] == 0.1, "status override applied"
    assert cfg["status_prior"]["active"] == 1.0, "unlisted status kept from defaults"
    assert cfg["token_budget"]["strategic"] == 9999, "budget override applied"
    assert cfg["token_budget"]["tactical"] == 2500, "unlisted budget kept from defaults"
    # the top-level verification block is surfaced into the retrieval cfg with defaults
    assert cfg["verification"]["enabled"] is True, "verification defaults surfaced"
    assert cfg["verification"]["min_confidence_warn"] == 0.5, "verification default kept"
    print("PASS  config merges key-by-key (no silent loss of defaults; verification surfaced)")


def test_inspect_bucket(tmp: Path) -> None:
    store = tmp / "bucket"
    _write(store, "code", "c.md", _node("code:src/a.py::f", "function", "a"))
    _write(store, "notes", "n.md", _node("notes:note-1", "note", "b"))
    _write(store, "_hubs", "h.md", _node("hub:topic", "hub", "c"))
    nodes = R.load_nodes(store)
    by = {nid: n for nid, n in nodes.items()}
    assert IG._in_bucket(by["notes:note-1"], "notes")
    assert not IG._in_bucket(by["notes:note-1"], "code")
    assert IG._in_bucket(by["hub:topic"], "_hubs")
    assert IG._in_bucket(by["code:src/a.py::f"], "code")
    assert not IG._in_bucket(by["code:src/a.py::f"], "notes")
    print("PASS  inspect --bucket filters by real directory (notes/_hubs/code)")


def test_explain(tmp: Path) -> None:
    store = tmp / "explain"
    # A seeds (matches query); B shares no query words, reached only via A--calls-->B.
    _write(store, "code", "a.md",
           _node("code:src/m.py::a", "function", "charge the customer card payment",
                 source_path="src/m.py", lineno=10,
                 edges=[{"rel": "calls", "to": "code:src/m.py::b", "w": 0.9}]))
    _write(store, "code", "b.md",
           _node("code:src/m.py::b", "function", "aggregate line items into a total",
                 source_path="src/m.py", lineno=30))
    res = R.retrieve(store, "charge customer card payment",
                     write_pack=False, log_coactivation=False, explain=5)
    contribs = res.get("explain", {}).get("code:src/m.py::b", [])
    assert contribs, "explain must report inflow edges for the multi-hop node"
    top = contribs[0]
    assert top["from"] == "code:src/m.py::a", f"top contributor should be the seed: {top}"
    assert "calls" in top["rel"], f"edge label should name the relation: {top['rel']}"
    assert top["share"] > 0.0, "share must be positive"
    print(f"PASS  --explain: b's activation attributed to a via '{top['rel']}' "
          f"({top['share'] * 100:.0f}%)")


def test_intent_and_conflict(tmp: Path) -> None:
    """Stage 14 surfacing, driven by a caller-supplied intent flag (the model recognizes
    intent in any language; the code only applies it):
      (a) status prior: disputed/rejected get their multipliers, and `lift` neutralizes
          ALL retired downranks;
      (b) a superseded node, normally downranked, is lifted by intent=['history'];
      (c) a conflict intent seeds the conflict subgraph so a disputed node (lexically
          off-topic) surfaces and is flagged."""
    # (a) unit
    nodes: Dict[str, Dict[str, Any]] = {"a": {"status": "active"}, "s": {"status": "superseded"},
                                        "d": {"status": "disputed"}, "r": {"status": "rejected"}}
    act = {"a": 1.0, "s": 1.0, "d": 1.0, "r": 1.0}
    cfg = {"status_prior": {"active": 1.0, "superseded": 0.2, "disputed": 0.5, "rejected": 0.1}}
    out = R._apply_status_prior(act, nodes, cfg)
    assert abs(out["d"] - 0.5) < 1e-9 and abs(out["r"] - 0.1) < 1e-9, out
    lifted = R._apply_status_prior(act, nodes, cfg, lift=True)
    assert all(abs(lifted[k] - 1.0) < 1e-9 for k in nodes), "lift neutralizes every downrank"

    # (b) history intent lifts the superseded downrank end-to-end
    store = tmp / "intent"
    summ = "the billing retry schedule"
    _write(store, "notes", "act.md", _node("notes:cur", "note", summ))
    _write(store, "notes", "sup.md", _node("notes:old", "note", summ, status="superseded"))
    base = R.retrieve(store, "billing retry schedule", write_pack=False, log_coactivation=False)
    brank = {nid: a for nid, a in base["ranked"]}
    assert brank["notes:cur"] > brank["notes:old"], "without intent, superseded is downranked"
    hist = R.retrieve(store, "billing retry schedule", write_pack=False,
                      log_coactivation=False, intent=["history"])
    hrank = {nid: a for nid, a in hist["ranked"]}
    assert abs(hrank["notes:cur"] - hrank["notes:old"]) < 1e-9, "history intent lifts the downrank"
    assert hist["intent"] == ["history"]

    # (c) conflict intent seeds the conflict subgraph: a disputed node off-topic to the
    # query surfaces and is flagged
    store2 = tmp / "conflict"
    _write(store2, "notes", "q.md", _node("notes:topic", "note", "alpha beta gamma"))
    _write(store2, "notes", "disp.md", _node("notes:contested", "note",
           "completely unrelated zeta payload", status="disputed",
           edges=[{"rel": "contradicts", "to": "notes:other"}]))
    _write(store2, "notes", "oth.md", _node("notes:other", "note", "another zeta payload"))
    plain = R.retrieve(store2, "alpha beta gamma", write_pack=False, log_coactivation=False)
    conf = R.retrieve(store2, "alpha beta gamma", write_pack=False,
                      log_coactivation=False, intent=["conflict"])
    pa = {nid: a for nid, a in plain["ranked"]}
    ca = {nid: a for nid, a in conf["ranked"]}
    assert ca["notes:contested"] > pa["notes:contested"], "conflict intent seeds the disputed node"
    assert "disputed: an unresolved contradiction" in conf["pack"], conf["pack"]
    print("PASS  intent: history lifts retired downrank; conflict seeds the conflict subgraph + flags disputed")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    orig = embed.get_embedder
    embed.get_embedder = lambda cfg: None              # force pure BM25, deterministic
    tmp = Path(tempfile.mkdtemp(prefix="amg-retr-"))
    try:
        test_status_prior_unit()
        test_superseded_ranks_below_active(tmp)
        test_stale_is_flagged(tmp)
        test_trust_marks(tmp)
        test_decision_strategic_with_body(tmp)
        test_intent_and_conflict(tmp)
        test_config_deep_merge(tmp)
        test_inspect_bucket(tmp)
        test_explain(tmp)
        print("\nALL RETRIEVAL CHECKS PASSED")
    finally:
        embed.get_embedder = orig
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
