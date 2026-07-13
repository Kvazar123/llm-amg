#!/usr/bin/env python3
"""
selftest_lifecycle.py — proves the lifecycle control plane (lifecycle.py + the digest).

Checks:
  1. digest   : write_digest selects active decisions + open questions (by salience),
                renders them, excludes plain notes, and emits a placeholder when empty.
  2. gate     : session-start / session-end are no-ops unless active AND automation on.
  3. start    : the session-start hook heals (recover + verify --repair), clean store.
  4. end      : the session-end hook folds weights and refreshes the digest.
  5. status   : the report carries every field (active, automation, counts, pending,
                lock, queue, last pack/consolidation) without reading files by hand.
  6. on/off   : /amg on|off flips `active` in config.yml in place; status reflects it.
  7. heal-note: format_heal_note is silent on a clean heal, summarizes otherwise.
  8. unclean  : session-start reports a healed stale lock (task 9), then stays silent.

Run:  python selftest_lifecycle.py
"""
import json
import shutil
import sys
import tempfile
import time
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


def case_consolidation_nudge() -> None:
    """The judged pass has no event of its own, so its overdue nudge is mechanical:
    actions.log arithmetic (weight folds since the last `consolidation applied`) plus
    a leftover plan/actions check — surfaced identically by session-start (the hook
    path) and by status (the hook-less path: Codex/generic read it in the loop)."""
    proj = setup_project()
    amg = amg_root(proj)
    try:
        fold = "[{ts}] tx-{n} consolidate | weights folded: apply_hebbian=False\n"
        log = amg / "actions.log"
        log.write_text("".join(fold.format(ts=f"2026-01-0{i}T10:00:00", n=i)
                               for i in (1, 2)), encoding="utf-8")
        st = LC._consolidation_state(amg)
        assert st["folds_since_judged"] == 2 and st["leftover"] == [], st
        assert LC._consolidation_note(st) is None, "two folds: not overdue yet"

        with open(log, "a", encoding="utf-8") as f:      # third fold crosses the bar
            f.write(fold.format(ts="2026-01-03T10:00:00", n=3))
        d = LC.status(proj, amg)
        assert d["weight_folds_since_judged"] == 3 and d["last_judged_consolidation"] is None, d
        assert d["consolidation_note"] and "no judgment consolidation for 3" in d["consolidation_note"], d
        assert "last judged pass" in LC.format_status(d), LC.format_status(d)
        res = LC.session_start(proj, amg)
        assert "no judgment consolidation" in res.get("note", ""), res

        with open(log, "a", encoding="utf-8") as f:      # a judged pass resets the count
            f.write("[2026-01-04T10:00:00] tx-9 consolidate | consolidation applied: {'promote': 1}\n")
        st = LC._consolidation_state(amg)
        assert st["folds_since_judged"] == 0 and st["last_judged"], st
        assert LC._consolidation_note(st) is None, st

        # an unapplied plan WRITTEN AFTER the judged pass = an interrupted judge run
        (amg / "work").mkdir(exist_ok=True)
        (amg / "work" / "consolidation-plan.json").write_text("{}", encoding="utf-8")
        st = LC._consolidation_state(amg)
        assert st["leftover"] == ["consolidation-plan.json"], st
        note = LC._consolidation_note(st)
        assert note and "unapplied" in note, note
        print("PASS  nudge: fold arithmetic + leftover plan; same note via start and status")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_status(proj: Path) -> None:
    rc.plan(proj, amg_root(proj))            # populate nodes + the work queue
    d = LC.status(proj, amg_root(proj))
    for k in ("active", "automation", "graph_root", "branch", "commit", "nodes", "stale",
              "pending_transactions", "stale_lock", "conflicts", "queue_size",
              "last_consolidation", "last_judged_consolidation",
              "weight_folds_since_judged", "consolidation_leftover", "eval_summary"):
        assert k in d, f"status missing field {k}"
    assert d["active"] is True and d["automation"] is True, d
    assert d["nodes"] >= 1, d
    assert d["queue_size"] is not None, "bootstrap wrote a queue -> size is known"
    text = LC.format_status(d)
    assert "AMG status" in text and "automation:" in text, text
    assert "git branch" in text and "conflicts:" in text, text
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


