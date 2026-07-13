#!/usr/bin/env python3
"""
selftest_install.py — proves the installer (install.py) end to end, headless.

Checks:
  1. local      : engine + block (markers, preamble stripped) + merged settings + /amg
                  command + config (answered keys parse) + seeded digest; store verifies.
  2. reinstall  : user content above the block survives; the block is replaced not
                  duplicated; an existing config is NOT clobbered; AMG hooks are not
                  duplicated and a user's own hook is preserved.
  3. agents_env : --agent-dir .agents --entrypoint AGENTS.md renders every path to
                  .agents/AGENTS.md (no leftover .claude) — portability without code edits.
  4. global     : engine in a fake HOME, block carries ABSOLUTE engine paths, the graph
                  + config stay LOCAL to the project, digest seeded locally; the
                  machine-wide DEFAULTS config (~/<agent_dir>/amg/config.yml)
                  is written and is NOT a store (no nodes/journal); the local config
                  omits the personal blocks (models, retrieval.embeddings) so they
                  inherit, and the loaders see the merged view (local wins per key).
  5. uninstall  : the block is stripped (user content kept), amg-* engine + AMG hooks
                  removed, the graph kept unless --purge-graph.
  6. answers    : --set with a dotted path lands on the nested key (comments kept);
                  --absorb-once fills absorb_once_path.

Run:  python selftest_install.py
"""
import json
import os
import re
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
        assert any("lifecycle.py prompt-hint" in c for c in cmds), st
        assert "UserPromptSubmit" in st["hooks"], "the gated prompt hint is wired as a hook"
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
        # 1.32: the engine PROMPTS (SKILL.md, agents/*.md) are rendered too, not copied verbatim
        smd = (t / ".agents/skills/amg-bootstrap/SKILL.md").read_text(encoding="utf-8")
        assert ".agents/skills" in smd and ".claude" not in smd, "SKILL.md prompt rendered"
        amd = (t / ".agents/agents/amg-builder.md").read_text(encoding="utf-8")
        assert ".agents/amg" in amd and ".claude" not in amd, "agent prompt rendered"
        assert _cfg(t, ".agents")["eval_gate"]["cases"].startswith(".agents/"), "eval_gate.cases rendered"
        assert _cfg(t, ".agents")["agent_dir"] == ".agents", "agent_dir recorded"
        print("PASS  install: .agents renders AGENTS.md + command + SKILL.md + agent prompts + eval cases, no .claude")
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
        I.main(["--target", str(t), "--scope", "global", "--mirror", "src",
                "--set-global", "retrieval.embeddings.enabled=auto", "--no-verify"])
        assert (home / ".claude/skills/amg-bootstrap/scripts/graph_store.py").exists(), "engine in HOME"
        entry = (home / "CLAUDE.md").read_text(encoding="utf-8")
        assert (home / ".claude").as_posix() + "/skills" in entry, "absolute engine path in block"
        assert "@.claude/amg/digest.md" not in entry, "global @digest import replaced with a note"
        # the GRAPH stays local; HOME now carries the machine-wide DEFAULTS config
        # which is a config layer, NOT a store
        assert (t / ".claude/amg/config.yml").exists(), "local project config written"
        assert (t / ".claude/amg/digest.md").exists(), "digest seeded locally"
        g = yaml.safe_load((home / ".claude/amg/config.yml").read_text(encoding="utf-8"))
        assert g["models"]["module_summary"] == "sonnet", g
        assert g["retrieval"]["embeddings"]["enabled"] == "auto", "--set-global landed"
        assert not (home / ".claude/amg/nodes").exists(), "the defaults layer is not a store"
        # the local config OMITS the personal blocks so they inherit from the global layer
        lcfg = yaml.safe_load((t / ".claude/amg/config.yml").read_text(encoding="utf-8"))
        assert "models" not in lcfg, "models stripped from the local config (inherited)"
        assert "embeddings" not in (lcfg.get("retrieval") or {}), "embeddings stripped (inherited)"
        assert lcfg["retrieval"]["damping"] == 0.85, "the rest of the retrieval block kept"
        assert lcfg["mirror_path"] == ["src"] and lcfg["agent_dir"] == ".claude", lcfg
        # the loaders see the merged view: global under local, local wins per key
        sys.path.insert(0, str(HERE / "skills" / "amg-retrieve" / "scripts"))
        import retrieve as R
        cfg = R.load_config(t / ".claude/amg")
        assert cfg["embeddings"]["enabled"] == "auto", "global default inherited by retrieve"
        with open(t / ".claude/amg/config.yml", "a", encoding="utf-8") as f:
            f.write("\nretrieval:\n  embeddings:\n    blend: 0.9\n")   # local override (dup key: last wins)
        cfg2 = R.load_config(t / ".claude/amg")
        assert cfg2["embeddings"]["blend"] == 0.9, "local key overrides the global layer"
        assert cfg2["embeddings"]["enabled"] == "auto", "unset keys still inherit"
        # extract_structure merges the same way (models visible to the raw-config reader)
        sys.path.insert(0, str(HERE / "skills" / "amg-bootstrap" / "scripts"))
        import extract_structure as ES
        raw = ES.load_config(t / ".claude/amg")
        assert raw["models"]["synthesis"]["model"] == "opus", "global models inherited"
        assert raw["mirror_path"] == ["src"], "local project keys intact"
        print("PASS  install: global engine + defaults layer in HOME; local config inherits "
              "models/embeddings per key (local wins)")
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
        assert "models" not in _cfg(p2), "project-only config inherits models from the global layer"
        assert (home / ".claude/amg/config.yml").exists(), "global defaults layer in place"
        assert not (p2 / ".claude/skills").exists(), "project-only does not copy the engine"
        assert not (p2 / "CLAUDE.md").exists(), "project-only does not write a block (it is global)"
        assert (home / ".claude/skills/amg-bootstrap").exists(), "global engine intact"
        print("PASS  install: --project-only adds a project (local config) to a global install, engine untouched")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for d in (home, p1, p2):
            shutil.rmtree(d, ignore_errors=True)


