#!/usr/bin/env python3
"""
selftest_export.py — the read-only JSON graph export.

Proves the exporter is a faithful, read-only projection of the graph:
  1. shape       : every node is exported with its FULL frontmatter (not retrieve's
                   projection) + top-level render fields + correct bucket + degree.
  2. links       : a typed edge to an existing node becomes a link; a DANGLING edge
                   (target absent — external import / unresolved call) is dropped, as
                   retrieve does; rel / w / origin survive.
  3. part_of     : membership in a real hub node becomes a part_of link; membership in
                   a directory string does NOT (it is the node's cluster `group`).
  4. stage14     : disputed / rejected / superseded statuses and contradicts /
                   supersedes links ride along for the viewer to surface.
  5. read-only   : nodes/ is byte-for-byte unchanged after an export; the only write is
                   the output JSON (under cache/).
  6. round-trip  : the written file re-parses to the same data; the CLI writes it.
  7. html        : the viewer HTML is self-contained — library + glue + data inlined, no
                   external <script src=, no data fetch; the inlined JSON survives a
                   </script> in a node body (escaping) and re-parses to the same data.

No graph engine or model is needed. Run:  python selftest_export.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import export_graph as E


def _node(root: Path, bucket: str, name: str, fm: str, body: str = "") -> None:
    d = root / "nodes" / bucket
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")


def _build_fixture(root: Path) -> None:
    """A small graph touching every mapping the exporter must get right."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yml").write_text(
        "active: true\n"
        "viewer:\n"
        "  quality: medium\n"
        "  large_graph_mode: on\n"            # YAML coerces bare on -> True; the viewer accepts both
        "  large_graph_nodes: 50\n"
        "  options:\n"
        "    linkOpacity: 0.7\n", encoding="utf-8")

    # code: a function carrying the full trust layer + a VALID call, a DANGLING
    # call (target absent), and two part_of memberships (one hub id, one directory).
    _node(root, "code", "charge-0001",
          "id: code:src/billing.py::charge\n"
          "type: function\n"
          "qualname: charge\n"
          "source_path: src/billing.py\n"
          "lineno: 10\n"
          "line_end: 30\n"
          "source_kind: derived_from_file\n"
          "policy: mirror\n"
          "source_hash: abc123\n"
          "derived_from_hash: abc123\n"
          "confidence: 0.9\n"
          "provenance:\n  kind: code\n  commit: deadbee\n"
          "verification:\n  status: verified\n  method: ast\n"
          "lang: ru\n"
          "status: active\n"
          "summary: Charges the card.\n"
          "part_of:\n  - {topic: hub:billing, w: 0.7}\n  - {topic: src/billing, w: 0.3}\n"
          "edges:\n"
          "  - {rel: calls, to: code:src/util.py::fmt, w: 0.8, origin: structural}\n"
          "  - {rel: calls, to: code:src/ghost.py::missing, w: 0.8, origin: structural}\n")
    _node(root, "code", "fmt-0002",
          "id: code:src/util.py::fmt\ntype: function\nsource_path: src/util.py\n"
          "lineno: 3\nstatus: active\nsummary: Formats money.\n")

    # doc: documents the code function (semantic edge to an existing node).
    _node(root, "doc", "overview-0003",
          "id: doc:docs/billing.md::overview\ntype: section\nsource_path: docs/billing.md\n"
          "status: active\nsummary: Billing overview.\n"
          "edges:\n  - {rel: documents, to: code:src/billing.py::charge, w: 1.0, origin: semantic}\n")

    # _hubs: the synthesized hub that `charge` is part_of (so part_of becomes a link).
    # The body deliberately contains </script> to prove the HTML inlining escapes it.
    _node(root, "_hubs", "billing-0004",
          "id: hub:billing\ntype: hub\nsource_kind: synthesized\npolicy: authored\n"
          "status: active\nsummary: Billing subsystem hub.\n",
          body="Billing groups card charging, retries and refunds. <x></script>\n")

    # notes: an authored decision (user provenance) + arbitration verdicts.
    _node(root, "notes", "decision-0005",
          "id: note:use-stripe-ab12cd34\ntype: decision\nsource_kind: authored\n"
          "policy: authored\nstatus: captured\nconfidence: 0.85\n"
          "provenance:\n  kind: user\nverification:\n  status: verified\n  method: user\n"
          "tags: [billing, payments]\ncreated: '2026-06-01T10:00:00'\n"
          "updated: '2026-06-01T10:00:00'\nsummary: Use Stripe for card charging.\n")
    _node(root, "notes", "old-0006",
          "id: note:old-claim-1111\ntype: note\nstatus: superseded\nsummary: Old fee was 2%.\n")
    _node(root, "notes", "new-0007",
          "id: note:new-claim-2222\ntype: note\nstatus: active\nsummary: Fee is 3%.\n"
          "edges:\n  - {rel: supersedes, to: note:old-claim-1111, w: 0.3, "
          "coact: 5, origin: consolidation}\n")
    _node(root, "notes", "dispA-0008",
          "id: note:dispA-3333\ntype: note\nstatus: disputed\nsummary: Refund window is 30 days.\n"
          "edges:\n  - {rel: contradicts, to: note:dispB-4444, w: 0.3, origin: consolidation}\n")
    _node(root, "notes", "dispB-0009",
          "id: note:dispB-4444\ntype: note\nstatus: disputed\nsummary: Refund window is 60 days.\n")
    _node(root, "notes", "rej-0010",
          "id: note:rej-5555\ntype: note\nstatus: rejected\nsummary: Refunds are instant (false).\n")


