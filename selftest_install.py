#!/usr/bin/env python3
"""
selftest_install.py — proves the Stage 10 installer (install.py) end to end, headless.

Checks:
  1. local      : engine + block (markers, preamble stripped) + merged settings + /amg
                  command + config (answered keys parse) + seeded digest; store verifies.
  2. reinstall  : user content above the block survives; the block is replaced not
                  duplicated; an existing config is NOT clobbered; AMG hooks are not
                  duplicated and a user's own hook is preserved.
  3. agents_env : --agent-dir .agents --entrypoint AGENTS.md renders every path to
                  .agents/AGENTS.md (no leftover .claude) — portability without code edits.
  4. global     : engine in a fake HOME, block carries ABSOLUTE engine paths, the graph
                  + config stay LOCAL to the project, digest seeded locally.
  5. uninstall  : the block is stripped (user content kept), amg-* engine + AMG hooks
                  removed, the graph kept unless --purge-graph.

Run:  python selftest_install.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import install as I

try:
    import yaml
except ImportError:                       # pragma: no cover
    sys.stderr.write("selftest_install needs PyYAML\n")
    raise


def _cfg(target: Path, agent_dir=".claude") -> dict:
    return yaml.safe_load((target / agent_dir / "amg" / "config.yml").read_text(encoding="utf-8"))


def test_local():
    t = Path(tempfile.mkdtemp(prefix="amg-inst1-"))
    try:
        I.main(["--target", str(t), "--scope", "local",
                "--mirror", "src,doc", "--absorb", "logs", "--exclude", "*.min.js",
                "--set", "active=false", "--set", "working_language=ru",
                "--set", "automation=true"])
        assert (t / ".claude/skills/amg-bootstrap/scripts/graph_store.py").exists()
        assert (t / ".claude/agents/amg-builder.md").exists()
        entry = (t / "CLAUDE.md").read_text(encoding="utf-8")
        assert I.BEGIN in entry and I.END in entry, "markers present"
        assert "## AMG" in entry and "# Project memory" not in entry, "preamble stripped"
        assert ".claude/skills" in entry, "default agent dir paths"
        st = json.loads((t / ".claude/settings.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for ev in st["hooks"].values() for e in ev for h in e["hooks"]]
        assert any("lifecycle.py session-start" in c for c in cmds), st
        assert any("lifecycle.py session-end" in c for c in cmds), st
        assert (t / ".claude/commands/amg.md").exists()
        cfg = _cfg(t)
        assert cfg["active"] is False and cfg["working_language"] == "ru", cfg
        assert cfg["mirror_path"] == ["src", "doc"], cfg
        assert cfg["absorb_path"] == ["logs"], cfg
        assert cfg["exclude"] == ["*.min.js"], cfg
        assert cfg["agent_dir"] == ".claude" and cfg["entrypoint"] == "CLAUDE.md", cfg
        assert (t / ".claude/amg/digest.md").exists()
        assert (t / ".claude/amg/nodes").is_dir(), "verify --repair initialized the store"
        print("PASS  install: local layout, markers, merged hooks, parsed config, seeded digest, verified")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_reinstall_idempotent():
    t = Path(tempfile.mkdtemp(prefix="amg-inst2-"))
    try:
        # a pre-existing entry file with the user's own instructions + a user settings hook
        (t / "CLAUDE.md").write_text("# My project rules\n\nBe concise.\n", encoding="utf-8")
        (t / ".claude").mkdir()
        (t / ".claude/settings.json").write_text(json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}),
            encoding="utf-8")
        I.main(["--target", str(t), "--mirror", "src", "--no-verify"])
        # user content + their hook survive the first install
        entry = (t / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Be concise." in entry and entry.index("Be concise.") < entry.index(I.BEGIN)
        st = json.loads((t / ".claude/settings.json").read_text(encoding="utf-8"))
        starts = [h["command"] for e in st["hooks"]["SessionStart"] for h in e["hooks"]]
        assert "echo mine" in starts and any("lifecycle.py" in c for c in starts), st
        # mutate the config, then reinstall: config kept, block + hooks not duplicated
        cfgf = t / ".claude/amg/config.yml"
        cfgf.write_text(cfgf.read_text(encoding="utf-8") + "\n# my note\n", encoding="utf-8")
        I.main(["--target", str(t), "--mirror", "OTHER", "--no-verify"])
        entry2 = (t / "CLAUDE.md").read_text(encoding="utf-8")
        assert entry2.count(I.BEGIN) == 1 and entry2.count(I.END) == 1, "block not duplicated"
        assert "Be concise." in entry2
        st2 = json.loads((t / ".claude/settings.json").read_text(encoding="utf-8"))
        starts2 = [h["command"] for e in st2["hooks"]["SessionStart"] for h in e["hooks"]]
        assert sum("lifecycle.py" in c for c in starts2) == 1, "AMG hook not duplicated"
        assert "echo mine" in starts2, "user hook preserved"
        assert "# my note" in cfgf.read_text(encoding="utf-8"), "existing config not clobbered"
        assert _cfg(t)["mirror_path"] == ["src"], "config kept the first install's values"
        print("PASS  install: reinstall keeps user content + config, replaces block, no hook dupes")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_agents_env():
    t = Path(tempfile.mkdtemp(prefix="amg-inst3-"))
    try:
        I.main(["--target", str(t), "--agent-dir", ".agents", "--entrypoint", "AGENTS.md",
                "--mirror", "src", "--no-verify"])
        assert (t / ".agents/skills/amg-bootstrap/scripts/lifecycle.py").exists()
        entry = (t / "AGENTS.md").read_text(encoding="utf-8")
        assert ".agents/skills" in entry and ".claude" not in entry, "rendered to .agents, no .claude"
        cmd = (t / ".agents/commands/amg.md").read_text(encoding="utf-8")
        assert ".agents/skills" in cmd and ".claude" not in cmd, cmd
        assert _cfg(t, ".agents")["agent_dir"] == ".agents", "agent_dir recorded"
        print("PASS  install: .agents/AGENTS.md renders every path, no leftover .claude")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_global():
    t = Path(tempfile.mkdtemp(prefix="amg-inst4-"))
    home = Path(tempfile.mkdtemp(prefix="amg-home4-"))
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    try:
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        assert Path.home() == home, "fake HOME in effect"
        I.main(["--target", str(t), "--scope", "global", "--mirror", "src", "--no-verify"])
        assert (home / ".claude/skills/amg-bootstrap/scripts/graph_store.py").exists(), "engine in HOME"
        entry = (home / "CLAUDE.md").read_text(encoding="utf-8")
        assert (home / ".claude").as_posix() + "/skills" in entry, "absolute engine path in block"
        assert "@.claude/amg/digest.md" not in entry, "global @digest import replaced with a note"
        # the graph + config stay LOCAL to the project, never in HOME
        assert (t / ".claude/amg/config.yml").exists() and not (home / ".claude/amg/config.yml").exists()
        assert (t / ".claude/amg/digest.md").exists(), "digest seeded locally"
        print("PASS  install: global engine in HOME with absolute paths; graph + config stay local")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(t, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def test_preserve_other_skills():
    t = Path(tempfile.mkdtemp(prefix="amg-inst6-"))
    try:
        # a user's own skill already sits in the shared agent dir
        user_skill = t / ".claude/skills/my-skill"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text("# my own skill\n", encoding="utf-8")
        I.main(["--target", str(t), "--mirror", "src", "--no-verify"])
        assert (user_skill / "SKILL.md").exists(), "install must not delete other skills"
        assert (t / ".claude/skills/amg-bootstrap").exists(), "amg skill installed alongside"
        I.main(["--target", str(t), "--mirror", "src", "--no-verify"])     # reinstall
        assert (user_skill / "SKILL.md").exists(), "reinstall must not delete other skills"
        print("PASS  install: a user's other skills in a shared agent dir survive install + reinstall")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_build():
    t = Path(tempfile.mkdtemp(prefix="amg-inst7-"))
    try:
        (t / "src").mkdir()
        (t / "src/app.py").write_text("def charge(x):\n    return x\n", encoding="utf-8")
        I.main(["--target", str(t), "--mirror", "src", "--set", "working_language=en", "--build"])
        nodes = list((t / ".claude/amg/nodes").rglob("*.md"))
        assert nodes, "--build runs reconcile bootstrap -> structural nodes exist"
        print("PASS  install: --build builds the structural graph during install (ready this session)")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_project_only_global():
    home = Path(tempfile.mkdtemp(prefix="amg-home8-"))
    p1 = Path(tempfile.mkdtemp(prefix="amg-p1-"))
    p2 = Path(tempfile.mkdtemp(prefix="amg-p2-"))
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    try:
        os.environ["HOME"] = str(home); os.environ["USERPROFILE"] = str(home)
        I.main(["--target", str(p1), "--scope", "global", "--mirror", "src", "--no-verify"])
        # add a second project to the existing global install: local config only, no engine
        I.main(["--target", str(p2), "--project-only", "--mirror", "lib", "--no-verify"])
        assert (p2 / ".claude/amg/config.yml").exists(), "project config written"
        assert _cfg(p2)["mirror_path"] == ["lib"], _cfg(p2)
        assert not (p2 / ".claude/skills").exists(), "project-only does not copy the engine"
        assert not (p2 / "CLAUDE.md").exists(), "project-only does not write a block (it is global)"
        assert (home / ".claude/skills/amg-bootstrap").exists(), "global engine intact"
        print("PASS  install: --project-only adds a project (local config) to a global install, engine untouched")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for d in (home, p1, p2):
            shutil.rmtree(d, ignore_errors=True)


def test_uninstall():
    t = Path(tempfile.mkdtemp(prefix="amg-inst5-"))
    try:
        (t / "CLAUDE.md").write_text("# Keep me\n", encoding="utf-8")
        I.main(["--target", str(t), "--mirror", "src", "--no-verify"])
        I.main(["--target", str(t), "--uninstall"])
        entry = (t / "CLAUDE.md").read_text(encoding="utf-8")
        assert I.BEGIN not in entry and "Keep me" in entry, "block stripped, user content kept"
        assert not (t / ".claude/skills/amg-bootstrap").exists(), "amg skill removed"
        assert not (t / ".claude/commands/amg.md").exists()
        st = json.loads((t / ".claude/settings.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for ev in st["hooks"].values() for e in ev for h in e["hooks"]]
        assert not any("lifecycle.py" in c for c in cmds), "AMG hooks removed"
        assert (t / ".claude/amg/config.yml").exists(), "graph kept without --purge-graph"
        I.main(["--target", str(t), "--uninstall", "--purge-graph"])
        assert not (t / ".claude/amg").exists(), "graph purged with --purge-graph"
        print("PASS  install: uninstall strips block (keeps user content), removes engine/hooks, keeps graph")
    finally:
        shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    test_local()
    test_reinstall_idempotent()
    test_agents_env()
    test_global()
    test_preserve_other_skills()
    test_build()
    test_project_only_global()
    test_uninstall()
    print("\nALL INSTALL CHECKS PASSED")
