#!/usr/bin/env python3
"""
selftest_chunkers.py — Stage 11 structural chunkers, all stdlib-only (no optional libs):
ndjson, csv/tsv, log, rst. PPTX (optional python-pptx) lives in selftest_stage2.py with
the other binary-document extractors.

Checks:
  1. classify : the new extensions route to their own chunkers (not the old fallbacks —
                .rst left `headings`, .ndjson left `json`; .pptx left BINARY_EXT).
  2. ndjson   : one record per JSON line, an id-ish field gives a stable qual, real
                lineno, garbage lines skipped, all-garbage -> one file unit.
  3. csv/tsv  : one STRUCTURAL sheet unit (headers + sample rows), delimiter per suffix.
  4. log      : timestamped lines -> bounded episode blocks; a non-log file -> paragraphs.
  5. rst      : underline/overline headings -> sections (+ preamble); none -> file unit.
  6. reconcile: units from the new chunkers build nodes in the right buckets (data/doc)
                with canonical types — proving reconcile consumes them unchanged.

Run:  python selftest_chunkers.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import extract_structure as ES
import graph_store as gs
import reconcile as rc


def test_classify():
    assert ES.classify(Path("a.ndjson"))[:2] == ("data", "ndjson")
    assert ES.classify(Path("a.csv"))[:2] == ("data", "csv")
    assert ES.classify(Path("a.tsv"))[:2] == ("data", "csv")
    assert ES.classify(Path("a.log"))[:2] == ("doc", "log")
    assert ES.classify(Path("a.rst"))[:2] == ("doc", "rst")
    assert ES.classify(Path("a.pptx"))[:2] == ("doc", "pptx")     # extracted, not skipped
    assert ES.classify(Path("a.json"))[:2] == ("data", "json")    # unchanged
    print("PASS  classify: ndjson/csv/tsv/log/rst/pptx route to their own chunkers")


def test_ndjson(tmp):
    p = tmp / "events.ndjson"
    p.write_text('{"id": "evt-1", "msg": "a"}\nnot json\n{"msg": "b"}\n\n', encoding="utf-8")
    units = ES._ndjson_units(p, "events.ndjson", "absorb")
    assert len(units) == 2, units                              # the garbage line is skipped
    assert units[0]["id"] == "data:events.ndjson::evt-1", units[0]
    assert units[0]["kind"] == "record" and units[0]["category"] == "data", units[0]
    assert units[1]["qualname"] == "L2" and units[1]["lineno"] == 3, units[1]  # real source line
    p2 = tmp / "bad.ndjson"
    p2.write_text("nope\n---\n", encoding="utf-8")
    fb = ES._ndjson_units(p2, "bad.ndjson", "absorb")
    assert len(fb) == 1 and fb[0]["kind"] == "file", fb         # nothing parsed -> file unit
    print("PASS  ndjson: record per JSON line; id field -> stable qual; garbage -> skip/fallback")


def test_csv(tmp):
    p = tmp / "routes.csv"
    p.write_text("path,handler,method\n/users,list,GET\n/users,create,POST\n", encoding="utf-8")
    units = ES._csv_units(p, "routes.csv", "mirror")
    assert len(units) == 1 and units[0]["kind"] == "sheet", units
    assert units[0]["category"] == "data" and units[0]["id"] == "data:routes.csv", units[0]
    assert "Columns: path, handler, method" in units[0]["text"], units[0]["text"]
    assert "2 rows x 3 columns" in units[0]["text"], units[0]["text"]
    pt = tmp / "x.tsv"
    pt.write_text("a\tb\n1\t2\n", encoding="utf-8")
    ut = ES._csv_units(pt, "x.tsv", "mirror")
    assert "Columns: a, b" in ut[0]["text"], ut[0]["text"]      # tab delimiter honored
    print("PASS  csv/tsv: one structural sheet unit (headers + sample rows + counts)")


def test_log(tmp):
    p = tmp / "app.log"
    p.write_text("\n".join(f"2026-06-18 10:00:0{i} INFO line {i}" for i in range(6)) + "\n",
                 encoding="utf-8")
    units = ES._log_units(p, "app.log", "absorb", group_lines=2)
    assert len(units) == 3 and all(u["kind"] == "block" for u in units), units   # 6 lines / 2
    assert units[0]["id"] == "doc:app.log::e1" and units[0]["lineno"] == 1, units[0]
    assert units[1]["lineno"] == 3, units[1]
    assert all(u["category"] == "doc" and u["text"] for u in units), units
    pn = tmp / "notes.log"
    pn.write_text("just a note\n\nanother paragraph\n", encoding="utf-8")
    nb = ES._log_units(pn, "notes.log", "absorb")
    assert nb and all(u["lang"] == "text" for u in nb), nb       # not a log -> paragraphs
    print("PASS  log: timestamped lines -> bounded episode blocks; non-log -> paragraphs")


def test_rst(tmp):
    p = tmp / "guide.rst"
    p.write_text("intro text\n\nTitle One\n=========\nbody one\n\nSub\n---\nbody sub\n",
                 encoding="utf-8")
    units = ES._rst_units(p, "guide.rst", "mirror")
    quals = [u["qualname"] for u in units]
    assert "_preamble" in quals, quals
    assert "title-one" in quals and "sub" in quals, quals
    assert all(u["kind"] == "section" and u["lang"] == "rst" for u in units), units
    pf = tmp / "flat.rst"
    pf.write_text("no headings here\njust lines\n", encoding="utf-8")
    fb = ES._rst_units(pf, "flat.rst", "mirror")
    assert len(fb) == 1 and fb[0]["kind"] == "file", fb          # no adornments -> file unit
    print("PASS  rst: underline headings -> sections (+ preamble); none -> file unit")


def test_recursive_json(tmp):
    import json as _json
    flat = tmp / "flat.json"
    flat.write_text(_json.dumps({"name": "svc", "port": 8080}), encoding="utf-8")
    fu = ES._data_units(flat, "flat.json", "mirror")
    assert {u["qualname"] for u in fu} == {"name", "port"}, fu       # classic one-record-per-entry
    assert all(u["kind"] == "record" for u in fu), fu
    nested = {"service": "billing",
              "config": {"endpoints": {f"ep{i}": {"path": f"/p{i}", "method": "GET"}
                                       for i in range(8)}}}
    nf = tmp / "nested.json"
    nf.write_text(_json.dumps(nested), encoding="utf-8")
    nu = ES._data_units(nf, "nested.json", "mirror", recurse_min=50, max_depth=5)
    quals = {u["qualname"] for u in nu}
    assert "service" in quals, quals                                 # small scalar stays a record
    assert any(q.startswith("config.endpoints.ep") for q in quals), quals   # descended by path
    assert "config" not in quals, quals                              # big container not emitted whole
    fl = tmp / "ids.json"
    fl.write_text(_json.dumps({"ids": list(range(1000))}), encoding="utf-8")
    flu = ES._data_units(fl, "ids.json", "mirror", recurse_min=50)
    assert len(flu) == 1 and flu[0]["qualname"] == "ids", flu        # flat scalar list NOT exploded
    capped = ES._data_units(nf, "nested.json", "mirror", recurse_min=50, max_depth=5, cap=3)
    assert len(capped) <= 3, capped                                  # node cap honored
    print("PASS  recursive-json: large nests split by key path; flat/small kept; cap honored")


def test_chat(tmp):
    import json as _json
    convo = [
        {"role": "user", "content": "How does the lock work?", "id": "u1", "conversation_id": "c1"},
        {"role": "assistant", "id": "a1", "conversation_id": "c1",
         "content": [{"type": "text", "text": "A single-writer lock."},
                     {"type": "tool_use", "name": "Bash"}]},
    ]
    p = tmp / "chat.json"
    p.write_text(_json.dumps(convo), encoding="utf-8")
    units = ES._chat_units(p, "chat.json", "absorb")
    assert units and len(units) == 2, units
    assert units[0]["id"] == "doc:chat.json::u1" and units[0]["kind"] == "section", units[0]
    assert units[0]["lang"] == "chat" and units[0]["category"] == "doc", units[0]
    assert "[user]" in units[0]["text"] and "lock" in units[0]["text"], units[0]["text"]
    assert "follows" not in units[0], units[0]                       # first turn: no predecessor
    assert units[1].get("follows") == "doc:chat.json::u1", units[1]  # adjacency chain
    assert "== Attachment 1:" in units[1]["text"], units[1]["text"]  # non-text block marked
    nd = tmp / "chat.ndjson"
    nd.write_text("\n".join(_json.dumps(m) for m in convo), encoding="utf-8")
    ndu = ES._chat_units(nd, "chat.ndjson", "absorb")
    assert ndu and len(ndu) == 2 and ndu[1].get("follows"), ndu       # NDJSON chat too
    wp = tmp / "wrap.json"
    wp.write_text(_json.dumps({"messages": convo}), encoding="utf-8")
    assert ES._chat_units(wp, "wrap.json", "absorb"), "messages-wrapper recognized"
    op = tmp / "plain.json"
    op.write_text(_json.dumps([{"x": 1}, {"y": 2}]), encoding="utf-8")
    assert ES._chat_units(op, "plain.json", "absorb") is None, "ordinary array is not a chat"
    fp = tmp / "dump.md"
    fp.write_text("=== Human ===\nhi\n\n=== Assistant ===\nhello\n", encoding="utf-8")
    assert ES._has_role_markers(fp), "flat role-marker dump detected"
    print("PASS  chat: json/ndjson/messages -> per-message sections + follows; non-chat -> None")


def test_chat_reconcile():
    proj = Path(tempfile.mkdtemp(prefix="amg-chat-rc-"))
    try:
        import json as _json
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        (amg / "config.yml").write_text(
            "active: true\nworking_language: en\nabsorb_path: chat\n", encoding="utf-8")
        cdir = proj / "chat"
        cdir.mkdir()
        (cdir / "c.json").write_text(_json.dumps([
            {"role": "user", "content": "q", "id": "u1"},
            {"role": "assistant", "content": "a", "id": "a1"}]), encoding="utf-8")
        (cdir / "d.md").write_text("=== Human ===\nq2\n\n=== Assistant ===\na2\n", encoding="utf-8")
        rc.plan(proj, amg)
        nodes = rc.load_nodes(gs.GraphStore(amg))
        a1 = nodes.get("doc:chat/c.json::a1")
        assert a1 and a1["type"] == "section", sorted(nodes)
        assert any(e.get("rel") == "follows" and e.get("to") == "doc:chat/c.json::u1"
                   for e in a1["edges"]), a1["edges"]                 # structural adjacency edge
        flat = [n for n in nodes.values() if (n.get("source_path") or "").endswith("d.md")]
        assert flat and all(n["type"] == "section" for n in flat), flat
        # per-turn m{n} ids prove the session chunker ran (markdown would yield one _preamble)
        assert {n["qualname"] for n in flat} == {"m1", "m2"}, flat
        print("PASS  chat: reconcile builds section nodes + follows edges; flat dump -> session sections")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_reconcile_buckets():
    proj = Path(tempfile.mkdtemp(prefix="amg-chunk-rc-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        (amg / "config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: src\n", encoding="utf-8")
        src = proj / "src"
        src.mkdir()
        (src / "events.ndjson").write_text('{"id":"a","x":1}\n{"id":"b","x":2}\n', encoding="utf-8")
        (src / "routes.csv").write_text("p,h\n/a,f\n", encoding="utf-8")
        (src / "app.log").write_text(
            "2026-06-18 10:00:00 INFO up\n2026-06-18 10:00:01 WARN slow\n", encoding="utf-8")
        (src / "guide.rst").write_text("Title\n=====\nbody\n", encoding="utf-8")
        rc.plan(proj, amg)
        nodes = rc.load_nodes(gs.GraphStore(amg))
        paths = {n["_path"] for n in nodes.values()}
        assert any(p.startswith("nodes/data/") and "events" in p for p in paths), sorted(paths)
        assert any(p.startswith("nodes/data/") and "routes" in p for p in paths), sorted(paths)
        assert any(p.startswith("nodes/doc/") and "app" in p for p in paths), sorted(paths)
        assert any(p.startswith("nodes/doc/") and "guide" in p for p in paths), sorted(paths)
        types = {n["type"] for n in nodes.values()}
        assert {"record", "sheet", "block", "section"} & types, types
        print("PASS  reconcile: new-chunker units build nodes in correct buckets (data/doc)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="amg-chunkers-"))
    try:
        test_classify()
        test_ndjson(tmp)
        test_csv(tmp)
        test_log(tmp)
        test_rst(tmp)
        test_recursive_json(tmp)
        test_chat(tmp)
        test_chat_reconcile()
        test_reconcile_buckets()
        print("\nALL CHUNKER CHECKS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
