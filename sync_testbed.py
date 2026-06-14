#!/usr/bin/env python3
"""
sync_testbed.py — copy the AMG engine from this source repo into the integration
sandbox, the manual predecessor of the Stage 10 installer (roadmap 4.9).

Dev sessions run headless selftests and CLI cycles on temp fixtures inside the repo,
but the parts that only exist as a live Claude Code project — skills, subagents,
hooks, commands — are exercised in a SEPARATE sandbox project so the engine is not
mistaken for this repo's own configuration and graph data never lands in the shipped
tree. This script mirrors the engine into the sandbox's agent dir; the sandbox then
runs its OWN copy, exactly as a real install would.

What it copies (idempotent, overwrites the engine each run):
  <repo>/skills/         -> <testbed>/.claude/skills/
  <repo>/agents/         -> <testbed>/.claude/agents/
  <repo>/entrypoint/CLAUDE.md -> <testbed>/CLAUDE.md   (project-root activation block)

What it never touches (the sandbox's own project data):
  <testbed>/.claude/amg/   nodes, config.yml, work, journal, archive
  <testbed>/src, /docs, /data and any other source folders

Usage:
  python sync_testbed.py [<testbed_dir>]      # default: ../amg-testbed
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
ENGINE_DIRS = ("skills", "agents")


def sync(testbed: Path) -> None:
    claude = testbed / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    for name in ENGINE_DIRS:
        src = REPO / name
        if not src.is_dir():
            raise SystemExit(f"missing engine dir: {src}")
        dest = claude / name
        if dest.exists():
            shutil.rmtree(dest)            # clean overwrite: drop files deleted upstream
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
        print(f"  engine  {name}/ -> {dest}")
    entry = REPO / "entrypoint" / "CLAUDE.md"
    if entry.is_file():
        shutil.copy2(entry, testbed / "CLAUDE.md")
        print(f"  block   entrypoint/CLAUDE.md -> {testbed / 'CLAUDE.md'}")
    # Ensure the graph root exists but never overwrite the sandbox's config/graph.
    (claude / "amg").mkdir(parents=True, exist_ok=True)
    cfg = claude / "amg" / "config.yml"
    print(f"  graph   {claude / 'amg'}  (config {'present' if cfg.exists() else 'absent — create it'})")


def main(argv: list[str]) -> int:
    testbed = Path(argv[1]).resolve() if len(argv) > 1 else (REPO.parent / "amg-testbed")
    print(f"sync engine -> {testbed}")
    sync(testbed)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
