#!/usr/bin/env python3
"""Selftest for migrate_schema.py: a pre-canon graph passes migration.

Covers: source_kind derived -> synthesized; type derived -> hub/overview;
tree-sitter grammar kinds -> canonical; edge-origin backfill per owner class;
idempotency (second run writes nothing); lineno restoration is reconcile's job —
the bootstrap right after migration refreshes it via the pointer-drift branch.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_store as gs                                    # noqa: E402
import migrate_schema                                       # noqa: E402
import reconcile as rc                                      # noqa: E402


def _write_node(store: gs.GraphStore, meta: Dict[str, Any], bucket: str) -> None:
    rel = rc.node_relpath(meta["id"], bucket)
    gs.atomic_write_text(store.root / rel, rc.serialize_node(meta, ""))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        (proj / "src").mkdir()
        (proj / "src" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        amg = proj / ".claude" / "amg"
        store = gs.GraphStore(amg)
        store.init()
        (amg / "config.yml").write_text(
            "working_language: en\nmirror_path: src\n", encoding="utf-8")

        # legacy strategic nodes (pre-canon: type/source_kind 'derived')
        _write_node(store, {
            "id": "hub:billing", "type": "derived", "source_kind": "derived",
            "policy": "authored", "source_hash": None, "derived_from_hash": None,
            "part_of": [], "edges": [{"rel": "relates_to", "to": "code:src/m.py",
                                      "w": 0.4, "coact": 0}],
            "lang": "en", "status": "active", "summary": "Billing hub.",
            "updated": "2025-01-01T00:00:00"}, "_hubs")
        _write_node(store, {
            "id": "hub:overview", "type": "derived", "source_kind": "derived",
            "policy": "authored", "source_hash": None, "derived_from_hash": None,
            "part_of": [], "edges": [], "lang": "en", "status": "active",
            "summary": "Architecture overview.",
            "updated": "2025-01-01T00:00:00"}, "_hubs")
        # legacy tree-sitter node (grammar kind, absorb orphan: survives the diff)
        _write_node(store, {
            "id": "code:src/app.js::foo", "type": "function_definition",
            "source_path": "src/app.js", "qualname": "foo", "lineno": 3,
            "source_kind": "derived_from_file", "policy": "absorb",
            "source_hash": "deadbeef", "derived_from_hash": "deadbeef",
            "part_of": [{"topic": "src", "w": 1.0}],
            "edges": [{"rel": "calls", "to": "code:src/app.js::bar", "w": 0.7,
                       "coact": 0}],
            "lang": "en", "status": "active", "summary": "foo.",
            "updated": "2025-01-01T00:00:00"}, "code")

        rc.plan(proj, amg)                       # creates the python mirror nodes
        nodes = rc.load_nodes(store)
        fn = nodes["code:src/m.py::f"]
        # simulate a pre-lineno node with an unmarked semantic edge
        meta = {k: v for k, v in fn.items() if not k.startswith("_")}
        meta.pop("lineno", None)
        meta["edges"] = list(meta.get("edges") or []) + [
            {"rel": "documents", "to": "hub:billing", "w": 0.5, "coact": 0}]
        gs.atomic_write_text(store.root / fn["_path"], rc.serialize_node(meta, ""))

        res = migrate_schema.migrate(proj, amg)
        assert res["source_kind_normalized"] == 2, res
        assert res["hub_types_fixed"] == 2 and res["overview_ids"] == ["hub:overview"], res
        assert res["kinds_canonicalized"] == 1, res
        assert res["edges_origin_backfilled"] == 3, res    # hub edge + calls + documents
        # the 3 hand-written legacy nodes lack provenance/verification (the python mirror
        # nodes got them at plan time); migrate backfills exactly those
        assert res["provenance_backfilled"] == 3 and res["verification_backfilled"] == 3, res
        nodes = rc.load_nodes(store)
        assert nodes["hub:billing"]["type"] == "hub"
        assert nodes["hub:billing"]["source_kind"] == "synthesized"
        assert nodes["hub:billing"]["edges"][0]["origin"] == "synthesized"
        assert nodes["hub:overview"]["type"] == "overview"
        assert nodes["code:src/app.js::foo"]["type"] == "function"
        assert nodes["code:src/app.js::foo"]["edges"][0]["origin"] == "structural"
        docs_edge = [e for e in nodes["code:src/m.py::f"]["edges"]
                     if e["rel"] == "documents"][0]
        assert docs_edge["origin"] == "semantic"
        # provenance.kind inferred per class: synthesized hub -> model_inference, a
        # file-projected node -> its id-prefix domain; verification starts unverified
        assert nodes["hub:billing"]["provenance"]["kind"] == "model_inference", nodes["hub:billing"]
        assert nodes["code:src/app.js::foo"]["provenance"]["kind"] == "code", nodes["code:src/app.js::foo"]
        assert nodes["hub:billing"]["verification"]["status"] == "unverified"
        print("PASS  migrate: derived -> hub/overview + synthesized; grammar kind "
              "-> function; origin + provenance/verification backfilled per owner class")

        res2 = migrate_schema.migrate(proj, amg)
        assert res2["nodes_updated"] == 0, res2
        print("PASS  migrate: second run is a no-op (idempotent)")

        summary = rc.plan(proj, amg)             # bootstrap right after migration
        nodes = rc.load_nodes(store)
        assert nodes["code:src/m.py::f"]["lineno"] == 1, nodes["code:src/m.py::f"]
        assert summary["pointer_refreshed"] >= 1, summary
        print("PASS  migrate + bootstrap: pointer drift restores lineno for free")

    print("\nALL MIGRATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
