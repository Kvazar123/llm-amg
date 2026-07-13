#!/usr/bin/env python3
"""
selftest_ignore.py — proves the ignore controls: what reaches the
graph is controllable per intent, universal (no git needed), and never silently drops
an explicitly chosen source.

Checks:
  1. per_intent : exclude (global) + mirror_exclude / absorb_exclude filter only the
                  intended sources; per-intent lists are isolated (mirror_exclude does
                  not touch an absorb source) and additive over the global exclude.
  2. explicit   : an EXPLICIT source whose root is gitignored is still ingested
                  (absorb_path: logs where .gitignore has logs/), while .gitignore keeps
                  filtering junk it matches deeper inside a source (no regression).
  3. respect_off: respect_gitignore: false ignores .gitignore entirely (git-independent,
                  fully config-driven).
  4. agent_dir  : the engine never indexes its own dir, even a custom one (.myagent),
                  derived from the store location — no self-indexing.
  5. by_source  : --stats reports per-source file counts, so a source that yields 0
                  files (all filtered) or a missing path is visible, not silent.

Run:  python selftest_ignore.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import extract_structure as ES
import reconcile as rc


def _mk(proj: Path, rel: str, text: str = "x\n") -> None:
    p = proj / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _paths(units) -> set:
    return {u["source_path"] for u in units}


def test_per_intent():
    proj = Path(tempfile.mkdtemp(prefix="amg-ign1-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        amg.joinpath("config.yml").write_text(
            "active: true\nworking_language: en\n"
            "mirror_path: [src]\nabsorb_path: [data]\n"
            'exclude: ["*.bak"]\n'
            'mirror_exclude: ["*.test.py"]\n'
            'absorb_exclude: ["*.tmp"]\n', encoding="utf-8")
        _mk(proj, "src/app.py")
        _mk(proj, "src/app.test.py")            # mirror_exclude
        _mk(proj, "src/old.bak")                # global exclude
        _mk(proj, "data/d.json", '{"a": 1}\n')
        _mk(proj, "data/d.tmp")                 # absorb_exclude
        _mk(proj, "data/keep.test.py")          # mirror_exclude must NOT touch absorb
        _mk(proj, "data/old.bak")               # global exclude
        cfg = ES.load_config(amg)
        got = _paths(ES.extract(proj, cfg, amg))
        assert "src/app.py" in got, got
        assert "data/d.json" in got, got
        assert "data/keep.test.py" in got, "per-intent isolation: mirror_exclude is mirror-only"
        for gone in ("src/app.test.py", "src/old.bak", "data/d.tmp", "data/old.bak"):
            assert gone not in got, (gone, got)
        print("PASS  ignore: global + per-intent excludes filter the right source, isolated")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_explicit_source_beats_gitignore():
    proj = Path(tempfile.mkdtemp(prefix="amg-ign2-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        amg.joinpath("config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: [src]\nabsorb_path: [logs]\n",
            encoding="utf-8")
        # .gitignore lists the absorb root (logs/) AND a file inside a mirror source.
        (proj / ".gitignore").write_text("logs/\nsecret.txt\n", encoding="utf-8")
        _mk(proj, "src/keep.py")
        _mk(proj, "src/secret.txt")             # gitignored deeper in a source -> dropped
        _mk(proj, "logs/app.log")               # root gitignored, but explicitly absorbed
        cfg = ES.load_config(amg)
        got = _paths(ES.extract(proj, cfg, amg))
        assert "logs/app.log" in got, "explicit source root beats .gitignore"
        assert "src/keep.py" in got, got
        assert "src/secret.txt" not in got, ".gitignore still filters junk inside a source"
        print("PASS  ignore: explicit source root beats .gitignore; deeper junk still filtered")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_respect_gitignore_off():
    proj = Path(tempfile.mkdtemp(prefix="amg-ign3-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        amg.joinpath("config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: [src]\nrespect_gitignore: false\n",
            encoding="utf-8")
        (proj / ".gitignore").write_text("src/secret.py\n", encoding="utf-8")
        _mk(proj, "src/keep.py")
        _mk(proj, "src/secret.py", "K = 1\n")
        cfg = ES.load_config(amg)
        got = _paths(ES.extract(proj, cfg, amg))
        assert {"src/keep.py", "src/secret.py"} <= got, "respect_gitignore:false ignores .gitignore"
        print("PASS  ignore: respect_gitignore:false makes ignore fully config-driven (no git)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_agent_dir_not_indexed():
    proj = Path(tempfile.mkdtemp(prefix="amg-ign4-"))
    try:
        amg = proj / ".myagent" / "amg"          # a CUSTOM agent dir, not in the base set
        amg.mkdir(parents=True)
        amg.joinpath("config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: ['.']\n", encoding="utf-8")
        _mk(proj, "src/keep.py")
        _mk(proj, ".myagent/skills/amg-bootstrap/scripts/engine.py", "def f():\n    return 1\n")
        cfg = ES.load_config(amg)
        got = _paths(ES.extract(proj, cfg, amg))
        assert "src/keep.py" in got, got
        assert not any(p.startswith(".myagent/") for p in got), \
            "the configured agent dir must never be indexed (no self-indexing)"
        print("PASS  ignore: the resolved agent dir is never indexed, even a custom name")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_by_source_stats():
    proj = Path(tempfile.mkdtemp(prefix="amg-ign5-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        amg.joinpath("config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: [src, missing]\n", encoding="utf-8")
        _mk(proj, "src/keep.py")
        cfg = ES.load_config(amg)
        stats = ES._stats(proj, cfg, amg)
        bs = stats["by_source"]
        assert bs["src"]["found"] and bs["src"]["files"] >= 1, bs
        assert bs["missing"]["found"] is False and bs["missing"]["files"] == 0, bs
        print("PASS  ignore: --stats reports per-source counts (0 / not-found is visible)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_overlap_warns():
    """1.29: a file under both mirror_path and absorb_path is flagged, not resolved
    silently — in --stats (overlapping_sources) and in reconcile.plan (policy_conflicts)."""
    proj = Path(tempfile.mkdtemp(prefix="amg-ign6-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        amg.joinpath("config.yml").write_text(            # '.' (mirror) overlaps data (absorb)
            "active: true\nworking_language: en\nmirror_path: ['.']\nabsorb_path: [data]\n",
            encoding="utf-8")
        _mk(proj, "app.py", "def f():\n    return 1\n")
        _mk(proj, "data/d.json", "42\n")                  # scalar -> a single file-level unit
        cfg = ES.load_config(amg)
        stats = ES._stats(proj, cfg, amg)
        assert "data/d.json" in stats.get("overlapping_sources", []), stats
        assert "overlap_hint" in stats, stats
        summ = rc.plan(proj, amg)
        conflicts = summ.get("policy_conflicts", [])
        assert any("data/d.json" in c["id"] and set(c["policies"]) == {"absorb", "mirror"}
                   for c in conflicts), summ
        print("PASS  ignore: a file under both mirror_path and absorb_path is flagged")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_gitignore_negation():
    """A '!' re-include rule works and order decides: 'logs/' + '!logs/keep.md' brings
    keep.md back, while a re-include overridden by a LATER exclude stays ignored
    (last matching rule wins, the way git reads the file)."""
    proj = Path(tempfile.mkdtemp(prefix="amg-ign8-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        amg.joinpath("config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: ['.']\n", encoding="utf-8")
        (proj / ".gitignore").write_text(
            "logs/\n!logs/keep.md\n!tmp/back.md\ntmp/\n", encoding="utf-8")
        _mk(proj, "logs/app.log")                        # excluded by logs/
        _mk(proj, "logs/keep.md", "# keep\n\ntext\n")    # re-included by !logs/keep.md
        _mk(proj, "tmp/back.md", "# back\n\ntext\n")     # '!' BEFORE tmp/ -> later exclude wins
        _mk(proj, "src/keep.py", "def f():\n    return 1\n")
        cfg = ES.load_config(amg)
        got = _paths(ES.extract(proj, cfg, amg))
        assert "logs/keep.md" in got, ("a '!' rule must re-include", got)
        assert "logs/app.log" not in got, got
        assert "tmp/back.md" not in got, ("a later exclude overrides an earlier '!'", got)
        assert "src/keep.py" in got, got
        print("PASS  ignore: .gitignore '!' re-includes; last matching rule wins")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_missing_in_plan():
    """1.30: a non-existent source path is reported by plan (not just --stats), so a typo
    in mirror_path does not masquerade as 'graph built, added: 0'."""
    proj = Path(tempfile.mkdtemp(prefix="amg-ign7-"))
    try:
        amg = proj / ".claude" / "amg"
        amg.mkdir(parents=True)
        amg.joinpath("config.yml").write_text(
            "active: true\nworking_language: en\nmirror_path: [src, nope]\n", encoding="utf-8")
        _mk(proj, "src/keep.py", "def f():\n    return 1\n")
        summ = rc.plan(proj, amg)
        assert "nope" in summ.get("missing_sources", []), summ
        assert "src" not in summ.get("missing_sources", []), summ
        print("PASS  ignore: a non-existent source path is reported in plan")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    test_per_intent()
    test_explicit_source_beats_gitignore()
    test_respect_gitignore_off()
    test_agent_dir_not_indexed()
    test_by_source_stats()
    test_overlap_warns()
    test_gitignore_negation()
    test_missing_in_plan()
    print("\nALL IGNORE CHECKS PASSED")
