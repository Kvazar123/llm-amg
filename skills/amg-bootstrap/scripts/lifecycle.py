#!/usr/bin/env python3
"""
lifecycle.py — AMG lifecycle & control plane: the session hooks, the /amg commands,
and the status report. Thin orchestration over the existing scripts (graph_store for
healing, consolidate for weights + the always-on digest); it adds no new graph logic.

Three AUTOMATIC entry points, wired by the installer into the agent dir's settings.json
as Claude Code hooks. They self-gate on config, so they are no-ops unless AMG is both
active and `automation: on` (turning automation off leaves only the manual commands):

  session-start : heal the store (recover + verify --repair) and refresh the digest
                  before task work (so the entry point's import is current and exists);
                  report an unclean prior shutdown when one is healed, else stay silent.
  session-end   : fold the co-activation log into weights (deterministic; with
                  apply_hebbian off this only ACCUMULATES coact, never mutating
                  conductance), refresh the digest, dump the session transcript to
                  <store>/sessions (from the hook's stdin payload), and record
                  USAGE provenance (work/usage.log: pack nodes whose source the session
                  edited, with a coarse outcome — the non-circular substrate
                  for the outcome-gated Hebbian rule). The JUDGMENT half of consolidation — the
                  amg-consolidator subagent + apply — stays model-driven: a hook cannot
                  run a subagent, so it lives in the activation loop, not here.
  prompt-hint   : the GATED mid-session reminder under UserPromptSubmit — one short
                  line only when the memory has demonstrably gone unconsulted while a
                  task-shaped prompt arrives (see prompt_hint); on every other prompt
                  it prints nothing and injects zero tokens.

The MANUAL commands, exposed as the `/amg <sub>` slash command (and reachable by
verbal intent through the activation block):

  on / off   : flip `active` in config.yml (turn AMG on/off for this project).
  start-check: the WHOLE deterministic start routine as one entry point — heal +
               digest (session-start) + the free reconcile half (plan) + the sync
               question under the deferral cadence. Built for event surfaces that
               can inject context but have no block-driven model half at session
               start: the OpenCode plugin and the Codex SessionStart hook run it and
               inject its printed note. Claude Code keeps its split on purpose (the
               hook heals; the model runs `plan` per the block).
  repair     : recover + verify --repair + the store invariant audit (heal on demand;
               the manual analog of the session-start hook — and it runs regardless
               of the automation gate; the audit half only reports, with the full
               sampled report on `reconcile.py audit`).
  status     : a one-screen report (engine version, active, automation, graph root,
               node/stale counts, pending transactions, stale lock, semantic queue,
               sync deferral, last sync/pack/consolidation, eval summary) so the user
               sees state without reading files; presented verbatim by every env.
  version    : the installed engine version (VERSION shipped inside this skill).
  sync-defer : record "the user deferred the offered sync" with the backlog size, so
               the start-of-session question is not repeated at the same size.
  help       : the user-facing verb list (control verbs here, work verbs = skills/loop).

Every automatic operation thus has a manual analog (DoD): healing <-> /amg repair,
weight folding <-> the amg-consolidate skill, the digest <-> consolidate.py digest.

CLI:
  python lifecycle.py session-start|session-end     [<project_root>] [--root <agent_dir>]
  python lifecycle.py status|repair|on|off          [<project_root>] [--root <agent_dir>]
  python lifecycle.py version|help|sync-defer       [<project_root>] [--root <agent_dir>]
  python lifecycle.py start-check|prompt-hint       [<project_root>] [--root <agent_dir>]
  session-end also accepts the hook's JSON on stdin, or --transcript <path> to dump a
  session manually in an environment without the SessionEnd hook; prompt-hint reads
  the UserPromptSubmit payload (the `prompt` field) the same way. A session-end stdin
  payload with `"format": "opencode"` carries the transcript INLINE (the OpenCode
  plugin fetches the messages over the SDK and pipes them) plus the session's edited
  files; the dump then lands under a session-stable filename, so each re-dump
  overwrites the same file — an incremental, crash-proof transcript.
  `--hook-json` on start-check / prompt-hint wraps the note in the advanced
  hook-output JSON (`hookSpecificOutput.additionalContext`) — the shape Claude Code
  defines and Codex requires (Codex parses hook stdout as JSON only; plain text is
  ignored there, unlike Claude Code).

The graph root is <agent_dir>/amg, resolved by graph_store.resolve_amg_root (the same
chain as reconcile/consolidate/notes).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import graph_store as gs
import reconcile as rc
from extract_structure import (session_attachment_marker, session_dir,
                               session_role_marker)

# Cross-skill import of consolidate for weights + digest — the established pattern in
# this codebase (consolidate itself imports graph_store from amg-bootstrap, and the
# eval harness from amg-retrieve, the same way).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "amg-consolidate" / "scripts"))
import consolidate as co                                   # noqa: E402

try:
    import yaml
except ImportError:                                        # pragma: no cover
    sys.stderr.write("lifecycle.py needs PyYAML: pip install pyyaml\n")
    raise

# Windows consoles default to cp1252; force UTF-8 so Cyrillic summaries print.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass


# --------------------------------------------------------------------------- #
# Config gate (active + automation)
# --------------------------------------------------------------------------- #

def _read_config(amg: Path) -> Dict[str, Any]:
    f = amg / "config.yml"
    if f.exists():
        try:
            return yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
    return {}


def _is_active(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("active", False))


def _automation_on(cfg: Dict[str, Any]) -> bool:
    """automation true/false (boolean, like `active`). Default ON (the shipped template
    sets `automation: true`; an absent key on an older config means on). Tolerates a
    string `on`/`off` too — YAML already parses a bare on/off as a bool, this just
    covers a quoted value."""
    v = cfg.get("automation", True)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("off", "false", "no", "0")


# --------------------------------------------------------------------------- #
# Healing (shared by the session-start hook and the /amg repair command)
# --------------------------------------------------------------------------- #

def _heal(amg: Path) -> Dict[str, Any]:
    """recover + verify --repair under a single lock. Idempotent and cheap; this is
    what makes a crashed or interrupted prior session a non-event.

    A read-only probe runs FIRST, before the lock: lock() steals a stale lock on
    acquisition and recover() empties the journal, so by the time we hold the lock the
    evidence of an unclean shutdown is already gone. The probe is what lets
    session-start / repair report that they healed one.

    On a SHARED FOLDER a live writer (possibly on another machine) may legitimately hold
    the lock; the host-aware staleness rule (graph_store) no longer steals it.
    Rather than crash the session, skip this heal cycle — healing is idempotent and runs
    on the next start/repair once the lock frees."""
    store = gs.GraphStore(amg)
    store.init()
    pre = store.verify(repair=False)
    try:
        with store.lock():
            recovered = store.recover()
            problems = store.verify(repair=True)
    except gs.StoreLockError as exc:
        return {"recovered": [], "verify": pre, "stale_lock_cleared": False,
                "skipped": "another writer holds the lock", "lock_note": str(exc)}
    return {"recovered": recovered, "verify": problems,
            "stale_lock_cleared": bool(pre.get("stale_lock"))}


def format_heal_note(healed: Dict[str, Any]) -> Optional[str]:
    """A friendly one-liner when a prior unclean shutdown was just healed, else
    None — so a clean session-start stays silent (no per-session noise).
    A hard kill (closed terminal, killed process) skips SessionEnd, so the store
    self-heals on the next start; this surfaces that it happened, in plain words."""
    recovered = healed.get("recovered") or []
    stale = bool(healed.get("stale_lock_cleared"))
    if not recovered and not stale:
        return None
    parts = []
    if recovered:
        parts.append(f"replayed {len(recovered)} unfinished transaction(s)")
    if stale:
        parts.append("cleared a stale lock")
    return ("Previous session ended uncleanly: " + "; ".join(parts)
            + ". Recovered automatically — continuing. Any notes captured via notes.py "
            "are intact (each is its own committed transaction).")


# --------------------------------------------------------------------------- #
# Session transcript dump
#
# Claude Code's SessionEnd hook pipes JSON on stdin with the path to the session
# .jsonl; we render it to <store>/sessions as a role-marked markdown dump (the same
# format the session chunker reads), which the next bootstrap ingests like any source.
# Parsing the .jsonl is Claude-Code-specific BY NATURE; in an environment without that
# transcript the portable "don't lose the dialogue" guarantee is capturing notes as
# you go (notes.py) — see the activation block.
# --------------------------------------------------------------------------- #

# Harness-injected user-message wrappers (enumerated from real Claude Code
# transcripts): slash-command plumbing, the `!` bash mode, and task notifications.
# These are mechanical, not dialogue, so they are dropped from the dump. Genuine human
# text never starts with one of these tags; an unknown future wrapper degrades to a
# turn rather than crashing.
_WRAPPER_PREFIXES = ("<local-command", "<command-name", "<command-message",
                     "<command-args", "<command-contents", "<bash-input",
                     "<bash-stdout", "<bash-stderr", "<task-notification")

# Tool calls that EDIT a file; their input names the path -> usage attribution.
# A few non-Claude-Code synonyms are tolerated for portability.
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit",
               "create_file", "edit_file", "apply_patch", "update_file"}
_EDIT_PATH_KEYS = ("file_path", "path", "notebook_path")


def _is_wrapper_text(s: str) -> bool:
    """A user 'message' that is a Claude Code wrapper (slash command, bash mode, task
    notification), not text the human actually typed."""
    t = s.lstrip()
    return any(t.startswith(p) for p in _WRAPPER_PREFIXES)


def _render_transcript(transcript_path: Path) -> Optional[Dict[str, Any]]:
    """Parse a Claude Code .jsonl transcript into a role-marked markdown body.

    Keeps human and assistant TEXT; cuts raw `thinking` (private reasoning, never
    stored); counts tool calls / results / images as attachments (a `== Attachments
    N ==` marker, not reproduced); drops meta entries and system/attachment plumbing.
    Returns {markdown, turns, attachments, started, ended} or None when the file is
    unreadable or holds no real dialogue (so an empty session writes no file).
    """
    try:
        raw = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    out: List[str] = []
    turns = 0
    attach_seq = 0
    pending: List[str] = []                        # attachment labels awaiting a flush
    started: Optional[str] = None
    ended: Optional[str] = None
    edited: List[str] = []                          # files an edit/write tool touched (usage.log)

    def flush_attach() -> None:
        nonlocal attach_seq
        for label in pending:                      # one numbered marker PER attachment
            attach_seq += 1
            out.append(session_attachment_marker(attach_seq, label))
        pending.clear()

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") not in ("user", "assistant") or o.get("isMeta"):
            continue
        msg = o.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        texts: List[str] = []
        att: List[str] = []                        # this entry's omitted attachments
        if isinstance(content, str):
            if _is_wrapper_text(content):
                continue
            if content.strip():
                texts.append(content.strip())
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    t = (b.get("text") or "").strip()
                    if t:
                        texts.append(t)
                elif bt in ("thinking", "redacted_thinking"):
                    continue                       # cut raw model reasoning
                elif bt == "tool_use":
                    name = b.get("name") or "tool"
                    att.append(f"tool call ({name})")
                    if name in _EDIT_TOOLS and isinstance(b.get("input"), dict):
                        for k in _EDIT_PATH_KEYS:
                            if b["input"].get(k):
                                edited.append(str(b["input"][k]))
                                break
                elif bt == "tool_result":
                    att.append("tool result")
                elif bt == "image":
                    att.append("image")
                else:
                    att.append(str(bt or "attachment"))   # file / unknown blob
        else:
            continue
        ts = o.get("timestamp")
        if ts:
            started = started or ts
            ended = ts
        joined = "\n\n".join(texts).strip()
        if joined:
            flush_attach()                         # attachments accrued before this turn
            role = "Assistant" if msg.get("role") == "assistant" else "Human"
            out.append(session_role_marker(role))
            out.append(joined)
            turns += 1
        pending.extend(att)                        # this entry's own attachments follow it
    flush_attach()
    if turns == 0:
        return None
    return {"markdown": "\n\n".join(out).rstrip() + "\n", "turns": turns,
            "attachments": attach_seq, "started": started, "ended": ended,
            "edited": edited}


def _render_transcript_opencode(messages: List[Any]) -> Optional[Dict[str, Any]]:
    """Parse an OpenCode message list (the SDK's `session.messages` response, piped in
    by the AMG plugin) into the same role-marked markdown body `_render_transcript`
    produces for Claude Code — one renderer per transcript format, one dump format.
    Format parsers live HERE deliberately: the alternative — each environment's
    adapter pre-chewing a neutral format — would push the storage policy (thinking
    is never stored; synthetic plumbing is not dialogue) into per-environment shell
    and JS code. Two parsers sit fine in this file; a third format graduates them
    into their own module.

    Keeps user/assistant TEXT parts; skips `synthetic` and `ignored` parts (plumbing:
    OpenCode's auto-continue turns and AMG's own injected notes must not become
    memory); skips `reasoning` parts (private thinking, never stored — the same rule
    as Claude Code's `thinking` blocks); counts `tool` and `file` parts as numbered
    attachment markers; every other part kind (step-start/step-finish/snapshot/patch)
    is turn plumbing and is dropped silently."""
    out: List[str] = []
    turns = 0
    attach_seq = 0
    pending: List[str] = []
    started_ms: Optional[float] = None
    ended_ms: Optional[float] = None

    def flush_attach() -> None:
        nonlocal attach_seq
        for label in pending:
            attach_seq += 1
            out.append(session_attachment_marker(attach_seq, label))
        pending.clear()

    for entry in messages:
        if not isinstance(entry, dict):
            continue
        info = entry.get("info")
        parts = entry.get("parts")
        if not isinstance(info, dict) or not isinstance(parts, list):
            continue
        role = info.get("role")
        if role not in ("user", "assistant"):
            continue
        texts: List[str] = []
        att: List[str] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if pt == "text":
                if p.get("synthetic") or p.get("ignored"):
                    continue
                t = str(p.get("text") or "").strip()
                if t:
                    texts.append(t)
            elif pt == "reasoning":
                continue                       # cut raw model reasoning
            elif pt == "tool":
                att.append(f"tool call ({p.get('tool') or 'tool'})")
            elif pt == "file":
                att.append("file")
        ts = (info.get("time") or {}).get("created") if isinstance(info.get("time"), dict) else None
        if isinstance(ts, (int, float)):
            started_ms = ts if started_ms is None else started_ms
            ended_ms = ts
        joined = "\n\n".join(texts).strip()
        if joined:
            flush_attach()
            out.append(session_role_marker("Assistant" if role == "assistant" else "Human"))
            out.append(joined)
            turns += 1
        pending.extend(att)
    flush_attach()
    if turns == 0:
        return None

    def _iso(ms: Optional[float]) -> Optional[str]:
        return (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms / 1000.0))
                if isinstance(ms, (int, float)) else None)

    return {"markdown": "\n\n".join(out).rstrip() + "\n", "turns": turns,
            "attachments": attach_seq, "started": _iso(started_ms), "ended": _iso(ended_ms)}


def _dump_session_opencode(project_root: Path, amg: Path, cfg: Dict[str, Any],
                           payload: Dict[str, Any]) -> Dict[str, Any]:
    """Write the OpenCode transcript under a SESSION-STABLE filename and overwrite it
    on every call — the incremental dump. The plugin re-dumps on idle (throttled), so
    a crash at any moment costs at most the tail since the last idle; the dated
    collision suffix of `_dump_session` is deliberately absent here, because landing
    on the SAME file is the point (reconciliation then re-syncs the changed turns
    instead of ingesting a duplicate session). The name derives from the session's
    creation time plus an id tail, so it survives plugin restarts."""
    rendered = _render_transcript_opencode(payload.get("messages") or [])
    if rendered is None:
        return {"skipped": "empty or unreadable transcript"}
    sessions = session_dir(project_root, cfg, amg)
    if sessions is None:
        return {"skipped": "no sessions dir"}
    sid = str(payload.get("session_id") or "session")
    tail = re.sub(r"[^A-Za-z0-9]+", "", sid)[-6:] or "oc"
    created = payload.get("created_ms")
    stamp = (time.strftime("%Y-%m-%d-%H%M", time.localtime(float(created) / 1000.0))
             if isinstance(created, (int, float)) else time.strftime("%Y-%m-%d-%H%M"))
    target = sessions / f"{stamp}-{tail}.md"
    fm: Dict[str, object] = {
        "session": target.stem, "source": f"opencode:{sid}",
        "reason": str(payload.get("reason") or "idle"), "turns": rendered["turns"],
        "attachments": rendered["attachments"]}
    if rendered["started"]:
        fm["started"] = rendered["started"]
    if rendered["ended"]:
        fm["ended"] = rendered["ended"]
    body = ("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
            + "\n---\n\n" + rendered["markdown"])
    sessions.mkdir(parents=True, exist_ok=True)
    gs.atomic_write_text(target, body)
    try:
        rel = target.relative_to(amg).as_posix()
    except ValueError:
        rel = str(target)
    return {"file": rel, "turns": rendered["turns"], "attachments": rendered["attachments"]}


def _dump_session(project_root: Path, amg: Path, cfg: Dict[str, Any],
                  transcript_path: Optional[str], reason: Optional[str]) -> Dict[str, Any]:
    """Render the session transcript and write it to <store>/sessions as a dated dump
    (atomic file write — it is a source, not graph data; the next bootstrap ingests it).
    A missing transcript path (no hook payload) or an empty transcript is a no-op."""
    if not transcript_path:
        return {"skipped": "no transcript_path (no SessionEnd hook payload)"}
    rendered = _render_transcript(Path(transcript_path))
    if rendered is None:
        return {"skipped": "empty or unreadable transcript"}
    sessions = session_dir(project_root, cfg, amg)
    if sessions is None:
        return {"skipped": "no sessions dir"}
    stamp = time.strftime("%Y-%m-%d-%H%M")
    target = sessions / f"{stamp}.md"
    i = 1
    while target.exists():                         # same-minute collision: disambiguate
        target = sessions / f"{stamp}-{i}.md"
        i += 1
    fm: Dict[str, object] = {
        "session": target.stem, "source": Path(transcript_path).name,
        "reason": reason or "unknown", "turns": rendered["turns"],
        "attachments": rendered["attachments"]}
    if rendered["started"]:
        fm["started"] = rendered["started"]
    if rendered["ended"]:
        fm["ended"] = rendered["ended"]
    body = ("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
            + "\n---\n\n" + rendered["markdown"])
    gs.atomic_write_text(target, body)
    try:
        rel = target.relative_to(amg).as_posix()
    except ValueError:
        rel = str(target)
    return {"file": rel, "turns": rendered["turns"], "attachments": rendered["attachments"],
            "edited": rendered.get("edited", [])}


def _record_usage(project_root: Path, amg: Path, edited_raw: List[str],
                  reason: Optional[str], leave_stamp: bool = False) -> Dict[str, Any]:
    """USAGE provenance. Cross the files edited this session (from the
    transcript's edit/write tool calls) with the nodes the retrieval packs pointed at
    (work/pack-log.jsonl, written by retrieve) and append the USED nodes + a coarse outcome
    to work/usage.log. `used` is the non-circular signal — a node whose source was actually
    EDITED, not merely retrieved — so it is kept SEPARATE from the blind coactivation.log.
    The improved Hebbian rule (consolidate.fold_weights) reinforces co-used edges
    from this log when apply_hebbian is on (consuming it); with the default off it accrues.
    The pack log is session-scoped, so it is consumed (cleared) here.

    Outcome is COARSE: reaching session-end means no crash, and editing pack-pointed files
    means work landed -> `completed` (an ACCEPTED outcome the weight rule rewards). True
    accept / merge / revert detection (a REVERTED outcome would weaken) needs git/test
    integration — a later refinement; the rule already handles a `reverted` record.
    No-op without a pack log; records nothing when no edit hit a pack node. In an agent
    environment without the SessionEnd transcript there are no edited files to attribute
    (the portable fallback is capturing notes as you go)."""
    pack_log = amg / "work" / "pack-log.jsonl"
    if not pack_log.exists():
        return {"skipped": "no pack log this session"}
    try:
        lines = pack_log.read_text(encoding="utf-8").splitlines()
        consumed_mtime = pack_log.stat().st_mtime
    except OSError:
        return {"skipped": "pack log unreadable"}
    try:                                       # session-scoped: consume it once read
        pack_log.unlink()
    except OSError:
        pass
    if leave_stamp:
        # A MID-SESSION consumer (the OpenCode idle dump) eats the log while the
        # session — and its window, which still holds the pack — lives on. The stamp
        # preserves the last consultation's time for the prompt-hint gate; the true
        # end-of-session consumer (the Claude Code hook) leaves no stamp, so a new
        # session there still reads as "not consulted yet".
        try:
            stamp = amg / "work" / "pack-log-stamp"
            stamp.touch()
            os.utime(stamp, (consumed_mtime, consumed_mtime))
        except OSError:
            pass
    if not edited_raw:
        return {"used": 0, "note": "no edits to attribute"}

    proj = project_root.resolve()
    edited: set[str] = set()
    for p in edited_raw:
        try:
            edited.add(Path(p).resolve().relative_to(proj).as_posix())
        except (ValueError, OSError):
            edited.add(Path(p).as_posix())     # outside the project / unresolvable: as-is

    used: Dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for it in rec.get("pack", []):
            sp, nid = it.get("source_path"), it.get("id")
            if nid and sp and sp in edited:
                used.setdefault(nid, sp)
    if not used:
        return {"used": 0, "edited": len(edited)}

    record = {"ts": time.time(), "session": time.strftime("%Y-%m-%d-%H%M"),
              "reason": reason or "unknown", "outcome": "completed",
              "used": sorted(used), "edited_files": sorted(edited)}
    try:
        out = amg / "work" / "usage.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return {"skipped": "usage.log unwritable"}
    return {"used": len(used), "edited": len(edited), "outcome": "completed"}


# --------------------------------------------------------------------------- #
# Automatic hooks
# --------------------------------------------------------------------------- #

def session_start(project_root: Path, amg: Path) -> Dict[str, Any]:
    cfg = _read_config(amg)
    if not (_is_active(cfg) and _automation_on(cfg)):
        return {"skipped": "amg inactive or automation off"}
    healed = _heal(amg)
    # Refresh the digest so the entry point's import is current and the file exists
    # (a session-end may have been skipped by a hard kill of the prior session).
    digest = co.write_digest(project_root, amg)
    out = {"action": "session-start", **healed, "digest": digest}
    note = format_heal_note(healed)
    conflicts = rc.find_conflict_markers(gs.GraphStore(amg))
    if conflicts:                            # post-merge: warn until the user resolves them
        out["conflicts"] = conflicts
        cnote = (f"{len(conflicts)} node file(s) carry unresolved git merge markers — the "
                 "graph skips them; resolve the conflicts and re-run the bootstrap.")
        note = f"{note} {cnote}" if note else cnote
    # Judgment-consolidation nudge: the deterministic folds run themselves, the
    # judged pass has no event of its own — this line is its mechanical trigger.
    knote = _consolidation_note(_consolidation_state(amg))
    if knote:
        out["consolidation_note"] = knote
        note = f"{note} {knote}" if note else knote
    if note:
        out["note"] = note
    return out


def session_end(project_root: Path, amg: Path, transcript_path: Optional[str] = None,
                reason: Optional[str] = None,
                payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = _read_config(amg)
    if not (_is_active(cfg) and _automation_on(cfg)):
        return {"skipped": "amg inactive or automation off"}
    # Order matters: dump the transcript, RECORD usage, THEN fold weights — so this
    # session's outcome (work/usage.log) is available to the fold. The improved Hebbian
    # rule reinforces co-used edges from usage.log (consuming it when apply_hebbian is on);
    # with the default apply_hebbian off the fold leaves usage.log untouched (it accrues).
    if payload is not None and payload.get("format") == "opencode":
        # The OpenCode plugin runs this REPEATEDLY (throttled, on session.idle), not
        # once at session end: the transcript overwrites its stable file, the edited
        # files arrive in the payload (there is no transcript-side tool log to mine),
        # and the consumed pack log leaves a stamp so the mid-session hint gate stays
        # honest between dumps.
        session: Dict[str, Any] = _dump_session_opencode(project_root, amg, cfg, payload)
        edited = [str(p) for p in (payload.get("edited_files") or [])]
        usage = _record_usage(project_root, amg, edited, reason, leave_stamp=True)
    else:
        session = _dump_session(project_root, amg, cfg, transcript_path, reason)
        # Attribute usage: the files this session edited (popped off the dump result so
        # the long list does not bloat the return) crossed with the packs retrieve logged.
        edited = session.pop("edited", []) if isinstance(session, dict) else []
        usage = _record_usage(project_root, amg, edited, reason)
    try:
        weights = co.fold_weights(project_root, amg)
    except gs.StoreLockError as exc:         # shared folder: another writer holds the lock
        # Weight folding is idempotent and only accrues coact — skip this cycle rather
        # than crash session-end; the next end/consolidate folds it.
        weights = {"skipped": "another writer holds the lock", "lock_note": str(exc)}
    digest = co.write_digest(project_root, amg)
    return {"action": "session-end", "weights": weights, "digest": digest,
            "session": session, "usage": usage}


def start_check(project_root: Path, amg: Path) -> Dict[str, Any]:
    """The whole deterministic start-of-session routine as ONE entry point: heal +
    digest (session_start), then the free reconcile half (`plan` — the structural
    skeleton re-sync), then the sync question composed under the deferral cadence.

    Built for event surfaces that can inject context but have no block-driven model
    step at session start — the OpenCode plugin (session.created) and the Codex
    SessionStart hook run it and inject the printed note. Field evidence forced the
    move: on weak models the block's "run the start check first" imperative was
    skipped even when stated three times, so the deterministic half now runs itself
    and only the DECISION (sync now or defer) is put in front of the model. Claude
    Code deliberately keeps its split (the hook heals; the model runs `plan` per the
    block) — this verb changes nothing there.

    The note follows the blocks' cadence: with a recorded deferral the question is
    re-raised only once the backlog has grown noticeably (≥ ×1.5 or +50 units);
    until then the deferral stands silently. All clean → no note at all (the same
    no-noise rule as the hooks')."""
    base = session_start(project_root, amg)
    if base.get("skipped"):
        return base
    notes: List[str] = [base["note"]] if base.get("note") else []
    out: Dict[str, Any] = {**base, "action": "start-check"}
    try:
        summary = rc.plan(project_root, amg)
    except gs.StoreLockError as exc:       # shared folder: skip, the next start covers it
        out["plan"] = {"skipped": "another writer holds the lock", "lock_note": str(exc)}
        if notes:
            out["note"] = " ".join(notes)
        return out
    out["plan"] = {k: summary.get(k, 0) for k in
                   ("added", "changed", "deleted", "queued_for_semantic")}
    queued = int(summary.get("queued_for_semantic") or 0)
    if queued:
        deferred = _sync_deferred(amg)
        base_q = int(deferred.get("queued") or 0) if deferred else 0
        if not base_q or queued >= max(base_q * 1.5, base_q + 50):
            diff = ", ".join(f"{summary.get(k, 0)} {k}" for k in ("added", "changed", "deleted")
                             if summary.get(k))
            notes.append(
                f"AMG: sources changed{' (' + diff + ')' if diff else ''} — {queued} unit(s) "
                "await semantic enrichment. Ask the user: sync the semantic layer now "
                "(the amg-bootstrap flow / `/amg sync`), or defer? On a defer, record it "
                "with `lifecycle.py sync-defer` so later sessions stop re-asking.")
    if notes:
        out["note"] = " ".join(notes)
    return out


def _hook_json_wire(event: str, note: Optional[str]) -> Optional[str]:
    """Wrap a note in the advanced hook-output JSON
    (`hookSpecificOutput.additionalContext`) — the format Claude Code defines for
    its hooks and Codex adopted verbatim. The difference is tolerance, not shape:
    Claude Code also injects bare stdout, while Codex parses stdout strictly as
    JSON and ignores plain text — so the JSON wire is what a Codex hook must emit.
    No note -> None (print nothing; empty stdout is a clean no-op everywhere)."""
    if not note:
        return None
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": note}}, ensure_ascii=False)


# The mid-session reminder is GATED, never a per-prompt tax. Field evidence: the
# loop's retrieval discipline decays as a session runs — the "time to consult the
# graph" moment must arrive from outside, like the digest does at session start —
# while an unconditional per-prompt line is noise that trains the model to ignore
# it. So the hint fires only when the memory has DEMONSTRABLY gone unconsulted and
# the prompt looks like a task; on every other prompt it prints nothing and injects
# zero tokens. The thresholds are deliberate: a pack older than ~15 min no longer
# reflects the current focus; prompts under ~200 chars are mostly answers and
# follow-ups, not new tasks; one reminder per ~10 min keeps the channel quiet
# enough to stay a signal.
_HINT_PACK_STALE_S = 15 * 60
_HINT_MIN_PROMPT_CHARS = 200
_HINT_COOLDOWN_S = 10 * 60


def prompt_hint(amg: Path, prompt: str) -> Optional[str]:
    """One short line for the UserPromptSubmit hook, or None (silence — the normal
    outcome). Fires only when ALL gates pass: AMG active + automation on; the prompt
    is task-shaped (length); the cooldown stamp (work/hint-stamp) has expired; and
    this session's pack log (work/pack-log.jsonl — consumed by session-end, so its
    presence and mtime mean "consulted THIS session") is absent or stale. The stamp
    is touched when a hint is issued."""
    cfg = _read_config(amg)
    if not (_is_active(cfg) and _automation_on(cfg)):
        return None
    if len(prompt.strip()) < _HINT_MIN_PROMPT_CHARS:
        return None
    now = time.time()
    stamp = amg / "work" / "hint-stamp"
    try:
        if stamp.exists() and now - stamp.stat().st_mtime < _HINT_COOLDOWN_S:
            return None
        # "Consulted this session" is the freshest of the live pack log and the
        # consumption stamp a mid-session consumer leaves behind (the OpenCode idle
        # dump eats the log while the window still holds the pack; without the stamp
        # the hint would re-fire right after every dump).
        marks = [p.stat().st_mtime
                 for p in (amg / "work" / "pack-log.jsonl", amg / "work" / "pack-log-stamp")
                 if p.exists()]
        age = (now - max(marks)) if marks else None
    except OSError:
        return None                        # unreadable state: stay silent, never block
    if age is not None and age < _HINT_PACK_STALE_S:
        return None                        # the memory was consulted recently
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass                               # the hint is still worth printing once
    if age is None:
        return ("AMG: memory has not been consulted this session — "
                "a new topic starts with a retrieval.")
    return (f"AMG: the last memory pack is {int(age // 60)} min old — "
            "if this prompt opens a new topic, start it with a retrieval.")


# --------------------------------------------------------------------------- #
# Manual commands
# --------------------------------------------------------------------------- #

def _engine_version() -> str:
    """The installed engine's version. The installer ships the repo's VERSION inside
    the amg-bootstrap skill (next to scripts/); a dev checkout falls back to the
    repository-root VERSION. Unknown when neither exists (a hand-copied engine)."""
    here = Path(__file__).resolve()
    for cand in (here.parents[1] / "VERSION",      # <skill>/VERSION (installed)
                 here.parents[3] / "VERSION"):     # repo root (dev checkout)
        try:
            if cand.exists():
                return cand.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return "unknown"


def sync_defer(amg: Path) -> Dict[str, Any]:
    """Record that the user deferred the offered semantic sync, together with the
    backlog size it was deferred at (work/sync-defer.json). The start-of-session
    routine re-asks only when the backlog has grown noticeably past this mark, so a
    standing deferral stops costing the user the same question every session. The
    stamp is consumed implicitly: a sync drains the queue, the next non-zero backlog
    starts a fresh comparison."""
    store = gs.GraphStore(amg)
    store.init()
    queued = _semantic_backlog(amg, rc.load_nodes(store)) or 0
    stamp = amg / "work" / "sync-defer.json"
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        gs.atomic_write_text(stamp, json.dumps(
            {"queued": queued, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    except OSError as exc:
        return {"error": f"cannot write {stamp}: {exc}"}
    return {"action": "sync-defer", "queued": queued, "stamp": str(stamp)}


def _sync_deferred(amg: Path) -> Optional[Dict[str, Any]]:
    """The recorded sync deferral ({queued, ts}) or None."""
    stamp = amg / "work" / "sync-defer.json"
    if not stamp.exists():
        return None
    try:
        d = json.loads(stamp.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


HELP_TEXT = """\
AMG operations (any verb also works as plain words that name the memory / AMG):
  status       one-screen state report (engine version, graph size, semantic queue,
               last sync / pack / consolidation, connectivity) — deterministic script
  version      the installed engine version
  on / off     enable / disable AMG (flips `active` in config.yml; takes effect at once)
  repair       recover + verify --repair + the store invariant audit (heal after a
               crash; the audit half reports what needs a look) — deterministic script
  sync         build or reconcile the graph with the sources (model-driven: the
               amg-bootstrap flow / skill)
  retrieve <q> assemble a context pack for a query (direct retrieve.py call; --compact
               for a targeted pointer lookup, --intent history|conflict by meaning)
  consult <q>  session-aware memory consult (Claude Code only): a forked copy of the
               session retrieves in its own window and returns a verdict — adds /
               confirms / contradicts — without importing the pack
  consolidate  fold weights, select conclusions, compact over-budget branches
               (model-driven: the amg-consolidate flow / skill)
  relink       re-check the isolated (unlinked) nodes: nominate for the strays only
               (link_candidates.py --isolated; their past rejections re-opened),
               judge each batch, apply-derived — repeat until zero batches
               (nomination alone applies nothing)
  view         export + open the read-only 3D graph viewer (export_graph.py --open)
  sync-defer   record "sync deferred" so later sessions stop re-asking at the same size
  help         this list
Control verbs (status/version/on/off/repair/sync-defer/help) run via lifecycle.py;
work verbs (sync/retrieve/consolidate/relink/view) belong to the skills / activation loop."""


def repair(project_root: Path, amg: Path) -> Dict[str, Any]:
    healed = _heal(amg)
    out = {"action": "repair", **healed}
    note = format_heal_note(healed)
    if healed.get("skipped"):                # shared folder: a live writer holds the lock
        note = ("Another writer currently holds the AMG lock — skipped repair. Retry "
                "shortly; an abandoned lock frees itself once stale, and nothing is lost "
                "(healing is idempotent).")
    conflicts = rc.find_conflict_markers(gs.GraphStore(amg))
    if conflicts:                            # git merge left markers in node files
        out["conflicts"] = conflicts
        cnote = (f"{len(conflicts)} node file(s) carry unresolved git merge markers "
                 f"(e.g. {conflicts[0]}); the graph skips them. Resolve the conflicts, "
                 "then run reconcile.py bootstrap . to rebuild.")
        note = f"{note} {cnote}" if note else cnote
    # The invariant audit rides on repair: the verb's verbal trigger is "check the
    # memory graph", and healing covers only what the journal and lock can say —
    # the audit reports the logical faults verify cannot see (duplicate ids, path/id
    # mismatches, a lying queue). It runs AFTER the heal, so just-replayed journal
    # entries are not double-reported; lock-free and read-only, it degrades to a
    # report under a foreign lock. Session-start deliberately stays audit-free —
    # a per-session O(nodes) sweep is a tax, while repair is manual and rare.
    audit_rep = rc.audit(project_root, amg)
    out["audit"] = audit_rep
    if audit_rep.get("verdict") != "clean":
        buckets = ", ".join(f"{k}={v['count']}" for k, v in audit_rep.items()
                            if isinstance(v, dict) and v.get("count"))
        anote = (f"Store audit: {audit_rep.get('anomalies')} anomaly(ies) — {buckets}. "
                 "Full report with samples: reconcile.py audit .")
        note = f"{note} {anote}" if note else anote
    if note:
        out["note"] = note
    return out


def set_active(amg: Path, value: bool) -> Dict[str, Any]:
    """Flip `active` in config.yml in place, preserving every other line and comment.
    The control-plane config is not graph data, so this is a plain atomic file write,
    not a graph transaction."""
    f = amg / "config.yml"
    if not f.exists():
        return {"error": f"no config at {f} — AMG is not installed for this project"}
    text = f.read_text(encoding="utf-8")
    repl = "true" if value else "false"
    new, n = re.subn(r"(?m)^(\s*active\s*:\s*)(?:true|false|on|off|yes|no)\b",
                     lambda m: m.group(1) + repl, text, count=1)
    if n == 0:                               # no active: line -> prepend a valid one
        new = f"active: {repl}\n" + text
    gs.atomic_write_text(f, new)
    return {"action": "on" if value else "off", "active": value, "config": str(f)}


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def _mtime(p: Path) -> Optional[str]:
    if p.exists():
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime))
    return None


def _semantic_backlog(amg: Path, nodes: Dict[str, Any]) -> Optional[int]:
    """The honest semantic-queue figure: queue.json units whose node still awaits
    enrichment. Applying derivations now refreshes the queue file itself
    (reconcile._refresh_queue), but a report may still run over a store written by
    an older engine, between an interrupted apply and the next plan — intersecting
    with live node state keeps the DISPLAYED figure honest either way (the raw file
    length once showed hundreds of "awaiting" units on a fully derived graph)."""
    q = amg / "work" / "queue.json"
    if not q.exists():
        return None
    try:
        units = json.loads(q.read_text(encoding="utf-8")).get("units", [])
    except (OSError, json.JSONDecodeError):
        return None
    return sum(1 for u in units
               if (nodes.get(str(u.get("id"))) or {}).get("status") == "stale")


def _last_sync(amg: Path) -> Optional[str]:
    """The last reconciliation line from the action log (bootstrap or apply) — the
    honest 'when did the graph last move toward the sources' answer for status."""
    log = amg / "actions.log"
    if not log.exists():
        log = amg / "log.md"                 # legacy name, adopted on the next write
    if not log.exists():
        return None
    last: Optional[str] = None
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if " reconcile |" in line:
                last = line.strip()
    except OSError:
        return None
    return last


# How many deterministic weight folds (one per session end) may pass without a
# judgment consolidation before the loop starts warning. The deterministic half runs
# itself; the judgment half (promote / merge / compact / arbitrate) needs the model,
# has no event of its own, and silently lapses without a nudge — so the nudge is
# mechanical: one line at session start (and in status), gone once the pass runs.
_JUDGED_LAG_WARN = 3


def _consolidation_state(amg: Path) -> Dict[str, Any]:
    """Read the consolidation history out of the action log — cheap line arithmetic,
    no node loading. Distinguishes the two halves by their log messages: the
    deterministic weight fold writes `weights folded`, the judgment pass writes
    `consolidation applied`. Returns last lines of each kind, the count of folds
    since the last judged pass, and whether an unapplied plan/actions file sits in
    work/ (written after the last judged pass — an interrupted judge run)."""
    out: Dict[str, Any] = {"last": None, "last_judged": None, "folds_since_judged": 0,
                           "leftover": []}
    log = amg / "actions.log"
    if not log.exists():
        log = amg / "log.md"                 # legacy name, adopted on the next write
    judged_ts: Optional[str] = None
    if log.exists():
        try:
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                if " consolidate |" not in line:
                    continue
                out["last"] = line.strip()
                if "consolidation applied" in line:
                    out["last_judged"] = line.strip()
                    out["folds_since_judged"] = 0
                    judged_ts = line.strip()[1:20]        # "[YYYY-MM-DDThh:mm:ss]"
                elif "weights folded" in line:
                    out["folds_since_judged"] += 1
        except OSError:
            pass
    for name in ("consolidation-plan.json", "actions.json"):
        f = amg / "work" / name
        if f.exists() and (judged_ts is None or (_mtime(f) or "") > judged_ts):
            out["leftover"].append(name)     # written after the last judged pass
    return out


def _consolidation_note(state: Dict[str, Any]) -> Optional[str]:
    """The one-line judgment-consolidation nudge, or None while nothing is overdue.
    One wording for every environment: the SessionStart hook prints it in Claude
    Code, and `status` carries it where there are no hooks (Codex / generic — the
    activation block's start-of-session routine reads status)."""
    folds, leftover = state["folds_since_judged"], state["leftover"]
    if folds < _JUDGED_LAG_WARN and not leftover:
        return None
    parts = []
    if folds >= _JUDGED_LAG_WARN:
        parts.append(f"no judgment consolidation for {folds} session(s) "
                     "(only the deterministic folds ran)")
    if leftover:
        parts.append(f"an unapplied {' / '.join(leftover)} sits in work/")
    return ("Memory upkeep is overdue: " + "; ".join(parts)
            + ". Offer the user a consolidation at wrap-up, or run the "
            "amg-consolidate flow (/amg consolidate where commands exist).")


def _eval_summary(amg: Path) -> Optional[Dict[str, Any]]:
    report = amg / "work" / "eval-gate-report.json"
    if not report.exists():
        return None
    try:
        r = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {"status": r.get("status"), "recall_delta": r.get("recall_delta"),
            "hop_recall_delta": r.get("hop_recall_delta"), "cases": r.get("cases")}


def status(project_root: Path, amg: Path) -> Dict[str, Any]:
    cfg = _read_config(amg)
    store = gs.GraphStore(amg)
    store.init()
    nodes = rc.load_nodes(store)
    by_status: Dict[str, int] = defaultdict(int)
    for n in nodes.values():
        by_status[str(n.get("status") or "active")] += 1
    problems = store.verify(repair=False)    # read-only: report, do not mutate
    # Connectivity gate: fragmentation must be visible here,
    # not only in the 3D viewer. Computed over the already-loaded nodes (cheap).
    gate_cfg = cfg.get("connectivity_gate") if isinstance(cfg.get("connectivity_gate"), dict) else {}
    metrics = rc.graph_metrics(nodes, gate_cfg,
                               rc.session_source_prefix(project_root, cfg, amg))
    consolidation = _consolidation_state(amg)
    return {
        "engine_version": _engine_version(),
        "active": _is_active(cfg),
        "automation": _automation_on(cfg),
        "graph_root": str(amg),
        "branch": rc._git_branch(project_root),   # git awareness; None without git
        "commit": rc._git_commit(project_root),
        "nodes": len(nodes),
        "stale": by_status.get("stale", 0),
        "by_status": dict(by_status),
        "pending_transactions": problems.get("pending_transactions", []),
        "stale_lock": problems.get("stale_lock", []),
        "conflicts": rc.find_conflict_markers(store),   # git merge markers in nodes
        "queue_size": _semantic_backlog(amg, nodes),
        "sync_deferred": _sync_deferred(amg),
        "last_sync": _last_sync(amg),
        "last_pack": _mtime(amg / "cache" / "pack.md"),
        "last_consolidation": consolidation["last"],
        # The judgment half tracked apart from the deterministic fold: hooks (or the
        # loop) fold weights every session, while the judged pass needs the model —
        # `status` is where a hook-less environment sees the same overdue signal.
        "last_judged_consolidation": consolidation["last_judged"],
        "weight_folds_since_judged": consolidation["folds_since_judged"],
        "consolidation_leftover": consolidation["leftover"],
        "consolidation_note": _consolidation_note(consolidation),
        "eval_summary": _eval_summary(amg),
        "connectivity": {k: metrics[k] for k in
                         ("components", "largest_component_share", "isolated_nodes",
                          "dangling_structural", "dangling_semantic",
                          "doc_files_without_documents", "gate")},
    }


def format_status(d: Dict[str, Any]) -> str:
    """One-screen human-readable status (DoD: the user sees state without reading
    files). Environments are told to present this report VERBATIM — the layout is
    the contract, so the same facts read the same everywhere."""
    q = d.get("queue_size")
    deferred = d.get("sync_deferred") or {}
    lines = [
        f"AMG status (engine v{d.get('engine_version', 'unknown')})",
        f"  active:               {d['active']}",
        f"  automation:           {d['automation']}",
        f"  graph root:           {d['graph_root']}",
        f"  git branch / commit:  {d.get('branch') or '-'} / {d.get('commit') or '-'}",
        f"  nodes:                {d['nodes']}  (stale: {d['stale']})",
        f"  pending transactions: {len(d['pending_transactions'])}",
        f"  stale lock:           {'yes' if d['stale_lock'] else 'no'}",
        f"  conflicts:            {len(d.get('conflicts') or [])}",
        "  semantic queue:       "
        + (f"{q} unit(s) awaiting enrichment" if q else "0 — graph matches the sources"),
    ]
    if deferred.get("queued"):
        lines.append(f"  sync deferred at:     {deferred['queued']} unit(s) ({deferred.get('ts', '-')})")
    lines += [
        f"  last sync:            {d.get('last_sync') or '-'}",
        f"  last pack:            {d['last_pack'] or '-'}",
        f"  last consolidation:   {d['last_consolidation'] or '-'}",
        f"  last judged pass:     {d.get('last_judged_consolidation') or '-'}"
        f"  ({d.get('weight_folds_since_judged', 0)} weight fold(s) since)",
    ]
    if d.get("consolidation_leftover"):
        lines.append("  unapplied in work/:   " + ", ".join(d["consolidation_leftover"]))
    if d.get("consolidation_note"):
        lines.append(f"  note:                 {d['consolidation_note']}")
    cm = d.get("connectivity")
    if cm:
        lines.append(f"  connectivity:         {cm.get('gate')} "
                     f"(components={cm.get('components')}, "
                     f"largest={cm.get('largest_component_share')}, "
                     f"dangling structural={cm.get('dangling_structural')} / "
                     f"model-written={cm.get('dangling_semantic')}, "
                     f"doc files w/o documents={cm.get('doc_files_without_documents')})")
    es = d.get("eval_summary")
    if es:
        lines.append(f"  eval gate:            {es.get('status')} "
                     f"(Δrecall={es.get('recall_delta')}, Δhop={es.get('hop_recall_delta')})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _load_stdin_payload() -> Dict[str, Any]:
    """The SessionEnd hook pipes JSON on stdin (transcript_path, reason, cwd, ...).
    Return {} when stdin is a TTY or empty (a manual run), so session-end still folds
    weights + the digest without a dump and never blocks waiting on a terminal."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return d if isinstance(d, dict) else {}


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    cli_transcript: Optional[str] = None
    if "--transcript" in args:                   # manual dump without the hook
        i = args.index("--transcript")
        cli_transcript = args[i + 1]
        del args[i:i + 2]
    hook_json = "--hook-json" in args    # emit the advanced hook JSON (Codex requires it)
    if hook_json:
        args.remove("--hook-json")
    cmd = args[0] if args else "help"
    # Hook-driven commands read the event payload once; its `cwd` names the project
    # more reliably than the process cwd (Codex documents cwd in the payload, and a
    # hook is not guaranteed to spawn at the project root). An explicit positional
    # root still wins.
    payload: Dict[str, Any] = (_load_stdin_payload()
                               if cmd in ("session-start", "session-end",
                                          "start-check", "prompt-hint") else {})
    if len(args) > 1:
        root = Path(args[1]).resolve()
    elif payload.get("cwd"):
        root = Path(str(payload["cwd"])).resolve()
    else:
        root = Path.cwd()
    amg = gs.resolve_amg_root(cli_root, root)

    if cmd == "session-start":
        res = session_start(root, amg)
        if res.get("note"):
            print(res["note"])     # inject context ONLY when an unclean shutdown was healed
        return 0                   # clean start (or AMG off): stay silent, no per-session noise
    if cmd == "start-check":
        note = start_check(root, amg).get("note")
        wired = _hook_json_wire("SessionStart", note) if hook_json else note
        if wired:
            print(wired)           # the event surface injects this into the session
        return 0                   # all clean: silence, whatever the wire
    if cmd == "session-end":
        tp = cli_transcript or payload.get("transcript_path")
        print(json.dumps(session_end(root, amg, transcript_path=tp,
                                     reason=payload.get("reason"),
                                     payload=payload), indent=2)); return 0
    if cmd == "prompt-hint":
        note = prompt_hint(amg, str(payload.get("prompt") or ""))
        wired = _hook_json_wire("UserPromptSubmit", note) if hook_json else note
        if wired:
            print(wired)   # UserPromptSubmit: stdout is injected as context
        return 0           # silence on every gated-out prompt (zero tokens)
    if cmd == "repair":
        res = repair(root, amg)
        print(res.get("note") or "AMG store is consistent; nothing to repair.")
        return 0
    if cmd in ("on", "off"):
        print(json.dumps(set_active(amg, cmd == "on"), indent=2)); return 0
    if cmd == "status":
        print(format_status(status(root, amg))); return 0
    if cmd == "version":
        print(f"AMG engine v{_engine_version()}"); return 0
    if cmd == "sync-defer":
        print(json.dumps(sync_defer(amg), indent=2)); return 0
    if cmd == "help":
        print(HELP_TEXT); return 0
    print(__doc__); return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