def test_generic_env():
    t = Path(tempfile.mkdtemp(prefix="amg-inst9-"))
    try:
        # a skill-less AGENTS.md env (Qwen Coder, ...) -> the portable block, no
        # Claude-Code-only hooks or /amg command (codex is its own skill-AWARE mode now)
        I.main(["--target", str(t), "--env", "generic", "--agent-dir", ".agents",
                "--entrypoint", "AGENTS.md", "--mirror", "src", "--no-verify"])
        assert (t / ".agents/skills/amg-bootstrap/scripts/reconcile.py").exists(), "engine placed"
        entry = (t / "AGENTS.md").read_text(encoding="utf-8")
        assert I.BEGIN in entry and "portable block" in entry.lower(), "skill-less block written"
        assert "@.agents/amg/digest.md" not in entry and "@.claude" not in entry, "no @import"
        assert "are absent here" in entry, "block notes the Claude-Code-only conveniences are absent"
        assert ".claude" not in entry, "rendered to .agents, no leftover .claude"
        assert not (t / ".agents/settings.json").exists(), "no hooks in a skill-less env"
        assert not (t / ".agents/commands").exists(), "no /amg command in a skill-less env"
        assert (t / ".agents/amg/config.yml").exists() and (t / ".agents/amg/digest.md").exists()
        print("PASS  install: --env generic writes the portable AGENTS.md block (no hooks/command)")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_codex_env():
    t = Path(tempfile.mkdtemp(prefix="amg-codex-"))
    try:
        # codex is skill-AWARE: skills in .agents/skills, TOML subagents in .codex/agents,
        # a skill-aware AGENTS.md block, NO Claude hooks/command. agent_dir defaults to .agents.
        I.main(["--target", str(t), "--env", "codex", "--mirror", "src", "--no-verify"])
        assert (t / ".agents/skills/amg-bootstrap/scripts/reconcile.py").exists(), "skills in .agents"
        assert not (t / ".agents/agents").exists(), "codex uses TOML subagents, not .md agents"
        toml = (t / ".codex/agents/amg-builder.toml").read_text(encoding="utf-8")
        assert 'name = "amg-builder"' in toml and "developer_instructions" in toml, toml
        assert ".agents/amg" in toml and ".claude" not in toml, "prompt + description rendered to .agents"
        entry = (t / "AGENTS.md").read_text(encoding="utf-8")
        assert I.BEGIN in entry and ".agents/skills" in entry and ".claude" not in entry
        assert "skill" in entry.lower() and ".codex/agents" in entry, "codex block is skill-aware"
        assert not (t / ".agents/settings.json").exists() and not (t / ".agents/commands").exists()
        assert (t / ".agents/amg/config.yml").exists() and (t / ".agents/amg/digest.md").exists()
        # default template models are Claude aliases -> model omitted for codex; set a real
        # codex model + level and reinstall -> both render into the TOML (max clamps to xhigh)
        cfgf = t / ".agents/amg/config.yml"
        cfgf.write_text(re.sub(r"(?m)^  synthesis:.*$",
                               "  synthesis: {model: gpt-5.5, reasoning_effort: max}",
                               cfgf.read_text(encoding="utf-8")), encoding="utf-8")
        I.main(["--target", str(t), "--env", "codex", "--mirror", "src", "--no-verify"])
        synth = (t / ".codex/agents/amg-synth.toml").read_text(encoding="utf-8")
        synth_head = synth.split("developer_instructions")[0]
        assert 'model = "gpt-5.5"' in synth_head, synth_head
        assert 'model_reasoning_effort = "xhigh"' in synth_head, "max clamps to xhigh in Codex"
        assert len(list((t / ".codex/agents").glob("amg-*.toml"))) == 6, "reinstall: no dup TOML"
        builder_head = (t / ".codex/agents/amg-builder.toml").read_text(encoding="utf-8").split("developer_instructions")[0]
        assert "model =" not in builder_head, "a Claude-alias default is omitted for codex"
        linker_head = (t / ".codex/agents/amg-linker.toml").read_text(encoding="utf-8").split("developer_instructions")[0]
        assert 'model_reasoning_effort' not in linker_head, \
            "module_summary is flat -> the linker gets no effort field either"
        # uninstall clears the codex TOML subagents
        I.main(["--target", str(t), "--env", "codex", "--uninstall"])
        assert not list((t / ".codex/agents").glob("amg-*.toml")), "uninstall clears codex TOML"
        print("PASS  install: --env codex -> skills + TOML subagents (model/effort) + skill-aware block")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_models_render():
    t = Path(tempfile.mkdtemp(prefix="amg-inst-models-"))
    try:
        def fm(agent):
            txt = (t / ".claude/agents" / agent).read_text(encoding="utf-8")
            return yaml.safe_load(re.match(r"(?s)^---\n(.*?)\n---", txt).group(1))
        # fresh install renders the template's (flat) models into agent frontmatter,
        # by the role -> agent map (discovery/module_summary/synthesis)
        I.main(["--target", str(t), "--mirror", "src", "--no-verify"])
        assert fm("amg-builder.md")["model"] == "sonnet", "module_summary -> amg-builder"
        assert fm("amg-linker.md")["model"] == "sonnet", "module_summary -> amg-linker too"
        assert fm("amg-synth.md")["model"] == "opus", "synthesis -> amg-synth"
        assert fm("amg-classifier.md")["model"] == "haiku", "discovery -> amg-classifier"
        assert "effort" not in fm("amg-builder.md"), "flat role: no effort field added"
        # switch synthesis to the structured form (with a Codex-only level) and reinstall:
        # model passes through verbatim, reasoning_effort clamps to the Claude Code set
        cfgf = t / ".claude/amg/config.yml"
        c = re.sub(r"(?m)^  synthesis:.*$",
                   "  synthesis: {model: claude-opus-4-8, reasoning_effort: minimal}",
                   cfgf.read_text(encoding="utf-8"))
        cfgf.write_text(c, encoding="utf-8")
        I.main(["--target", str(t), "--mirror", "src", "--no-verify"])
        synth = fm("amg-synth.md")
        assert synth["model"] == "claude-opus-4-8", synth
        assert synth["effort"] == "low", "minimal clamps to low in Claude Code"
        cons = fm("amg-consolidator.md")
        assert cons["model"] == "claude-opus-4-8" and cons["effort"] == "low", cons
        assert fm("amg-builder.md")["model"] == "sonnet", "untouched role kept after reinstall"
        print("PASS  install: models block renders model + clamped effort into agent frontmatter")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_nested_set_and_absorb_once():
    t = Path(tempfile.mkdtemp(prefix="amg-inst-nested-"))
    try:
        # a LOCAL install: full self-contained config; a dotted --set lands on the
        # nested key (the flow's auto-embeddings answer) and --absorb-once
        # fills absorb_once_path
        I.main(["--target", str(t), "--mirror", "src", "--absorb", "logs",
                "--absorb-once", "snapshots,report.pdf",
                "--set", "retrieval.embeddings.enabled=auto", "--no-verify"])
        cfg = _cfg(t)
        assert cfg["retrieval"]["embeddings"]["enabled"] == "auto", cfg["retrieval"]["embeddings"]
        assert cfg["retrieval"]["embeddings"]["blend"] == 0.5, "sibling keys kept"
        assert cfg["absorb_once_path"] == ["snapshots", "report.pdf"], cfg
        assert cfg["models"]["module_summary"] == "sonnet", "local install keeps the full template"
        text = (t / ".claude/amg/config.yml").read_text(encoding="utf-8")
        assert "# auto = use if a backend is installed | on | off" in text, "inline comment kept"
        print("PASS  install: dotted --set hits the nested key (comments kept); --absorb-once fills absorb_once_path")
    finally:
        shutil.rmtree(t, ignore_errors=True)


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
    test_generic_env()
    test_codex_env()
    test_models_render()
    test_nested_set_and_absorb_once()
    test_uninstall()
    print("\nALL INSTALL CHECKS PASSED")