def _by_id(data: Dict[str, object]) -> Dict[str, dict]:
    return {n["id"]: n for n in data["nodes"]}            # type: ignore[index,union-attr]


def _links(data: Dict[str, object]) -> set:
    return {(lk["source"], lk["target"], lk["rel"]) for lk in data["links"]}  # type: ignore[index,union-attr]


def test_shape_and_full_frontmatter(root: Path) -> None:
    data = E.build_graph_data(root)
    assert data["meta"]["node_count"] == 10, data["meta"]
    nodes = _by_id(data)
    assert set(nodes) >= {"code:src/billing.py::charge", "hub:billing",
                          "note:use-stripe-ab12cd34"}, sorted(nodes)

    charge = nodes["code:src/billing.py::charge"]
    # top-level render contract
    assert charge["type"] == "function" and charge["status"] == "active"
    assert charge["bucket"] == "code" and charge["summary"] == "Charges the card."
    # FULL frontmatter — fields retrieve's projection drops must be present here
    fm = charge["frontmatter"]
    assert fm["source_kind"] == "derived_from_file" and fm["policy"] == "mirror"
    assert fm["qualname"] == "charge" and fm["lang"] == "ru"
    assert fm["provenance"] == {"kind": "code", "commit": "deadbee"}
    assert fm["verification"] == {"status": "verified", "method": "ast"}
    assert fm["lineno"] == 10 and fm["line_end"] == 30 and fm["confidence"] == 0.9
    # authored note keeps tags / created / updated; hub keeps its body
    note = nodes["note:use-stripe-ab12cd34"]
    assert note["frontmatter"]["tags"] == ["billing", "payments"]
    assert note["frontmatter"]["created"] == "2026-06-01T10:00:00"
    assert "Billing groups" in nodes["hub:billing"]["body"]
    assert nodes["hub:billing"]["bucket"] == "_hubs"
    assert nodes["note:use-stripe-ab12cd34"]["bucket"] == "notes"
    print("PASS  shape: 10 nodes, full frontmatter + render fields + correct buckets")


def test_links_and_dangling(root: Path) -> None:
    data = E.build_graph_data(root)
    links = _links(data)
    # valid edges become links
    assert ("code:src/billing.py::charge", "code:src/util.py::fmt", "calls") in links
    assert ("doc:docs/billing.md::overview", "code:src/billing.py::charge", "documents") in links
    # the DANGLING call to a non-existent node is dropped (exactly as retrieve does)
    assert not any(t == "code:src/ghost.py::missing" for _, t, _ in links), \
        "dangling edge must be dropped"
    # rel / w / origin survive on a link
    call = next(lk for lk in data["links"]
                if (lk["source"], lk["target"]) == ("code:src/billing.py::charge",
                                                     "code:src/util.py::fmt"))
    assert call["w"] == 0.8 and call["origin"] == "structural"
    print("PASS  links: valid edges kept with rel/w/origin; dangling edge dropped")


def test_part_of_hub_vs_directory(root: Path) -> None:
    data = E.build_graph_data(root)
    links = _links(data)
    # part_of -> a real hub node IS a link
    assert ("code:src/billing.py::charge", "hub:billing", "part_of") in links
    # part_of -> a directory string is NOT a link (no node with that id)
    assert not any(t == "src/billing" for _, t, _ in links), \
        "a directory part_of topic must not become a link"
    # ...but it can win the cluster `group` (0.7 hub vs 0.3 dir -> hub wins here)
    charge = _by_id(data)["code:src/billing.py::charge"]
    assert charge["group"] == "hub:billing", charge["group"]
    # the hub gains an incident link, so its degree is non-zero (hubs render larger)
    assert _by_id(data)["hub:billing"]["degree"] >= 1
    print("PASS  part_of: hub membership is a link; a directory topic is only a group")


def test_stage14_statuses_and_conflict_edges(root: Path) -> None:
    data = E.build_graph_data(root)
    nodes = _by_id(data)
    links = _links(data)
    assert nodes["note:old-claim-1111"]["status"] == "superseded"
    assert nodes["note:dispA-3333"]["status"] == "disputed"
    assert nodes["note:rej-5555"]["status"] == "rejected"
    assert ("note:new-claim-2222", "note:old-claim-1111", "supersedes") in links
    assert ("note:dispA-3333", "note:dispB-4444", "contradicts") in links
    # the Hebbian substrate rides along on the link (coact), not just w
    sup = next(lk for lk in data["links"] if lk["rel"] == "supersedes")
    assert sup.get("coact") == 5, "coact must be carried on the link for the viewer"
    # the meta tallies expose what the filter UI needs
    assert data["meta"]["statuses"].get("disputed") == 2
    assert data["meta"]["rels"].get("contradicts") == 1
    assert set(data["meta"]["buckets"]) == {"code", "doc", "notes", "_hubs"}
    print("PASS  stage14: disputed/rejected/superseded + contradicts/supersedes exported")


