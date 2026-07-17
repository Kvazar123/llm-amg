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
  8. unclean  : session-start reports a healed stale lock, then stays silent.
  9. hint     : prompt-hint fires only past ALL gates (active+automation, task-shaped
                prompt, cooldown, pack log absent/stale); a quiet prompt gets nothing.
 10. start-check: the one-call start routine for event surfaces (plan + sync question
                under the deferral cadence; the advanced hook-JSON wire for Codex).
 11. oc-end   : the OpenCode session-end payload — an incremental dump under a
                session-stable filename, thinking/synthetic parts cut, usage from the
                payload's edited files, the pack-log consumption stamp.

Run:  python selftest_lifecycle.py
"""
import json
import os
import re
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


def case_prompt_hint() -> None:
    """The UserPromptSubmit reminder is gated four ways (active+automation; a
    task-shaped prompt; the cooldown stamp; this session's pack log absent or stale) —
    every gated-out prompt gets NOTHING, so the hook stays a signal, not a tax."""
    task = "x" * 250                                     # task-shaped (length gate)
    for cfg, why in ((CONFIG.replace("automation: true", "automation: false"), "automation off"),
                     (CONFIG.replace("active: true", "active: false"), "inactive")):
        proj = setup_project(cfg)
        try:
            assert LC.prompt_hint(amg_root(proj), task) is None, f"must be silent when {why}"
        finally:
            shutil.rmtree(proj, ignore_errors=True)
    proj = setup_project()
    try:
        amg = amg_root(proj)
        assert LC.prompt_hint(amg, "short prompt") is None, "a short prompt is never hinted"
        n1 = LC.prompt_hint(amg, task)
        assert n1 and "not been consulted" in n1, n1     # no pack log this session
        stamp = amg / "work" / "hint-stamp"
        assert stamp.exists(), "issuing a hint must touch the cooldown stamp"
        assert LC.prompt_hint(amg, task) is None, "cooldown: no second hint at once"
        old = time.time() - LC._HINT_COOLDOWN_S - 5
        os.utime(stamp, (old, old))                      # expire the cooldown
        pack_log = amg / "work" / "pack-log.jsonl"
        pack_log.write_text("{}\n", encoding="utf-8")    # a fresh pack: memory in use
        assert LC.prompt_hint(amg, task) is None, "a fresh pack log silences the hint"
        stale = time.time() - LC._HINT_PACK_STALE_S - 5
        os.utime(pack_log, (stale, stale))               # the pack no longer reflects focus
        n2 = LC.prompt_hint(amg, task)
        assert n2 and "min old" in n2, n2
        print("PASS  hint: gated on config/length/cooldown/pack age; quiet prompts stay silent")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_start_check() -> None:
    """start-check = the whole deterministic start routine as one entry point for
    event surfaces (the OpenCode plugin, the Codex SessionStart hook): heal + digest
    + the free reconcile half (plan) + the sync question under the deferral cadence.
    The note asks about a fresh backlog, falls silent while a recorded deferral
    stands, and wraps into the advanced hook JSON on demand (the wire Codex
    requires; Claude Code accepts the same shape)."""
    proj = setup_project()
    try:
        amg = amg_root(proj)
        res = LC.start_check(proj, amg)
        assert res.get("action") == "start-check", res
        assert res["plan"]["queued_for_semantic"] >= 1, res["plan"]
        note = res.get("note") or ""
        assert "await semantic enrichment" in note and "sync-defer" in note, note

        LC.sync_defer(amg)                       # the user defers at this backlog
        res2 = LC.start_check(proj, amg)
        assert "note" not in res2, f"a standing deferral must silence the question: {res2}"

        wire = LC._hook_json_wire("SessionStart", note)
        assert wire is not None
        parsed = json.loads(wire)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart", parsed
        assert parsed["hookSpecificOutput"]["additionalContext"] == note, parsed
        assert LC._hook_json_wire("UserPromptSubmit", None) is None, "no note -> no wire"
        print("PASS  start-check: heal+plan+sync question; deferral cadence; hook-JSON wire")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_session_end_opencode() -> None:
    """The OpenCode payload path: an inline transcript (SDK messages) lands under a
    SESSION-STABLE filename and each re-dump overwrites it (the incremental dump);
    synthetic/ignored/reasoning parts never reach the file; tool parts become
    attachment markers; the payload's edited files feed usage attribution, and the
    consumed pack log leaves the stamp that keeps the mid-session hint gate honest."""
    proj = setup_project()
    try:
        amg = amg_root(proj)

        def msg(role, text, extra_parts=()):
            return {"info": {"role": role, "time": {"created": 1752700000000}},
                    "parts": [{"type": "text", "text": text}, *extra_parts]}

        messages = [
            msg("user", "how does charge work?"),
            {"info": {"role": "user", "time": {"created": 1752700001000}},
             "parts": [{"type": "text", "text": "AMG: injected note",
                        "synthetic": True}]},           # our own injection: not dialogue
            msg("assistant", "It returns the card.",
                extra_parts=({"type": "reasoning", "text": "секретный черновик"},
                             {"type": "tool", "tool": "read"})),
        ]
        payload = {"format": "opencode", "session_id": "ses_abc123XY",
                   "created_ms": 1752700000000, "reason": "idle",
                   "messages": messages, "edited_files": []}
        res = LC.session_end(proj, amg, payload=payload)
        dump = res["session"]
        assert dump.get("turns") == 2, dump                  # synthetic-only msg dropped
        f = amg / dump["file"]
        assert f.exists() and "123XY" in f.name, f
        text = f.read_text(encoding="utf-8")
        assert "how does charge work?" in text and "It returns the card." in text
        assert "injected note" not in text, "synthetic parts must not reach the dump"
        assert "секретный черновик" not in text, "reasoning is never stored"
        assert "== Attachment 1: tool call (read) ==" in text, text

        messages.append(msg("user", "and refunds?"))         # the dialogue grows
        res2 = LC.session_end(proj, amg, payload=payload)
        assert res2["session"]["file"] == dump["file"], "re-dump must land on the SAME file"
        assert res2["session"]["turns"] == 3, res2["session"]
        assert "and refunds?" in f.read_text(encoding="utf-8")
        assert len(list(f.parent.glob("*.md"))) == 1, "no dated duplicates"

        # usage attribution from the payload's edited files + the pack-log stamp
        (amg / "work").mkdir(exist_ok=True)
        (amg / "work" / "pack-log.jsonl").write_text(json.dumps(
            {"pack": [{"id": "code:src/app.py::charge", "source_path": "src/app.py"}]})
            + "\n", encoding="utf-8")
        payload["edited_files"] = [str(proj / "src" / "app.py")]
        res3 = LC.session_end(proj, amg, payload=payload)
        assert res3["usage"].get("used") == 1, res3["usage"]
        assert (amg / "work" / "usage.log").exists()
        assert (amg / "work" / "pack-log-stamp").exists(), \
            "a mid-session consumer must leave the consumption stamp"
        # the stamp keeps the hint gate honest: fresh stamp -> silence
        assert LC.prompt_hint(amg, "x" * 250) is None, \
            "a fresh consumption stamp must read as 'consulted recently'"
        print("PASS  oc-end: stable-file incremental dump; thinking/synthetic cut; usage + stamp")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def case_status(proj: Path) -> None:
    rc.plan(proj, amg_root(proj))            # populate nodes + the work queue
    d = LC.status(proj, amg_root(proj))
    for k in ("engine_version", "active", "automation", "graph_root", "branch", "commit",
              "nodes", "stale", "pending_transactions", "stale_lock", "conflicts",
              "queue_size", "sync_deferred", "last_sync",
              "last_consolidation", "last_judged_consolidation",
              "weight_folds_since_judged", "consolidation_leftover", "eval_summary"):
        assert k in d, f"status missing field {k}"
    assert d["active"] is True and d["automation"] is True, d
    assert d["nodes"] >= 1, d
    assert d["queue_size"] is not None, "bootstrap wrote a queue -> size is known"
    assert d["last_sync"] and "reconcile" in d["last_sync"], \
        "plan wrote nodes -> the action log carries a reconcile line"
    text = LC.format_status(d)
    assert "AMG status (engine v" in text and "automation:" in text, text
    assert "git branch" in text and "conflicts:" in text, text
    assert "semantic queue:" in text and "last sync:" in text, text
    print("PASS  status: every field present (version, semantic queue, last sync); one-screen report")


def case_version_and_help() -> None:
    v = LC._engine_version()
    assert v and v != "unknown", "dev checkout: repo-root VERSION resolves"
    assert re.match(r"^\d+\.\d+\.\d+", v), f"SemVer expected, got {v!r}"
    assert "status" in LC.HELP_TEXT and "sync-defer" in LC.HELP_TEXT \
        and "retrieve" in LC.HELP_TEXT and "view" in LC.HELP_TEXT, "help lists every verb"
    print(f"PASS  version/help: engine v{v} resolves; help lists control and work verbs")


def case_sync_defer(proj: Path) -> None:
    amg = amg_root(proj)
    rc.plan(proj, amg)                        # ensure a queue exists
    res = LC.sync_defer(amg)
    assert res.get("action") == "sync-defer" and "queued" in res, res
    d = LC.status(proj, amg)
    sd = d.get("sync_deferred")
    assert sd and sd.get("queued") == res["queued"], (sd, res)
    if res["queued"]:
        assert "sync deferred at:" in LC.format_status(d), "deferral surfaces in the report"
    print("PASS  sync-defer: deferral recorded with the backlog size; status carries it")


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
        # the invariant audit rides on repair: the conflict-marker file is also an
        # unparsable node, so the audit flags it and the note names the sweep
        assert rep.get("audit", {}).get("verdict") == "attention", rep.get("audit")
        assert "Store audit:" in rep["note"], rep["note"]
        print("PASS  conflict: status lists the conflicted node; repair notes it + audits")
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
        case_start_check()
        case_session_end_opencode()
        case_status(proj)
        case_version_and_help()
        case_sync_defer(proj)
        case_consolidation_nudge()
        case_prompt_hint()
        case_on_off(proj)
        case_heal_note()
        case_unclean_shutdown()
        case_shared_folder_contention()
        case_merge_conflict_surfaced()
        print("\nALL LIFECYCLE CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
