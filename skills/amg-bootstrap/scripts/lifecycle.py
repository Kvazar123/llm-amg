#!/usr/bin/env python3
"""
lifecycle.py — AMG lifecycle & control plane: the session hooks, the /amg commands,
and the status report. Thin orchestration over the existing scripts (graph_store for
healing, consolidate for weights + the always-on digest); it adds no new graph logic.

Two AUTOMATIC entry points, wired by the installer into the agent dir's settings.json
as Claude Code hooks. They self-gate on config, so they are no-ops unless AMG is both
active and `automation: on` (turning automation off leaves only the manual commands):

  session-start : heal the store (recover + verify --repair) and refresh the digest
                  before task work (so the entry point's import is current and exists);
                  report an unclean prior shutdown when one is healed, else stay silent.
  session-end   : fold the co-activation log into weights (deterministic; with
                  apply_hebbian off this only ACCUMULATES coact, never mutating
                  conductance), refresh the digest, and dump the session transcript
                  to <store>/sessions (Stage 9; from the hook's stdin payload). The
                  JUDGMENT half of consolidation — the amg-consolidator subagent +
                  apply — stays model-driven: a hook cannot run a subagent, so it
                  lives in the activation loop, not here.

Four MANUAL commands, exposed as the `/amg <sub>` slash command (and reachable by
verbal intent through the activation block):

  on / off : flip `active` in config.yml (turn AMG on/off for this project).
  repair   : recover + verify --repair (heal on demand; the manual analog of the
             session-start hook — and it runs regardless of the automation gate).
  status   : a one-screen report (active, automation, graph root, node/stale counts,
             pending transactions, stale lock, queue size, last pack, last
             consolidation, eval summary) so the user sees state without reading files.

Every automatic operation thus has a manual analog (DoD): healing <-> /amg repair,
weight folding <-> the amg-consolidate skill, the digest <-> consolidate.py digest.

CLI:
  python lifecycle.py session-start|session-end [<project_root>] [--root <agent_dir>]
  python lifecycle.py status|repair|on|off      [<project_root>] [--root <agent_dir>]
  session-end also accepts the hook's JSON on stdin, or --transcript <path> to dump a
  session manually in an environment without the SessionEnd hook.

The graph root is <agent_dir>/amg, resolved by graph_store.resolve_amg_root (the same
chain as reconcile/consolidate/notes).
"""
from __future__ import annotations

import json
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
    session-start / repair report that they healed one (Stage 9, task 9)."""
    store = gs.GraphStore(amg)
    store.init()
    pre = store.verify(repair=False)
    with store.lock():
        recovered = store.recover()
        problems = store.verify(repair=True)
    return {"recovered": recovered, "verify": problems,
            "stale_lock_cleared": bool(pre.get("stale_lock"))}


def format_heal_note(healed: Dict[str, Any]) -> Optional[str]:
    """A friendly one-liner when a prior unclean shutdown was just healed (Stage 9,
    task 9), else None — so a clean session-start stays silent (no per-session noise).
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
# Session transcript dump (Stage 9)
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
                    att.append(f"tool call ({b.get('name') or 'tool'})")
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
            "attachments": attach_seq, "started": started, "ended": ended}


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
    return {"file": rel, "turns": rendered["turns"], "attachments": rendered["attachments"]}


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
    if note:
        out["note"] = note
    return out


def session_end(project_root: Path, amg: Path, transcript_path: Optional[str] = None,
                reason: Optional[str] = None) -> Dict[str, Any]:
    cfg = _read_config(amg)
    if not (_is_active(cfg) and _automation_on(cfg)):
        return {"skipped": "amg inactive or automation off"}
    weights = co.fold_weights(project_root, amg)
    digest = co.write_digest(project_root, amg)
    session = _dump_session(project_root, amg, cfg, transcript_path, reason)
    return {"action": "session-end", "weights": weights, "digest": digest,
            "session": session}


# --------------------------------------------------------------------------- #
# Manual commands
# --------------------------------------------------------------------------- #

def repair(project_root: Path, amg: Path) -> Dict[str, Any]:
    healed = _heal(amg)
    out = {"action": "repair", **healed}
    note = format_heal_note(healed)
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


def _queue_size(amg: Path) -> Optional[int]:
    q = amg / "work" / "queue.json"
    if not q.exists():
        return None
    try:
        return len(json.loads(q.read_text(encoding="utf-8")).get("units", []))
    except (OSError, json.JSONDecodeError):
        return None


def _last_consolidation(amg: Path) -> Optional[str]:
    """The most recent consolidation line from the best-effort action log."""
    log = amg / "log.md"
    if not log.exists():
        return None
    last = None
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if " consolidate |" in line:
                last = line.strip()
    except OSError:
        return None
    return last


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
    return {
        "active": _is_active(cfg),
        "automation": _automation_on(cfg),
        "graph_root": str(amg),
        "nodes": len(nodes),
        "stale": by_status.get("stale", 0),
        "by_status": dict(by_status),
        "pending_transactions": problems.get("pending_transactions", []),
        "stale_lock": problems.get("stale_lock", []),
        "queue_size": _queue_size(amg),
        "last_pack": _mtime(amg / "cache" / "pack.md"),
        "last_consolidation": _last_consolidation(amg),
        "eval_summary": _eval_summary(amg),
    }


def format_status(d: Dict[str, Any]) -> str:
    """One-screen human-readable status (DoD: the user sees state without reading files)."""
    lines = [
        "AMG status",
        f"  active:               {d['active']}",
        f"  automation:           {d['automation']}",
        f"  graph root:           {d['graph_root']}",
        f"  nodes:                {d['nodes']}  (stale: {d['stale']})",
        f"  pending transactions: {len(d['pending_transactions'])}",
        f"  stale lock:           {'yes' if d['stale_lock'] else 'no'}",
        f"  queue size:           {d['queue_size'] if d['queue_size'] is not None else '-'}",
        f"  last pack:            {d['last_pack'] or '-'}",
        f"  last consolidation:   {d['last_consolidation'] or '-'}",
    ]
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
    cmd = args[0] if args else "help"
    root = Path(args[1]).resolve() if len(args) > 1 else Path.cwd()
    amg = gs.resolve_amg_root(cli_root, root)

    if cmd == "session-start":
        res = session_start(root, amg)
        if res.get("note"):
            print(res["note"])     # inject context ONLY when an unclean shutdown was healed
        return 0                   # clean start (or AMG off): stay silent, no per-session noise
    if cmd == "session-end":
        payload = _load_stdin_payload()
        tp = cli_transcript or payload.get("transcript_path")
        print(json.dumps(session_end(root, amg, transcript_path=tp,
                                     reason=payload.get("reason")), indent=2)); return 0
    if cmd == "repair":
        res = repair(root, amg)
        print(res.get("note") or "AMG store is consistent; nothing to repair.")
        return 0
    if cmd in ("on", "off"):
        print(json.dumps(set_active(amg, cmd == "on"), indent=2)); return 0
    if cmd == "status":
        print(format_status(status(root, amg))); return 0
    print(__doc__); return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