def test_viewer_config_in_meta(root: Path) -> None:
    meta = E.build_graph_data(root)["meta"]
    v = meta["viewer"]
    assert v["quality"] == "medium" and v["large_graph_nodes"] == 50, v
    assert v["large_graph_mode"] is True, "YAML coerces bare `on` to True; the viewer handles it"
    assert v["options"]["linkOpacity"] == 0.7, "raw 3d-force-graph passthrough must survive verbatim"
    assert meta["project"] == "myproj", "header project name comes from the path two levels up"
    print("PASS  viewer config: meta.viewer passthrough + project name in header")


def test_read_only(root: Path) -> None:
    before = {p.relative_to(root).as_posix(): p.read_bytes()
              for p in sorted((root / "nodes").rglob("*.md"))}
    E.build_graph_data(root)
    out = root / "cache" / "graph.json"
    E._write_json(E.build_graph_data(root), out)
    after = {p.relative_to(root).as_posix(): p.read_bytes()
             for p in sorted((root / "nodes").rglob("*.md"))}
    assert before == after, "export must not touch any node file"
    assert out.exists(), "the only write is the output JSON (under cache/)"
    print("PASS  read-only: nodes/ unchanged; only cache/graph.json written")


def test_html_self_contained(root: Path) -> None:
    data = E.build_graph_data(root)
    html = E.render_html(data)
    # every injection marker is consumed
    for mark in (E._DATA_MARK, E._LIB_MARK, E._VIEWER_MARK):
        assert mark not in html, f"marker {mark!r} not replaced"
    # the vendored library and the viewer glue are inlined (no external load)
    assert "ForceGraph3D" in html and "3d-force-graph" in html, "library must be inlined"
    assert "AMG graph — viewer glue" in html, "viewer glue must be inlined"
    assert len(html) > 1_000_000, "lib (~1.3MB) should be inlined, not linked"
    # self-contained: nothing is fetched at view time
    assert "<script src=" not in html.lower(), "no external <script src="
    assert "graph.json" not in html, "data is inlined, never fetched"
    assert 'id="amg-data"' in html and "code:src/billing.py::charge" in html
    # the inlined JSON survived a </script> in a node body and re-parses to the SAME data
    m = re.search(r'<script type="application/json" id="amg-data">(.*?)</script>',
                  html, re.DOTALL)
    assert m, "the data <script> node must be present and not broken by </script>"
    restored = json.loads(m.group(1).replace("<\\/", "</"))   # reverse the </ escaping
    assert restored == data, "inlined JSON must round-trip to the exported data"
    assert any("</script>" in (n.get("body") or "") for n in restored["nodes"]), \
        "the tricky </script> body must be preserved intact after parse"
    print("PASS  html: self-contained (lib+glue+data inlined, escaped, round-trips)")


def test_html_is_default_action(root: Path) -> None:
    cache = root / "cache"
    for f in (cache / "graph.html", cache / "graph.json"):
        if f.exists():
            f.unlink()
    argv = sys.argv
    sys.argv = ["export_graph.py", "--store", str(root)]      # no flags -> HTML default
    try:
        assert E.main() == 0
    finally:
        sys.argv = argv
    assert (cache / "graph.html").exists(), "default action writes the HTML viewer"
    assert not (cache / "graph.json").exists(), "JSON is written only when --json is asked"
    print("PASS  html: the default CLI action writes the self-contained viewer")


def test_round_trip_and_cli(root: Path) -> None:
    out = root / "cache" / "graph.json"
    argv = sys.argv
    sys.argv = ["export_graph.py", "--store", str(root), "--json", str(out)]
    try:
        assert E.main() == 0
    finally:
        sys.argv = argv
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded == E.build_graph_data(root), "written JSON must equal a fresh build"
    assert reloaded["meta"]["link_count"] == len(reloaded["links"])
    print("PASS  round-trip: CLI writes JSON that re-parses to the same data")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    tmp = Path(tempfile.mkdtemp(prefix="amg-export-test-"))
    try:
        root = tmp / "myproj" / ".claude" / "amg"   # realistic layout: project name = myproj
        _build_fixture(root)
        test_shape_and_full_frontmatter(root)
        test_links_and_dangling(root)
        test_part_of_hub_vs_directory(root)
        test_stage14_statuses_and_conflict_edges(root)
        test_viewer_config_in_meta(root)
        test_read_only(root)
        test_html_self_contained(root)
        test_html_is_default_action(root)
        test_round_trip_and_cli(root)
        print("\nALL EXPORT CHECKS PASSED")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
