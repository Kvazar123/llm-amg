#!/usr/bin/env python3
"""
selftest_sessions.py — proves Stage 9 session capture end to end.

Checks:
  1. chunk+ignore : a dump under <store>/sessions is chunked into per-turn units even
                    though the store sits under the ignored agent dir AND .gitignore
                    lists it (audit 1.18); a normal gitignored source file is still
                    dropped (no regression).
  2. ingest       : those units land in the graph as ordinary doc nodes via reconcile.
  3. portability  : the sessions path DERIVES from the resolved store, so it works under
                    a non-.claude agent dir; mirror policy + the no-marker fallback hold.
  4. dump         : lifecycle renders a real Claude Code .jsonl transcript into the
                    shared role-marker format — thinking cut, attachments counted, meta
                    and tool noise filtered — and the chunker re-parses it (round trip).

Run:  python selftest_sessions.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "amg-consolidate" / "scripts"))

import graph_store as gs
import reconcile as rc
import extract_structure as ex
import lifecycle as LC

SESSION_MD = """---
session: 2026-06-16-1200
turns: 2
---

=== Human ===
How does the lock work?

=== Assistant ===
A single-writer lock serializes graph writes.

== Attachments 1 ==
"""


def setup():
    proj = Path(tempfile.mkdtemp(prefix="amg-sess-"))
    amg = proj / ".claude" / "amg"
    (amg / "sessions").mkdir(parents=True)
    (amg / "config.yml").write_text(
        "active: true\nworking_language: ru\nmirror_path: src\nsession_policy: absorb\n",
        encoding="utf-8")
    (proj / ".gitignore").write_text(".claude/\nsecret.py\n", encoding="utf-8")
    src = proj / "src"
    src.mkdir()
    (src / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (src / "secret.py").write_text("KEY = 'x'\n", encoding="utf-8")      # gitignored
    (amg / "sessions" / "2026-06-16-1200.md").write_text(SESSION_MD, encoding="utf-8")
    return proj, amg


def case_chunk_and_ignore(proj, amg):
    cfg = ex.load_config(amg)
    units = ex.extract(proj, cfg, amg)
    by_id = {u["id"]: u for u in units}
    sess = [u for u in units if u.get("lang") == "session"]
    assert len(sess) == 2, [u["id"] for u in sess]                       # two turns
    sid = "doc:.claude/amg/sessions/2026-06-16-1200.md::m1"
    assert sid in by_id, sorted(by_id)
    assert by_id[sid]["kind"] == "section" and by_id[sid]["category"] == "doc", by_id[sid]
    assert by_id[sid]["policy"] == "absorb", by_id[sid]
    # the 1.18 fix beats DEFAULT_IGNORE_DIRS (.claude) AND .gitignore (.claude/);
    # but a gitignored NORMAL source file is still dropped (no regression).
    assert not any(u["source_path"].endswith("secret.py") for u in units), \
        "gitignore must still guard normal sources"
    assert any(u["source_path"] == "src/app.py" for u in units), "normal source still ingested"
    print("PASS  sessions: dumps under the store are chunked (1.18); gitignore still guards normal sources")


def case_ingest(proj, amg):
    rc.plan(proj, amg)
    nodes = rc.load_nodes(gs.GraphStore(amg))
    sess_nodes = [n for n in nodes.values() if "/sessions/" in (n.get("source_path") or "")]
    assert len(sess_nodes) == 2, [n["id"] for n in sess_nodes]
    for n in sess_nodes:
        assert n["source_kind"] == "derived_from_file" and n["policy"] == "absorb", n
        assert n["type"] == "section" and n["status"] == "stale", n
    print("PASS  sessions: ingested into the graph as ordinary doc nodes")


def case_portability_and_fallback():
    proj = Path(tempfile.mkdtemp(prefix="amg-sess2-"))
    try:
        amg = proj / ".agents" / "amg"           # NOT .claude: prove the derived path
        (amg / "sessions").mkdir(parents=True)
        (amg / "config.yml").write_text("active: true\nsession_policy: mirror\n", encoding="utf-8")
        (amg / "sessions" / "raw.md").write_text("no role markers here\njust text\n", encoding="utf-8")
        cfg = ex.load_config(amg)
        units = ex.extract(proj, cfg, amg)
        sess = [u for u in units if u.get("lang") == "session"]
        assert len(sess) == 1 and sess[0]["kind"] == "file", sess        # no-marker fallback
        assert sess[0]["policy"] == "mirror", sess[0]
        assert sess[0]["source_path"] == ".agents/amg/sessions/raw.md", sess[0]
        print("PASS  sessions: derived path works under a non-.claude agent dir; mirror + fallback")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_dump_round_trip():
    """A synthetic but real-shaped Claude Code .jsonl -> dump -> chunk."""
    proj = Path(tempfile.mkdtemp(prefix="amg-sess3-"))
    try:
        amg = proj / ".claude" / "amg"
        (amg / "sessions").mkdir(parents=True)
        (amg / "config.yml").write_text("active: true\nautomation: true\n", encoding="utf-8")
        tx = proj / "transcript.jsonl"
        rows = [
            {"type": "system", "content": "boot"},                      # skipped
            {"type": "user", "isMeta": True,
             "message": {"role": "user", "content": "<local-command-caveat>noise"}},  # meta
            {"type": "user",
             "message": {"role": "user", "content": "Explain the lock."},
             "timestamp": "2026-06-16T10:00:00"},
            {"type": "assistant",
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "secret reasoning", "signature": "x"},
                 {"type": "text", "text": "A single-writer lock serializes writes."},
                 {"type": "tool_use", "name": "Bash", "id": "t1", "input": {"command": "ls"}}]},
             "timestamp": "2026-06-16T10:00:05"},
            {"type": "user",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},   # attachment
        ]
        tx.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

        res = LC.session_end(proj, amg, transcript_path=str(tx), reason="clear")
        assert res["action"] == "session-end", res
        dumped = res["session"]
        assert dumped.get("turns") == 2, dumped                          # human + assistant
        assert dumped.get("attachments") == 2, dumped                    # tool_use + tool_result
        f = amg / dumped["file"]
        text = f.read_text(encoding="utf-8")
        assert "secret reasoning" not in text, "raw thinking must be cut"
        assert "=== Human ===" in text and "=== Assistant ===" in text, text
        assert "== Attachments" in text, text
        assert "<local-command-caveat>" not in text, "meta wrapper must be filtered"
        # round trip: the chunker re-parses the dump the writer produced
        cfg = ex.load_config(amg)
        units = [u for u in ex.extract(proj, cfg, amg) if u.get("lang") == "session"]
        assert len(units) == 2, [u["id"] for u in units]
        print("PASS  sessions: .jsonl dumped to shared format (thinking cut, attachments counted); round-trips")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    proj, amg = setup()
    try:
        case_chunk_and_ignore(proj, amg)
        case_ingest(proj, amg)
        case_portability_and_fallback()
        case_dump_round_trip()
        print("\nALL SESSION CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
