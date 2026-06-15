#!/usr/bin/env python3
"""
lifecycle.py — AMG lifecycle & control plane: the session hooks, the /amg commands,
and the status report. Thin orchestration over the existing scripts (graph_store for
healing, consolidate for weights + the always-on digest); it adds no new graph logic.

Two AUTOMATIC entry points, wired by the installer into the agent dir's settings.json
as Claude Code hooks. They self-gate on config, so they are no-ops unless AMG is both
active and `automation: on` (turning automation off leaves only the manual commands):

  session-start : heal the store (recover + verify --repair) and refresh the digest
                  before task work (so the entry point's import is current and exists).
  session-end   : fold the co-activation log into weights (deterministic; with
                  apply_hebbian off this only ACCUMULATES coact, never mutating
                  conductance) and refresh the digest. The judgment half of
                  consolidation — the amg-consolidator subagent + apply — and the
                  session transcript dump (Stage 9) stay model-driven: a hook cannot
                  run a subagent, so they live in the activation loop, not here.

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
from typing import Dict, List, Optional

import graph_store as gs
import reconcile as rc

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
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


# --------------------------------------------------------------------------- #
# Config gate (active + automation)
# --------------------------------------------------------------------------- #

def _read_config(amg: Path) -> dict:
    f = amg / "config.yml"
    if f.exists():
        try:
            return yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
    return {}


def _is_active(cfg: dict) -> bool:
    return bool(cfg.get("active", False))


def _automation_on(cfg: dict) -> bool:
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

def _heal(amg: Path) -> dict:
    """recover + verify --repair under a single lock. Idempotent and cheap; this is
    what makes a crashed or interrupted prior session a non-event."""
    store = gs.GraphStore(amg)
    store.init()
    with store.lock():
        recovered = store.recover()
        problems = store.verify(repair=True)
    return {"recovered": recovered, "verify": problems}


# --------------------------------------------------------------------------- #
# Automatic hooks
# --------------------------------------------------------------------------- #

def session_start(project_root: Path, amg: Path) -> dict:
    cfg = _read_config(amg)
    if not (_is_active(cfg) and _automation_on(cfg)):
        return {"skipped": "amg inactive or automation off"}
    healed = _heal(amg)
    # Refresh the digest so the entry point's import is current and the file exists
    # (a session-end may have been skipped by a hard kill of the prior session).
    digest = co.write_digest(project_root, amg)
    return {"action": "session-start", **healed, "digest": digest}


def session_end(project_root: Path, amg: Path) -> dict:
    cfg = _read_config(amg)
    if not (_is_active(cfg) and _automation_on(cfg)):
        return {"skipped": "amg inactive or automation off"}
    weights = co.fold_weights(project_root, amg)
    digest = co.write_digest(project_root, amg)
    return {"action": "session-end", "weights": weights, "digest": digest}


# --------------------------------------------------------------------------- #
# Manual commands
# --------------------------------------------------------------------------- #

def repair(project_root: Path, amg: Path) -> dict:
    return {"action": "repair", **_heal(amg)}


def set_active(amg: Path, value: bool) -> dict:
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


def _eval_summary(amg: Path) -> Optional[dict]:
    report = amg / "work" / "eval-gate-report.json"
    if not report.exists():
        return None
    try:
        r = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {"status": r.get("status"), "recall_delta": r.get("recall_delta"),
            "hop_recall_delta": r.get("hop_recall_delta"), "cases": r.get("cases")}


def status(project_root: Path, amg: Path) -> dict:
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


def format_status(d: dict) -> str:
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

def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "help"
    root = Path(args[1]).resolve() if len(args) > 1 else Path.cwd()
    amg = gs.resolve_amg_root(cli_root, root)

    if cmd == "session-start":
        print(json.dumps(session_start(root, amg), indent=2)); return 0
    if cmd == "session-end":
        print(json.dumps(session_end(root, amg), indent=2)); return 0
    if cmd == "repair":
        print(json.dumps(repair(root, amg), indent=2)); return 0
    if cmd in ("on", "off"):
        print(json.dumps(set_active(amg, cmd == "on"), indent=2)); return 0
    if cmd == "status":
        print(format_status(status(root, amg))); return 0
    print(__doc__); return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
