#!/usr/bin/env python3
"""
partition_queue.py — split the semantic work queue into per-subtree batches.

After `reconcile.py bootstrap` writes `work/queue.json` (the units awaiting semantic
derivation), a large queue is best derived as several parallel `amg-builder` subagents,
one per subtree, each in its own isolated context. This is the ready-made helper for
that split — instead of an ad-hoc one-liner (awkward on Windows): it groups the units
by the leading segments of their `source_path` and writes one `work/queue-<part>.json`
per group, each in the same `{generated, units}` shape the builder already reads (plus
a `part` label). Read-only over the graph; it only writes the batch files.

A subtree group larger than one builder should safely handle is additionally split
into numbered parts (`queue-<part>-01.json`, `-02.json`, ...): the cap is by unit
count AND by the estimated input volume (the sum of carried unit texts), because
grouping by subtree alone yields unbounded batches — a dense docs directory as one
giant batch overflows a builder's per-response output ceiling and loses hours of
work on an interruption. Caps come from the `builder` config block (or the flags).

CLI:
  python partition_queue.py [<project_root>] [--root <agent_dir>] [--depth N]
        [--max-units N] [--max-chars N]
  python partition_queue.py --priority [--usage] [<project_root>]   # lazy derivation:
        # split the queue into a PRIORITY batch (derive now) and a DEFERRED remainder.

`--depth` (default 2) is how many leading path segments form a batch key: depth 2
groups `src/billing/x.py` and `src/billing/y.py` together under `src/billing`; depth 1
would lump all of `src` into one batch. The graph root is resolved by
graph_store.resolve_amg_root (the same chain as reconcile/consolidate).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import graph_store as gs

try:
    import yaml
except ImportError:                       # pragma: no cover
    sys.stderr.write("partition_queue.py needs PyYAML: pip install pyyaml\n")
    raise

# One builder batch is bounded two ways: by unit count and by
# estimated input volume. Symmetric to `linker.batch_nodes` for the linking pass.
BUILDER_DEFAULTS: Dict[str, Any] = {"batch_units": 40, "batch_max_chars": 120000}

# Input estimate for a unit that carries no text (an oversized unit kept as a
# pointer): the builder will read about this much of the source itself.
_NO_TEXT_NOMINAL_CHARS = 2000

# Lazy derivation. The PRIORITY subset derived eagerly under
# `derivation: lazy` is the structural MAP — file/container-level units (module/class/
# package, and the whole-file fallback). Leaf detail (function/method/section/record/
# block/page/sheet) is deferred until a query activates it (phase B). These kinds mirror
# retrieve's strategic/tactical tiers; the synthesis hubs (a separate step) are likewise
# never deferred. Background fill (phase C) additionally promotes nodes seen in usage.log.
PRIORITY_KINDS = {"module", "class", "package", "file"}


def subtree_key(source_path: str, depth: int) -> str:
    """The batch key for a unit: the first `depth` directory segments of its
    source_path (the filename dropped). A file at the source root has no directory,
    so it groups under `_root`."""
    parts = [p for p in (source_path or "").replace("\\", "/").split("/") if p]
    dirs = parts[:-1]                       # drop the filename
    return "/".join(dirs[:depth]) or "_root"


def _builder_caps(amg_root: Path) -> Dict[str, int]:
    """The `builder` config block over the defaults, read tolerantly — a partition
    helper must not exit on a missing/odd config the way extraction deliberately
    does (test fixtures and diagnostics run without a full install)."""
    caps = dict(BUILDER_DEFAULTS)
    f = amg_root / "config.yml"
    try:
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        blk = raw.get("builder") if isinstance(raw, dict) else None
        if isinstance(blk, dict):
            for k in BUILDER_DEFAULTS:
                if k in blk:
                    caps[k] = int(blk[k])
    except (OSError, ValueError, yaml.YAMLError):
        pass
    return caps


def _unit_cost(u: Dict[str, Any]) -> int:
    return len(u.get("text") or "") or _NO_TEXT_NOMINAL_CHARS


def _chunk(units: List[Dict[str, Any]], max_units: int,
           max_chars: int) -> List[List[Dict[str, Any]]]:
    """Split one subtree group into bounded slices: a slice closes when adding the
    next unit would exceed either cap. A single oversized unit still ships alone —
    bounding is best-effort, never a drop."""
    out: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_chars = 0
    for u in units:
        cost = _unit_cost(u)
        if cur and (len(cur) >= max_units or cur_chars + cost > max_chars):
            out.append(cur)
            cur, cur_chars = [], 0
        cur.append(u)
        cur_chars += cost
    if cur:
        out.append(cur)
    return out


def partition(amg_root: Path, depth: int = 2, max_units: Optional[int] = None,
              max_chars: Optional[int] = None) -> Dict[str, int]:
    """Group work/queue.json units by subtree, split each group under the builder
    caps, and write one work/queue-<part>[-NN].json per slice (the -NN suffix appears
    only when a group needed splitting, so small queues keep the old names). Returns
    {part: unit_count}. Empty (no queue / no units) -> {}."""
    qpath = amg_root / "work" / "queue.json"
    if not qpath.exists():
        return {}
    caps = _builder_caps(amg_root)
    cap_units = max(1, int(max_units if max_units is not None else caps["batch_units"]))
    cap_chars = max(1, int(max_chars if max_chars is not None else caps["batch_max_chars"]))
    data = json.loads(qpath.read_text(encoding="utf-8"))
    units: List[Dict[str, Any]] = data.get("units", []) if isinstance(data, dict) else []
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for u in units:
        groups[subtree_key(str(u.get("source_path", "")), depth)].append(u)

    counts: Dict[str, int] = {}
    work = amg_root / "work"
    for old in work.glob("queue-*.json"):
        # a re-run replaces the batch set (stale parts would re-derive for nothing);
        # the priority/deferred pair belongs to priority_split, not to this splitter
        if old.name not in ("queue-priority.json", "queue-deferred.json"):
            try:
                old.unlink()
            except OSError:
                pass     # transiently held (AV/indexer on Windows): must not kill the split
    for key, group_units in groups.items():
        safe = key.replace("/", "_").replace("\\", "_") or "_root"
        slices = _chunk(group_units, cap_units, cap_chars)
        for i, batch in enumerate(slices, 1):
            label = key if len(slices) == 1 else f"{key}-{i:02d}"
            fname = safe if len(slices) == 1 else f"{safe}-{i:02d}"
            gs.atomic_write_text(
                work / f"queue-{fname}.json",
                json.dumps({"generated": data.get("generated"), "part": label,
                            "units": batch}, ensure_ascii=False, indent=2))
            counts[label] = len(batch)
    return counts


def _used_ids(amg_root: Path) -> Set[str]:
    """Node ids that were actually USED in a session (work/usage.log), for the lazy
    background pass (phase C): a deferred unit whose node was used is derived before
    never-touched ones. Empty when there is no usage log."""
    path = amg_root / "work" / "usage.log"
    used: Set[str] = set()
    if not path.exists():
        return used
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for u in rec.get("used", []):
            if isinstance(u, str):
                used.add(u)
    return used


def priority_split(amg_root: Path, use_usage: bool = False) -> Dict[str, int]:
    """Split work/queue.json into a PRIORITY batch (derive now, under derivation:lazy) and
    a DEFERRED remainder (left stale until a query activates it). Priority = the structural
    map (PRIORITY_KINDS) plus, when use_usage is set, any unit whose node was USED
    (work/usage.log — the phase-C background signal). Writes work/queue-priority.json +
    work/queue-deferred.json (same {generated, part, units} shape the builder reads), and
    leaves queue.json untouched — this only proposes what to derive now. Returns the two
    counts; an empty/absent queue yields zeros."""
    qpath = amg_root / "work" / "queue.json"
    if not qpath.exists():
        return {"priority": 0, "deferred": 0}
    data = json.loads(qpath.read_text(encoding="utf-8"))
    units: List[Dict[str, Any]] = data.get("units", []) if isinstance(data, dict) else []
    used: Set[str] = _used_ids(amg_root) if use_usage else set()
    priority: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    for u in units:
        if str(u.get("kind", "")) in PRIORITY_KINDS or str(u.get("id", "")) in used:
            priority.append(u)
        else:
            deferred.append(u)
    work = amg_root / "work"
    gen = data.get("generated") if isinstance(data, dict) else None
    for name, batch in (("priority", priority), ("deferred", deferred)):
        gs.atomic_write_text(work / f"queue-{name}.json",
                             json.dumps({"generated": gen, "part": name, "units": batch},
                                        ensure_ascii=False, indent=2))
    return {"priority": len(priority), "deferred": len(deferred)}


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    use_usage = "--usage" in args
    if use_usage:
        args.remove("--usage")
    if "--priority" in args:                 # lazy derivation: map first, detail later
        args.remove("--priority")
        project_root = Path(args[0]).resolve() if args else Path.cwd()
        amg_root = gs.resolve_amg_root(cli_root, project_root)
        print(json.dumps(priority_split(amg_root, use_usage), indent=2))
        return 0
    depth = 2
    if "--depth" in args:
        i = args.index("--depth")
        depth = int(args[i + 1])
        del args[i:i + 2]
    max_units: Optional[int] = None
    max_chars: Optional[int] = None
    if "--max-units" in args:
        i = args.index("--max-units")
        max_units = int(args[i + 1])
        del args[i:i + 2]
    if "--max-chars" in args:
        i = args.index("--max-chars")
        max_chars = int(args[i + 1])
        del args[i:i + 2]
    project_root = Path(args[0]).resolve() if args else Path.cwd()
    amg_root = gs.resolve_amg_root(cli_root, project_root)
    counts = partition(amg_root, depth, max_units, max_chars)
    print(json.dumps({"batches": counts, "total": sum(counts.values())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
