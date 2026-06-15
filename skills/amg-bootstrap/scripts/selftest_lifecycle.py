#!/usr/bin/env python3
"""
selftest_lifecycle.py — proves the Stage 8 control plane (lifecycle.py + the digest).

Checks:
  1. digest   : write_digest selects active decisions + open questions (by salience),
                renders them, excludes plain notes, and emits a placeholder when empty.
  2. gate     : session-start / session-end are no-ops unless active AND automation on.
  3. start    : the session-start hook heals (recover + verify --repair), clean store.
  4. end      : the session-end hook folds weights and refreshes the digest.
  5. status   : the report carries every field (active, automation, counts, pending,
                lock, queue, last pack/consolidation) without reading files by hand.
  6. on/off   : /amg on|off flips `active` in config.yml in place; status reflects it.

Run:  python selftest_lifecycle.py
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
import notes as NT
import lifecycle as LC
import consolidate as CO

CONFIG = ("active: true\nautomation: true\nworking_language: ru\nmirror_path: src\n"
          "retrieval:\n  embeddings:\n    enabled: off\n")
PY_SRC = "def charge(card):\n    return card\n"


def setup_project(config: str = CONFIG) -> Path:
    proj = Path(tempfile.mkdtemp(prefix="amg-life-"))
    amg = proj / ".claude" / "amg"
    amg.mkdir(parents=True)
    (amg / "config.yml").write_text(config, encoding="utf-8")
    src = proj / "src"
    src.mkdir()
    (src / "app.py").write_text(PY_SRC, encoding="utf-8")
    return proj


def amg_root(proj: Path) -> Path:
    return proj / ".claude" / "amg"


def case_digest(proj: Path) -> None:
    amg = amg_root(proj)
    NT.add_note(proj, "decision", "use a single-writer lock for all graph writes",
                amg_root=amg)
    NT.add_note(proj, "open_question", "should embeddings default on for ru projects",
                amg_root=amg)
    NT.add_note(proj, "note", "ZZ plain working note about nothing", amg_root=amg)

    res = CO.write_digest(proj, amg)
    assert res["decisions"] == 1 and res["open_questions"] == 1, res
    text = (amg / "digest.md").read_text(encoding="utf-8")
    assert "single-writer lock" in text, "decision must be in the digest"
    assert "embeddings default on" in text, "open question must be in the digest"
    assert "**decision**" in text and "**open_question**" in text, text
    assert "ZZ plain working note" not in text, "a plain note is NOT a digest item"
    print("PASS  digest: active decisions + open questions surfaced; plain note excluded")


def case_digest_empty() -> None:
    proj = setup_project()
    try:
        CO.write_digest(proj, amg_root(proj))
        text = (amg_root(proj) / "digest.md").read_text(encoding="utf-8")
        assert "No active decisions or open questions" in text, text
        print("PASS  digest: a graph with no decisions/questions yields a placeholder")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_gate() -> None:
    for cfg, why in ((CONFIG.replace("automation: true", "automation: false"), "automation off"),
                     (CONFIG.replace("active: true", "active: false"), "inactive")):
        proj = setup_project(cfg)
        try:
            amg = amg_root(proj)
            assert "skipped" in LC.session_start(proj, amg), f"start must skip when {why}"
            assert "skipped" in LC.session_end(proj, amg), f"end must skip when {why}"
        finally:
            shutil.rmtree(proj, ignore_errors=True)
    print("PASS  gate: hooks are no-ops unless active AND automation on")


def case_session_start(proj: Path) -> None:
    res = LC.session_start(proj, amg_root(proj))
    assert res.get("action") == "session-start", res
    assert res["verify"]["pending_transactions"] == [], res
    assert not res["verify"]["stale_lock"], res
    assert "digest" in res and (amg_root(proj) / "digest.md").exists(), res
    print("PASS  start: session-start heals (recover + verify --repair) + refreshes digest")


def case_session_end(proj: Path) -> None:
    res = LC.session_end(proj, amg_root(proj))
    assert res.get("action") == "session-end", res
    assert "hebbian_applied" in res["weights"], res
    assert (amg_root(proj) / "digest.md").exists(), "session-end must refresh the digest"
    print("PASS  end: session-end folds weights and refreshes the digest")


def case_status(proj: Path) -> None:
    rc.plan(proj, amg_root(proj))            # populate nodes + the work queue
    d = LC.status(proj, amg_root(proj))
    for k in ("active", "automation", "graph_root", "nodes", "stale",
              "pending_transactions", "stale_lock", "queue_size",
              "last_consolidation", "eval_summary"):
        assert k in d, f"status missing field {k}"
    assert d["active"] is True and d["automation"] is True, d
    assert d["nodes"] >= 1, d
    assert d["queue_size"] is not None, "bootstrap wrote a queue -> size is known"
    text = LC.format_status(d)
    assert "AMG status" in text and "automation:" in text, text
    print("PASS  status: every field present; renders a one-screen report")


def case_on_off(proj: Path) -> None:
    amg = amg_root(proj)
    off = LC.set_active(amg, False)
    assert off["active"] is False, off
    assert LC.status(proj, amg)["active"] is False, "off must flip active in config.yml"
    on = LC.set_active(amg, True)
    assert on["active"] is True, on
    assert LC.status(proj, amg)["active"] is True, "on must flip it back"
    # the flip preserved the rest of the template (a comment line and another key)
    cfg_text = (amg / "config.yml").read_text(encoding="utf-8")
    assert "working_language: ru" in cfg_text and "mirror_path: src" in cfg_text, cfg_text
    print("PASS  on/off: /amg on|off flips active in place, other config preserved")


if __name__ == "__main__":
    proj = setup_project()
    try:
        case_digest(proj)
        case_digest_empty()
        case_gate()
        case_session_start(proj)
        case_session_end(proj)
        case_status(proj)
        case_on_off(proj)
        print("\nALL LIFECYCLE CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