def case_heal_note() -> None:
    assert LC.format_heal_note({"recovered": [], "stale_lock_cleared": False}) is None
    n = LC.format_heal_note({"recovered": ["t1:redone", "t2:redone"],
                             "stale_lock_cleared": True})
    assert n and "2 unfinished" in n and "stale lock" in n and "notes.py" in n, n
    print("PASS  heal-note: silent on a clean heal; summarizes replays + stale lock otherwise")


def case_unclean_shutdown() -> None:
    proj = setup_project()
    try:
        amg = amg_root(proj)
        store = gs.GraphStore(amg)
        store.init()
        # plant a stale lock as if a prior session died holding it: a foreign host + an
        # old ts, so it reads stale by the AGE threshold (the cross-host-safe path; a
        # same-host dead pid is the other stale path, covered in selftest_graph_store).
        gs.atomic_write_text(store.lock_path, json.dumps(
            {"pid": 999999, "host": "dead-host", "ts": time.time() - 7200}))
        res = LC.session_start(proj, amg)
        assert res.get("stale_lock_cleared") is True, res
        assert res.get("note") and "stale lock" in res["note"], res
        assert not store.lock_path.exists(), "heal must clear the stale lock"
        # a clean start right after is silent: nothing healed, no note
        res2 = LC.session_start(proj, amg)
        assert "note" not in res2 and res2.get("stale_lock_cleared") is False, res2
        print("PASS  unclean: session-start reports a healed stale lock, then stays silent")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_shared_folder_contention() -> None:
    """On a SHARED FOLDER a live writer (possibly another machine) holds the
    lock; the host-aware rule no longer steals it. The automatic maintenance entry points
    must DEGRADE (skip), not crash. This holds in EVERY environment — they are plain
    function/script calls, so a hook-less or Codex env (no SessionStart/End hook) runs the
    same code via the model-driven loop, not a hook."""
    proj = setup_project()
    try:
        amg = amg_root(proj)
        store = gs.GraphStore(amg); store.init()
        foreign = gs.socket.gethostname() + "-foreign"           # guaranteed != this host
        gs.atomic_write_text(store.lock_path, json.dumps(
            {"pid": 1, "host": foreign, "ts": time.time()}))     # a LIVE foreign lock
        start = LC.session_start(proj, amg)
        assert start.get("skipped") == "another writer holds the lock", start
        assert (amg / "digest.md").exists(), "the digest still refreshes (it is lock-free)"
        rep = LC.repair(proj, amg)
        assert rep.get("skipped") and rep.get("note") and "another writer" in rep["note"].lower(), rep
        end = LC.session_end(proj, amg)
        assert end["weights"].get("skipped") == "another writer holds the lock", end
        assert store.lock_path.exists(), "the live foreign lock was never stolen"
        print("PASS  shared-folder: hooks + repair degrade on a live foreign lock, no crash")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_merge_conflict_surfaced() -> None:
    """A node carrying git merge markers is SURFACED by status (a conflicts list)
    and by repair (a note), so the user knows to resolve it — even though load_nodes skips
    it and the rest of the graph keeps working."""
    proj = setup_project()
    try:
        amg = amg_root(proj)
        store = gs.GraphStore(amg); store.init()
        bad = ("---\nid: code:src/x.py::h\n<<<<<<< HEAD\nsummary: a\n=======\n"
               "summary: b\n>>>>>>> feat\ntype: function\n---\n")
        gs.atomic_write_text(store.root / "nodes" / "code" / "torn-cafef00d.md", bad)
        st = LC.status(proj, amg)
        assert st.get("conflicts") == ["nodes/code/torn-cafef00d.md"], st.get("conflicts")
        assert "conflicts:" in LC.format_status(st), "status report must show a conflicts line"
        rep = LC.repair(proj, amg)
        assert rep.get("note") and "merge markers" in rep["note"], rep
        print("PASS  conflict: status lists the conflicted node; repair notes it for the user")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    proj = setup_project()
    try:
        case_digest(proj)
        case_digest_empty()
        case_gate()
        case_session_start(proj)
        case_session_end(proj)
        case_status(proj)
        case_consolidation_nudge()
        case_on_off(proj)
        case_heal_note()
        case_unclean_shutdown()
        case_shared_folder_contention()
        case_merge_conflict_surfaced()
        print("\nALL LIFECYCLE CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
