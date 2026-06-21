#!/usr/bin/env python3
"""
selftest_usage.py — usage provenance (Stage 13, task 9): retrieve's pack log crossed with
the session's edited files -> work/usage.log, kept SEPARATE from coactivation.log.

Checks:
  1. pack log   : a retrieve run with logging on records the pack composition
                  (id + source_path) to work/pack-log.jsonl; a --no-pack run writes none.
  2. usage      : session_end intersects the files the transcript's Edit tool touched with
                  the logged packs -> usage.log lists the USED nodes (source edited),
                  excludes a node whose source was untouched, and carries an outcome.
  3. consume    : the session-scoped pack log is cleared after session_end.
  4. separation : usage.log is distinct from coactivation.log and survives the weight
                  fold (consolidate does NOT read or remove it).

Embeddings are forced off (no model download). Run:  python selftest_usage.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                                            # gs/rc/lifecycle
sys.path.insert(0, str(HERE.parents[1] / "amg-retrieve" / "scripts"))   # retrieve/embed

import embed                      # noqa: E402
import graph_store as gs          # noqa: E402
import reconcile as rc            # noqa: E402
import retrieve as R              # noqa: E402
import lifecycle as LC            # noqa: E402

QUERY = "process request handler"
FA, FB = "code:src/a.py::fa", "code:src/b.py::fb"
MA, MB = "code:src/a.py", "code:src/b.py"


def amg_of(proj: Path) -> Path:
    return proj / ".claude" / "amg"


def setup() -> Path:
    proj = Path(tempfile.mkdtemp(prefix="amg-usage-"))
    amg = amg_of(proj)
    amg.mkdir(parents=True)
    (amg / "config.yml").write_text(
        "active: true\nautomation: true\nworking_language: en\nmirror_path: src\n"
        "retrieval:\n  embeddings:\n    enabled: off\n", encoding="utf-8")
    src = proj / "src"
    src.mkdir()
    (src / "a.py").write_text("def fa():\n    return 'alpha'\n", encoding="utf-8")
    (src / "b.py").write_text("def fb():\n    return 'beta'\n", encoding="utf-8")
    rc.plan(proj, amg)
    work = amg / "work"
    work.mkdir(exist_ok=True)
    nodes = rc.load_nodes(gs.GraphStore(amg))
    items = [{"id": nid, "summary": f"process request handler {nid.split('::')[-1] or 'module'}",
              "content_sha": n["source_hash"]}
             for nid, n in nodes.items() if n.get("source_kind") == "derived_from_file"]
    (work / "d.json").write_text(json.dumps(items), encoding="utf-8")
    rc.apply_derivation(proj, work / "d.json", amg)
    return proj


def _transcript(proj: Path, edit_rel: str) -> Path:
    """A minimal Claude Code .jsonl: one user turn, one assistant turn that edits a file
    (absolute path, as the real Edit tool emits)."""
    abs_path = str((proj / edit_rel).resolve())
    lines = [
        {"type": "user", "message": {"role": "user", "content": "fix the handler"},
         "timestamp": "2026-06-21T10:00:00"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Editing the handler now."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": abs_path}}]},
         "timestamp": "2026-06-21T10:00:05"},
    ]
    tp = proj / "transcript.jsonl"
    tp.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return tp


def case_pack_log(proj: Path) -> None:
    amg = amg_of(proj)
    pack_log = amg / "work" / "pack-log.jsonl"
    assert not pack_log.exists(), "no pack log before any retrieve"
    R.retrieve(amg, QUERY, write_pack=False, log_coactivation=False)
    assert not pack_log.exists(), "a --no-pack run must not write the pack log"
    R.retrieve(amg, QUERY, write_pack=True, log_coactivation=True)
    assert pack_log.exists(), "a logged retrieve must write the pack log"
    rec = json.loads(pack_log.read_text(encoding="utf-8").splitlines()[0])
    ids = {it["id"] for it in rec["pack"]}
    paths = {it["id"]: it["source_path"] for it in rec["pack"]}
    assert FA in ids and FB in ids, ids                  # both files' nodes were packed
    assert paths[FA] == "src/a.py" and paths[FB] == "src/b.py", paths
    print("PASS  pack log: logged retrieve records id+source_path; --no-pack writes none")


def case_usage_and_consume(proj: Path) -> None:
    amg = amg_of(proj)
    tp = _transcript(proj, "src/a.py")               # the session edits ONLY a.py
    res = LC.session_end(proj, amg, transcript_path=str(tp), reason="clear")
    usage = amg / "work" / "usage.log"
    assert usage.exists(), f"session_end must write usage.log; got {res.get('usage')}"
    rec = json.loads(usage.read_text(encoding="utf-8").splitlines()[-1])
    used = set(rec["used"])
    assert FA in used and MA in used, ("a.py nodes used (source edited)", used)
    assert FB not in used and MB not in used, ("b.py untouched -> not used", used)
    assert rec["edited_files"] == ["src/a.py"], rec["edited_files"]
    assert rec.get("outcome") == "completed", rec
    assert not (amg / "work" / "pack-log.jsonl").exists(), "pack log consumed at session_end"
    print("PASS  usage: edited-file nodes are used, untouched excluded; pack log consumed")


def case_separation(proj: Path) -> None:
    """usage.log is a different file from coactivation.log, and the weight fold (which
    rotates coactivation.log) leaves usage.log intact — consolidate does not read it."""
    amg = amg_of(proj)
    usage = amg / "work" / "usage.log"
    assert usage.exists(), "precondition: usage.log written by the prior case"
    before = usage.read_text(encoding="utf-8")
    sys.path.insert(0, str(HERE.parents[1] / "amg-consolidate" / "scripts"))
    import consolidate as CO
    CO.fold_weights(proj, amg)                        # rotates coactivation, must ignore usage
    assert usage.read_text(encoding="utf-8") == before, "fold_weights must not touch usage.log"
    assert usage.name != "coactivation.log", "distinct from the blind co-activation signal"
    print("PASS  separation: usage.log distinct from coactivation.log; survives weight fold")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    orig = embed.get_embedder
    embed.get_embedder = lambda cfg: None              # force pure BM25, deterministic
    proj = setup()
    try:
        case_pack_log(proj)
        case_usage_and_consume(proj)
        case_separation(proj)
        print("\nALL USAGE CHECKS PASSED")
    finally:
        embed.get_embedder = orig
        shutil.rmtree(proj, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
