#!/usr/bin/env python3
"""
install.py — the AMG installer. The successor to sync_testbed.py.

Deterministic and config-driven: the MODEL conducts the Q&A (see INSTALL.md) and calls
this with the answers; the script does the file work — copy the engine, render the entry
templates per agent_dir/entrypoint, inject the activation block between markers, merge
settings.json, seed digest.md, write config.yml, render the models block into agent
frontmatter, optionally install deps, and verify the store. It NEVER builds the graph:
activation is the user's choice (a question the model
asks), and even when active the structural build runs from the activation loop or
`/amg sync`, never as an install side effect — so installing/activating does not silently
index a whole project.

Local vs global engine; the graph is ALWAYS local:
  local  : engine -> <target>/<agent_dir>/{skills,agents}; block -> <target>/<entrypoint>
  global : engine -> ~/<agent_dir>/{skills,agents};        block -> the environment's
           USER-LEVEL entry (EnvProfile.global_entry: ~/.claude/CLAUDE.md,
           ~/.codex/AGENTS.md, ~/.config/opencode/AGENTS.md, ~/.qwen/QWEN.md), with
           ABSOLUTE engine paths in it. Each project still gets its own local
           <target>/<agent_dir>/amg graph + config.yml; graphs are never shared.

Control-plane mechanics (hooks, the /amg slash command, the @digest import) are Claude
Code features. In another environment the portable substrate is the activation loop plus
verbal triggers plus direct script calls; the block carries that loop regardless.

Run the installer FROM the AMG checkout, which may live ANYWHERE — outside the target
project (recommended: no clutter, and an amg/-named checkout inside a project is what
store resolution must veto) or as a global unpack; the engine is COPIED into
<agent_dir>/, so the checkout is not needed after the install.

CLI (the model fills these from the user's answers):
  python install.py --target <proj> [--scope local|global]
      [--agent-dir .claude] [--entrypoint CLAUDE.md]
      [--env claude-code|codex|opencode|qwen|generic]
                                    # codex    = skills + TOML subagents (.codex/agents)
                                    #            + hooks in .codex/hooks.json (run after
                                    #            the user trusts them via /hooks)
                                    # opencode = skills (.agents/skills, discovered natively)
                                    #            + subagents rendered to .opencode/agent
                                    #            + the event plugin .opencode/plugin/amg.js
                                    #            + the /amg command .opencode/command
                                    # qwen     = .qwen/QWEN.md preset: skills + markdown
                                    #            subagents (.qwen/agents) + session hooks
                                    #            + the /amg command .qwen/commands
                                    # generic  = any UNKNOWN AGENTS.md env: the portable
                                    #            skill-less block, no hooks/command
      [--mirror a,b] [--absorb c,d] [--absorb-once e,f] [--exclude "*.x,*.y"]
      [--set active=false] [--set working_language=ru] [--set automation=true] ...
      [--set retrieval.embeddings.enabled=auto]   # dotted path = nested key
      [--set-global retrieval.embeddings.enabled=auto]  # into the GLOBAL defaults config
      [--deps base,embeddings,text,treesitter] [--no-verify]
      [--build]          # also build the structural graph now (ready this session)
      [--project-only]   # add a project to an existing (global) install: local config only
  python install.py --target <proj> --uninstall [--scope global] [--purge-graph]

Config layers: a GLOBAL install also writes the machine-wide
personal defaults to ~/<agent_dir>/amg/config.yml — the `models` tiering and the
`retrieval.embeddings` block — and the project's LOCAL config then omits those blocks,
inheriting them per key (the loaders deep-merge global -> local; the local file wins).
Project keys (active, sources, working_language, budgets) always stay local: that file
is part of the project's git canon, the global one never leaves the machine.

Reinstall is safe and idempotent: only the AMG skills/agents are replaced (other skills
in a shared ~/.claude are kept), the block is refreshed between its markers, and an
existing project config.yml is never clobbered (its current values are printed so the
flow can confirm them). Project graphs are always local, so a global reinstall never
touches them.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:                                   # pragma: no cover
    yaml = None                                       # model-tiering render needs PyYAML

REPO = Path(__file__).resolve().parent
SKILL_NAMES = ("amg-bootstrap", "amg-retrieve", "amg-consolidate")
BEGIN = "<!-- AMG:BEGIN -->"
END = "<!-- AMG:END -->"

DIGEST_PLACEHOLDER = (
    "<!-- AMG memory digest — auto-generated by consolidation; do not edit by hand. -->\n"
    "## AMG memory digest — standing decisions & open questions\n\n"
    "_No active decisions or open questions captured yet._\n")

# Optional dependency groups (also documented in requirements.txt). base is the only
# hard requirement; the rest degrade gracefully when absent.
DEP_GROUPS = {
    "base": ["pyyaml"],
    "embeddings": ["model2vec"],
    "embeddings-st": ["sentence-transformers"],
    "text": ["pypdf", "python-docx", "openpyxl", "python-pptx"],
    "treesitter": ["tree-sitter", "tree-sitter-language-pack"],
}


# --------------------------------------------------------------------------- #
# Path rendering: the templates carry the Claude Code default `.claude`, rendered
# per agent_dir/entrypoint. For a GLOBAL install the ENGINE (skills/agents) lives in
# the home dir, so its script paths become absolute, while the GRAPH (amg/) stays
# project-local.
# --------------------------------------------------------------------------- #

def _engine_abs(agent_dir: str) -> str:
    """Absolute home-based engine dir for a global install, POSIX-style so the path
    works in both bash and PowerShell command lines."""
    return (Path.home() / agent_dir).as_posix()


def _env_kind(env: str) -> str:
    """Classify the target agent environment into one of five install modes:
      claude-code — skills + subagents + SessionStart/SessionEnd hooks + /amg command;
      codex       — OpenAI Codex: HAS skills (.agents/skills) + subagents (TOML in
                    .codex/agents) + a core hooks engine (.codex/hooks.json; a hook
                    runs only after the user trusts it via /hooks); no slash-command
                    surface for custom commands and no @import;
      opencode    — OpenCode: HAS skills (it discovers .agents/skills/ natively),
                    native subagents (.opencode/agent) and commands
                    (.opencode/command); no hooks — its event surface is the JS
                    plugin (.opencode/plugin), which the install renders;
      qwen        — Qwen Code: HAS skills (.qwen/skills), markdown subagents
                    (.qwen/agents), commands (.qwen/commands), context file QWEN.md,
                    and Claude-shaped session hooks (.qwen/settings.json);
      generic     — any UNKNOWN AGENTS.md env: the portable skill-less block, model-
                    driven via direct script calls (skills still land in
                    .agents/skills — the cross-tool location — so an environment
                    that does discover them is told by the block to prefer them)."""
    e = env.strip().lower()
    if e in ("claude-code", "claude", "cc", ""):
        return "claude-code"
    if e in ("codex", "openai-codex"):
        return "codex"
    if e in ("opencode", "open-code"):
        return "opencode"
    if e in ("qwen", "qwen-code", "qwen-coder"):
        return "qwen"
    return "generic"


# --------------------------------------------------------------------------- #
# The environment registry. Everything an install mode differs BY is data in one
# profile; install()/uninstall()/main() are generic executors over it. Adding an
# environment = adding one entry here (plus a new subagent formatter function ONLY
# when the environment brings a genuinely new format). Path fields are
# engine_root-relative and may carry the "{agent_dir}" placeholder.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EnvProfile:
    agent_dir: str                        # preset (an explicit --agent-dir overrides)
    entrypoint: str                       # preset (an explicit --entrypoint overrides)
    block: str                            # entrypoint/<template> of the activation block
    label: str                            # one-line block label for the install report
    hooks_template: Optional[str] = None  # entrypoint/<file> merged into hooks_dest
    hooks_dest: Optional[str] = None      # where the environment reads its hooks
    command_dest: Optional[str] = None    # the native /amg command file, if any
    command_args: str = "$ARGUMENTS"      # the environment's argument placeholder
    command_full: bool = True             # False = trim the Claude-only tail paragraph
    plugin_dest: Optional[str] = None     # the JS event plugin, if the env loads one
    # How the worker prompts deploy: "copies" = rendered agents/amg-*.md in the agent
    # dir (spawned or read as guidance); "qwen" = the same copies ARE the native
    # subagents, Claude-only frontmatter sanitized; "codex-toml" = no .md copies,
    # TOML subagents rendered instead; "copies+opencode" = copies plus native
    # .opencode/agent renders.
    subagents: str = "copies"
    native_agents: Optional[Tuple[str, str]] = None   # (dir, glob) of native renders
    ships_fork: bool = False              # amg-retriever-fork rides only where fork exists
    # Where the environment reads USER-LEVEL instructions — the global install puts
    # the activation block THERE, not at ~/<entrypoint>: none of the known
    # environments reliably walks past a project root into the home directory
    # (verified per environment; see 12-install). None = home/<entrypoint> (the
    # only guess available for an unknown environment).
    global_entry: Optional[str] = None    # home-relative; "{agent_dir}" substituted
    note: str = ""                        # the per-environment line of the install report


ENVS: Dict[str, EnvProfile] = {
    "claude-code": EnvProfile(
        ".claude", "CLAUDE.md", "CLAUDE.md", "skill-based",
        hooks_template="settings.json", hooks_dest="{agent_dir}/settings.json",
        command_dest="{agent_dir}/commands/amg.md", ships_fork=True,
        global_entry="{agent_dir}/CLAUDE.md"),
    "codex": EnvProfile(
        ".agents", "AGENTS.md", "AGENTS.codex.md", "skill-aware codex",
        hooks_template="hooks.codex.json", hooks_dest=".codex/hooks.json",
        subagents="codex-toml", native_agents=(".codex/agents", "amg-*.toml"),
        global_entry=".codex/AGENTS.md",
        note=("  env     codex (skill-aware): skills + TOML subagents; hooks in "
              ".codex/hooks.json run ONLY after you open /hooks in Codex and trust "
              "the AMG hooks (Codex reviews unmanaged hooks; a reinstall that "
              "changes them re-requires the review). No SessionEnd exists there — "
              "the wrap-up signal stays the block's discipline. No custom-command "
              "surface: the discoverable entry is the skills popup ($-completion).")),
    "opencode": EnvProfile(
        ".agents", "AGENTS.md", "AGENTS.skills.md", "skill-aware portable",
        command_dest=".opencode/command/amg.md", command_full=False,
        plugin_dest=".opencode/plugin/amg.js",
        subagents="copies+opencode", native_agents=(".opencode/agent", "amg-*.md"),
        global_entry=".config/opencode/AGENTS.md",
        note=("  env     opencode (skill-aware): OpenCode discovers the amg-* skills "
              "under {agent_dir}/skills natively; subagents rendered to "
              ".opencode/agent; the AMG plugin in .opencode/plugin replaces session "
              "hooks event-driven (start check, gated hint, incremental transcript "
              "dump), and /amg autocompletes from .opencode/command.")),
    "qwen": EnvProfile(
        ".qwen", "QWEN.md", "AGENTS.skills.md", "skill-aware portable",
        hooks_template="settings.json", hooks_dest="{agent_dir}/settings.json",
        command_dest="{agent_dir}/commands/amg.md", command_args="{{args}}",
        command_full=False, subagents="qwen",
        global_entry="{agent_dir}/QWEN.md",
        note=("  env     qwen (skill-aware): skills in {agent_dir}/skills, subagents "
              "in {agent_dir}/agents, session hooks merged into "
              "{agent_dir}/settings.json, the /amg command in {agent_dir}/commands; "
              "the digest @import is Claude-Code-only — the model reads the digest "
              "itself.")),
    "generic": EnvProfile(
        ".agents", "AGENTS.md", "AGENTS.md", "skill-less / portable",
        note=("  env     skill-less: the SessionStart/SessionEnd hooks and the /amg "
              "command are Claude-Code-only and were NOT written; the block drives "
              "the loop with direct script calls (the digest is read, not "
              "@import-ed). If the environment discovers the amg-* skills on its "
              "own, the block tells the model to prefer them.")),
}


def render_control_text(text: str, agent_dir: str, entrypoint: str,
                        scope: str) -> str:
    """Render `.claude`/`CLAUDE.md` literals in a control-plane file. ENGINE script
    paths (.claude/skills, .claude/agents) point at the install location (absolute for
    global); GRAPH paths (.claude/amg) and the digest import stay project-local."""
    if scope == "global":
        eng = _engine_abs(agent_dir)
        # The @digest import is resolved by Claude Code relative to the (global) entry
        # file, so it cannot point at a per-project graph — replace it with a loop note.
        text = re.sub(r"(?m)^@\.claude/amg/digest\.md\s*$",
                      f"<!-- per-project digest: the loop reads {agent_dir}/amg/digest.md "
                      f"at session start (a global entry file cannot @import it) -->", text)
        text = text.replace(".claude/skills", f"{eng}/skills")
        text = text.replace(".claude/agents", f"{eng}/agents")
    text = text.replace("@.claude/amg", f"@{agent_dir}/amg")     # local digest import
    text = text.replace(".claude", agent_dir)                    # graph + remaining paths
    text = text.replace("CLAUDE.md", entrypoint)
    return text


def _block_body(template_text: str) -> str:
    """The activation block to inject: the entry template minus any standalone
    `# Project memory` preamble (framing for the wholesale dev/testbed copy, redundant
    inside a user's existing entry file). Start at the first `## ` heading."""
    idx = template_text.find("\n## ")
    return template_text[idx + 1:].rstrip() if idx != -1 else template_text.strip()


def inject_block(entry_path: Path, block: str) -> str:
    """Write the activation block between the AMG markers, preserving the user's own
    content above and below. Reinstall replaces only the marked region (idempotent)."""
    wrapped = f"{BEGIN}\n{block.strip()}\n{END}"
    if entry_path.exists():
        text = entry_path.read_text(encoding="utf-8")
        pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
        if pat.search(text):
            new = pat.sub(lambda _m: wrapped, text)
        else:
            new = text.rstrip() + "\n\n" + wrapped + "\n"
    else:
        new = wrapped + "\n"
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(new, encoding="utf-8")
    return new


# --------------------------------------------------------------------------- #
# settings.json hooks: MERGE, never clobber
# --------------------------------------------------------------------------- #

def _is_amg_hook(entry: dict) -> bool:
    """An AMG lifecycle hook entry (so reinstall replaces ours, keeps the user's)."""
    for h in (entry or {}).get("hooks", []):
        if "lifecycle.py" in str(h.get("command", "")):
            return True
    return False


def merge_settings(dest_path: Path, template_obj: dict) -> None:
    """Merge AMG's hook entries into an existing hooks file without dropping the
    user's own hooks or other keys; any prior AMG entries are replaced. One merger
    serves every Claude-shaped hooks carrier — <agent_dir>/settings.json (Claude
    Code, Qwen Code) and .codex/hooks.json (the same {hooks: {Event: [...]}} shape,
    handlers keyed by `timeout_sec`)."""
    existing: dict = {}
    if dest_path.exists():
        try:
            existing = json.loads(dest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    hooks = existing.setdefault("hooks", {})
    for event, entries in (template_obj.get("hooks") or {}).items():
        cur = [e for e in hooks.get(event, []) if not _is_amg_hook(e)]
        cur.extend(entries)
        hooks[event] = cur
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# config.yml: copy the shipped template, fill the answered keys (preserve comments)
# --------------------------------------------------------------------------- #

# A real config key line: `key:` (or a commented-out `# key:`) at column 0, the colon
# immediately after the key. This deliberately does NOT match prose in the template's
# header comments (e.g. `#   mirror_path : the graph is a LIVE PROJECTION ...`), which
# is indented and has a space before the colon — matching it would inject a duplicate key.
def _keyline(key: str) -> "re.Pattern[str]":
    return re.compile(rf"^(# ?)?{re.escape(key)}:")


def _set_scalar(text: str, key: str, value: str) -> str:
    """Set a top-level scalar key (commented or not) in place; append if missing."""
    pat = re.compile(rf"(?m)^(# ?)?{re.escape(key)}:.*$")
    new, n = pat.subn(lambda _m: f"{key}: {value}", text, count=1)
    return new if n else text.rstrip() + f"\n{key}: {value}\n"


def _set_list(text: str, key: str, items: List[str]) -> str:
    """Replace a top-level list key (block, flow, or commented) with a flow list. Items
    are quoted, so globs like *.tmp stay valid YAML. Empty items -> leave unchanged."""
    if not items:
        return text
    flow = "[" + ", ".join(f'"{it}"' for it in items) + "]"
    keyline = _keyline(key)
    itemline = re.compile(r"^\s*#?\s*-\s")
    lines = text.split("\n")
    out: List[str] = []
    i, done = 0, False
    while i < len(lines):
        if not done and keyline.match(lines[i]):
            done = True
            i += 1
            while i < len(lines) and itemline.match(lines[i]):   # consume block items
                i += 1
            out.append(f"{key}: {flow}")
            continue
        out.append(lines[i])
        i += 1
    if not done:
        out.append(f"{key}: {flow}")
    return "\n".join(out)


def _set_nested(text: str, dotted: str, value: str) -> str:
    """Set a NESTED scalar key given as a dotted path (e.g.
    retrieval.embeddings.enabled) in the template text, preserving comments. Each
    segment is located inside its parent's block by indentation (2 spaces per level);
    the leaf keeps its inline comment. Missing segments are created at the end of the
    deepest existing parent's block. Top-level keys go through _set_scalar."""
    keys = dotted.split(".")
    lines = text.split("\n")
    lo, hi, indent = 0, len(lines), 0            # search window = parent's block
    for depth, key in enumerate(keys):
        leaf = depth == len(keys) - 1
        pat = re.compile((rf"^{' ' * indent}(# ?)?{re.escape(key)}:" if leaf
                          else rf"^{' ' * indent}{re.escape(key)}:"))
        found = next((i for i in range(lo, hi) if pat.match(lines[i])), None)
        if found is None:                        # create the remaining chain
            chain = []
            for k in keys[depth:-1]:
                chain.append(f"{' ' * indent}{k}:")
                indent += 2
            chain.append(f"{' ' * indent}{keys[-1]}: {value}")
            lines[hi:hi] = chain
            return "\n".join(lines)
        if leaf:                                 # replace the value, keep the comment
            rest = lines[found].split(":", 1)[1]
            cpos = rest.find("#")
            comment = ("        " + rest[cpos:]) if cpos != -1 else ""
            lines[found] = f"{' ' * indent}{key}: {value}{comment}"
            return "\n".join(lines)
        lo = found + 1                           # narrow to this key's block
        j = lo
        while j < hi:
            if not lines[j].strip():
                j += 1
                continue
            if len(lines[j]) - len(lines[j].lstrip(" ")) <= indent:
                break
            j += 1
        hi = j
        indent += 2
    return "\n".join(lines)


def _strip_top_block(text: str, key: str) -> str:
    """Remove a top-level key's line, its indented block, and the contiguous run of
    column-0 comment lines right above it (the template's banner). Used to keep a
    PERSONAL key out of a written local config so it inherits from the global
    defaults layer instead."""
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    keyline = re.compile(rf"^{re.escape(key)}:")
    while i < len(lines):
        if keyline.match(lines[i]):
            while out and out[-1].startswith("#"):        # the banner above
                out.pop()
            if out and not out[-1].strip():               # one separating blank
                out.pop()
            i += 1
            while i < len(lines):                         # the indented block
                if not lines[i].strip():                  # blank: block may end here
                    j = i
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j >= len(lines) or not lines[j].startswith((" ", "\t")):
                        break
                elif not lines[i].startswith((" ", "\t")):
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _strip_sub_block(text: str, parent: str, key: str) -> str:
    """Remove a nested `key:` block (its line + deeper-indented lines + the
    same-indent comment run right above it) inside a top-level `parent:` block.
    Same purpose as _strip_top_block, one level down."""
    lines = text.split("\n")
    p = next((i for i, ln in enumerate(lines)
              if re.match(rf"^{re.escape(parent)}:", ln)), None)
    if p is None:
        return text
    end = p + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        end += 1
    sub = re.compile(rf"^(\s+){re.escape(key)}:")
    for i in range(p + 1, end):
        m = sub.match(lines[i])
        if not m:
            continue
        indent = m.group(1)
        j = i + 1
        while j < end and (lines[j].startswith(indent + " ")
                           or (not lines[j].strip() and j + 1 < end
                               and lines[j + 1].startswith(indent + " "))):
            j += 1
        k = i
        while k > p + 1 and lines[k - 1].startswith(indent + "#"):
            k -= 1
        return "\n".join(lines[:k] + lines[j:])
    return text


def _deep_merge(base: Dict[str, object], over: Dict[str, object]) -> Dict[str, object]:
    """Per-key overlay (nested dicts merge key-by-key, scalars/lists replace whole) —
    the same rule the loaders use to overlay the local config on the global one."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)      # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def _set_in(data: Dict[str, object], dotted: str, value: str) -> None:
    """Set a dotted-path scalar in a nested dict; the value is parsed like YAML
    (true/off/0.5 become their typed forms, matching a hand-edited config)."""
    keys = dotted.split(".")
    cur: Dict[str, object] = data
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[keys[-1]] = yaml.safe_load(value) if yaml is not None else value


GLOBAL_CONFIG_HEADER = """\
# =============================================================================
# AMG - machine-wide personal defaults (the GLOBAL config layer).
#
# Every project's local <agent_dir>/amg/config.yml INHERITS these keys and
# overrides them per key (deep merge; the local file wins). Keep PERSONAL,
# machine-level preferences here - the `models` tiering, the embeddings
# backend; PROJECT keys (active, sources, working_language, budgets) belong in
# each project's local config, which is part of that project's git canon. This
# file never leaves this machine and is NOT a store: the graph always lives in
# the project (<project>/<agent_dir>/amg).
# =============================================================================
"""


def write_global_config(engine_agent_dir: Path, overrides: Dict[str, str]) -> bool:
    """Create ~/<agent_dir>/amg/config.yml — the machine-wide PERSONAL defaults every
    installed project's local config inherits per key: the
    `models` tiering and the `retrieval.embeddings` block, with values from the
    shipped template plus any --set-global answers (dotted keys). Never clobbers an
    existing global config. Needs PyYAML (like the models render); returns True when
    written."""
    dest = engine_agent_dir / "amg" / "config.yml"
    if dest.exists():
        return False
    if yaml is None:
        print("  global  defaults skipped (PyYAML not importable; the local config stays full)")
        return False
    tpl = yaml.safe_load((REPO / "config.yml").read_text(encoding="utf-8")) or {}
    data: Dict[str, object] = {
        "models": tpl.get("models") or {},
        "retrieval": {"embeddings": (tpl.get("retrieval") or {}).get("embeddings") or {}},
    }
    for dotted, value in overrides.items():
        _set_in(data, dotted, value)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(GLOBAL_CONFIG_HEADER
                    + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return True


def _existing_config_summary(cfg_path: Path) -> str:
    """The key values of an existing config, one line — printed when a reinstall
    keeps it, so the install flow can show and confirm what is in force instead of
    silently keeping unknown state."""
    if yaml is None or not cfg_path.exists():
        return ""
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return ""
    emb = ((cfg.get("retrieval") or {}).get("embeddings") or {}).get("enabled")
    shown = [("active", cfg.get("active")), ("working_language", cfg.get("working_language")),
             ("mirror_path", cfg.get("mirror_path")), ("absorb_path", cfg.get("absorb_path")),
             ("absorb_once_path", cfg.get("absorb_once_path")), ("exclude", cfg.get("exclude")),
             ("automation", cfg.get("automation")), ("session_policy", cfg.get("session_policy")),
             ("embeddings.enabled", emb)]
    return "; ".join(f"{k}={v!r}" for k, v in shown if v is not None)


def write_config(dest_amg: Path, agent_dir: str, entrypoint: str,
                 mirror: List[str], absorb: List[str], absorb_once: List[str],
                 exclude: List[str], scalars: Dict[str, str],
                 strip_personal: bool = False) -> Optional[List[str]]:
    """Create <agent_dir>/amg/config.yml from the shipped template, filling the answered
    keys. An EXISTING config is preserved — but keys EXPLICITLY passed on THIS run
    (--set / --mirror / --absorb / --absorb-once / --exclude) are applied to it as
    surgical line edits (same _set_* machinery over the existing text; comments and
    untouched keys stay) — an answer the flow collected must never silently vanish.
    Returns None for a fresh write, else the list of updated keys (possibly empty).
    agent_dir/entrypoint are always recorded so resolution and the docs agree with the
    install. With strip_personal (a global-scope install that wrote the global defaults
    config), the PERSONAL blocks — `models` and `retrieval.embeddings` — are omitted
    from a fresh config so they inherit from the global layer instead of shadowing it.
    A dotted --set key (retrieval.embeddings.enabled) lands on the nested key."""
    dest = dest_amg / "config.yml"
    if dest.exists():
        text = dest.read_text(encoding="utf-8")
        updated: List[str] = []
        for key, value in scalars.items():
            text = _set_nested(text, key, value) if "." in key else _set_scalar(text, key, value)
            updated.append(key)
        for key, items in (("mirror_path", mirror), ("absorb_path", absorb),
                           ("absorb_once_path", absorb_once), ("exclude", exclude)):
            if items:
                text = _set_list(text, key, items)
                updated.append(key)
        # The environment identity always follows the CURRENT install (an env switch
        # must not leave a stale agent_dir behind).
        text = _set_scalar(text, "agent_dir", agent_dir)
        text = _set_scalar(text, "entrypoint", entrypoint)
        dest.write_text(text, encoding="utf-8")
        return updated
    text = (REPO / "config.yml").read_text(encoding="utf-8")
    if strip_personal:
        text = _strip_top_block(text, "models")
        text = _strip_sub_block(text, "retrieval", "embeddings")
    for key, value in scalars.items():
        text = _set_nested(text, key, value) if "." in key else _set_scalar(text, key, value)
    text = _set_list(text, "mirror_path", mirror)
    text = _set_list(text, "absorb_path", absorb)
    text = _set_list(text, "absorb_once_path", absorb_once)
    if exclude:
        text = _set_list(text, "exclude", exclude)
    text = _set_scalar(text, "agent_dir", agent_dir)
    text = _set_scalar(text, "entrypoint", entrypoint)
    if agent_dir != ".claude":               # render the one path-valued default
        text = re.sub(r"(?m)^(\s*cases:\s*)\.claude/",
                      lambda m: m.group(1) + agent_dir + "/", text)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return None


# --------------------------------------------------------------------------- #
# Model tiering: render config.yml `models` into agent frontmatter
#
# config.yml `models` is the SINGLE SOURCE OF TRUTH for per-role model + reasoning
# effort; install (and reinstall) renders it into the installed agent definitions, so
# agents/*.md carry only the rendered result. Each role maps to one or more agents.
# A role value is a flat model string ("opus") or a {model, reasoning_effort} mapping.
#
# Known upstream caveat (Claude Code issue #44385): a subagent's frontmatter `model:`
# is currently IGNORED when the agent is spawned without an explicit model parameter
# (it inherits the parent model); the `effort` field IS honored. We still write
# `model:` as the documented, forward-compatible surface — to force a model today,
# pass it on the Agent call or set CLAUDE_CODE_SUBAGENT_MODEL.
# --------------------------------------------------------------------------- #

ROLE_AGENTS = {
    "discovery": ("amg-classifier", "amg-retriever"),
    # amg-linker is bulk confirmation over bounded candidate batches — the same
    # tier as the builder; its global reach comes from candidate nomination, not
    # from model size.
    "module_summary": ("amg-builder", "amg-linker"),
    "synthesis": ("amg-synth", "amg-consolidator"),
    # structural_extraction is deterministic — no model, no agent.
    # amg-retriever-fork is deliberately absent: a fork inherits the PARENT model
    # by definition (Claude Code `context: fork`), so tiering does not apply to it.
}

# AMG's neutral `reasoning_effort` clamps to what each environment supports. Claude
# Code's subagent `effort` field is low|medium|high|xhigh|max (no `minimal`); Codex's
# `model_reasoning_effort` is minimal|low|medium|high|xhigh (no `max`).
_EFFORT_CLAMP = {
    "claude-code": {"minimal": "low", "low": "low", "medium": "medium",
                    "high": "high", "xhigh": "xhigh", "max": "max"},
    "codex": {"minimal": "minimal", "low": "low", "medium": "medium",
              "high": "high", "xhigh": "xhigh", "max": "xhigh"},
}


def _resolve_role(value: object) -> tuple[Optional[str], Optional[str]]:
    """A models.<role> entry is either a flat model string or a {model,
    reasoning_effort} mapping. Return (model, reasoning_effort); either may be None."""
    if isinstance(value, str):
        return (value.strip() or None), None
    if isinstance(value, dict):
        model = value.get("model")
        eff = value.get("reasoning_effort")
        return ((str(model).strip() or None) if model else None,
                (str(eff).strip() or None) if eff else None)
    return None, None


def _clamp_effort(effort: Optional[str], env: str) -> Optional[str]:
    """Map a neutral reasoning_effort level to the one this environment supports.
    Unknown level -> None (skip)."""
    if not effort:
        return None
    table = _EFFORT_CLAMP.get(_env_kind(env), _EFFORT_CLAMP["claude-code"])
    return table.get(effort.strip().lower())


def _set_agent_field(text: str, key: str, value: str) -> str:
    """Set `key: value` inside the YAML frontmatter (first --- ... --- block) of an
    agent markdown file, preserving description/tools and the body. Replaces the line
    if present, else appends it to the frontmatter. No frontmatter -> text unchanged."""
    m = re.match(r"(?s)^(---\n)(.*?)(\n---\n?)(.*)$", text)
    if not m:
        return text
    head, fm, close, body = m.groups()
    line = f"{key}: {value}"
    pat = re.compile(rf"(?m)^{re.escape(key)}:.*$")
    fm = pat.sub(line, fm, count=1) if pat.search(fm) else fm.rstrip() + "\n" + line
    return head + fm + close + body


def _drop_agent_field(text: str, key: str) -> str:
    """Remove a single-line `key: ...` from the agent frontmatter (first ---...---
    block). Multi-line values (e.g. a `>-` description) are not this helper's target."""
    m = re.match(r"(?s)^(---\n)(.*?)(\n---\n?)(.*)$", text)
    if not m:
        return text
    head, fm, close, body = m.groups()
    fm = re.sub(rf"(?m)^{re.escape(key)}:.*\n?", "", fm).rstrip()
    return head + fm + close + body


def render_agent_models(agents_dir: Path, models_cfg: dict, env: str) -> None:
    """Render per-role model + reasoning effort from config.yml `models` into the
    installed agent frontmatter. Idempotent: reinstall re-copies the source agents,
    then this re-applies. A role given only a model gets no effort field (the
    model/tool default applies).

    Runs for EVERY non-TOML environment: even where the files are loop guidance
    rather than spawned subagents, frontmatter diverging from config.yml `models`
    misleads (a field failure). For qwen the files ARE its native subagents
    (.qwen/agents/*.md), so the frontmatter is sanitized to what Qwen Code reads:
    Claude tool names and the `effort` field are dropped (absent tools = all tools),
    and `model` is written only when it is a real id rather than a Claude alias —
    an alias would name a model the environment does not serve."""
    if not isinstance(models_cfg, dict):
        models_cfg = {}
    qwen = _env_kind(env) == "qwen"
    touched: List[str] = []
    for role, agents in ROLE_AGENTS.items():
        model, eff = _resolve_role(models_cfg.get(role))
        eff = _clamp_effort(eff, env)
        for ag in agents:
            f = agents_dir / f"{ag}.md"
            if not f.exists():
                continue
            text = f.read_text(encoding="utf-8")
            changed = False
            if qwen:
                for key in ("tools", "effort"):
                    new = _drop_agent_field(text, key)
                    changed |= new != text
                    text = new
                if model and model not in _CLAUDE_ALIASES:
                    text = _set_agent_field(text, "model", model)
                    changed = True
                else:                        # alias or unset: let Qwen use its default
                    new = _drop_agent_field(text, "model")
                    changed |= new != text
                    text = new
            else:
                if model:
                    text = _set_agent_field(text, "model", model)
                    changed = True
                if eff:
                    text = _set_agent_field(text, "effort", eff)
                    changed = True
            if changed:
                f.write_text(text, encoding="utf-8")
                touched.append(ag)
    if touched:
        print(f"  models  rendered model/effort into {len(touched)} agent(s) from config.yml models")


def render_opencode_agents(repo_agents_dir: Path, dest_dir: Path, models_cfg: dict) -> List[str]:
    """Render agents/amg-*.md into OpenCode subagents in <dest_dir> (.opencode/agent):
    frontmatter `description` + `mode: subagent` + (when the configured model is a real
    OpenCode id like `anthropic/claude-sonnet-4-5`, not a Claude alias) `model`; the
    prompt body follows. Tool restriction is left to the prompts (OpenCode tool keys
    are its own; an allow-map that omitted one would silently change defaults).
    Reinstall replaces the amg-*.md set."""
    if not isinstance(models_cfg, dict):
        models_cfg = {}
    agent_role = {a: r for r, ags in ROLE_AGENTS.items() for a in ags}
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("amg-*.md"):            # reinstall: replace, don't duplicate
        old.unlink()
    written: List[str] = []
    for ag in sorted(repo_agents_dir.glob("amg-*.md")):
        if ag.stem == "amg-retriever-fork":
            continue                 # `context: fork` is Claude-Code-only; no OpenCode analog
        fm, body = _md_frontmatter_body(ag.read_text(encoding="utf-8"))
        name = str(fm.get("name") or ag.stem)
        desc = render_control_text(str(fm.get("description") or ""), ".agents",
                                   "AGENTS.md", "local").strip().replace("\n", " ")
        model, _eff = _resolve_role(models_cfg.get(agent_role.get(name, "")))
        lines = ["---", f"description: {json.dumps(desc)}", "mode: subagent"]
        if model and model not in _CLAUDE_ALIASES:
            lines.append(f"model: {json.dumps(str(model))}")
        lines.append("---")
        instr = render_control_text(body, ".agents", "AGENTS.md", "local")
        (dest_dir / f"{name}.md").write_text("\n".join(lines) + "\n\n" + instr + "\n",
                                             encoding="utf-8")
        written.append(name)
    if written:
        print(f"  agents  rendered {len(written)} OpenCode subagent(s) -> {dest_dir}")
    return written


def render_plugin(engine_root: Path, prof: EnvProfile, agent_dir: str,
                  entrypoint: str, scope: str) -> Optional[Path]:
    """Render the AMG event plugin (entrypoint/plugin/amg.js) to the profile's
    destination. Today only OpenCode loads one — `{plugin,plugins}/*.{ts,js}` from
    every config directory: the project's `.opencode` for a local install,
    `~/.opencode` for a global one (both on its discovery chain). The plugin is the
    event-driven replacement for session hooks (session.created -> start-check,
    chat.message -> the gated hint, session.idle -> the throttled incremental
    transcript dump + usage attribution); all logic lives in lifecycle.py, the JS
    only routes events, so the render is the same path substitution the blocks get."""
    if prof.plugin_dest is None:
        return None
    dest = engine_root / prof.plugin_dest.format(agent_dir=agent_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_control_text(
        (REPO / "entrypoint" / "plugin" / "amg.js").read_text(encoding="utf-8"),
        agent_dir, entrypoint, scope), encoding="utf-8")
    return dest


# The trailing paragraph of commands/amg.md is Claude-Code commentary (skills as
# slash commands, the .claude default note); non-full command renders cut it here.
_COMMAND_CLAUDE_TAIL = "Each work verb is also directly available as its own skill"


def render_command(engine_root: Path, prof: EnvProfile, agent_dir: str,
                   entrypoint: str, scope: str) -> Optional[Path]:
    """Render the /amg command into the environment's NATIVE command surface, so
    typing `/amg` autocompletes everywhere the environment has one (a field gap:
    outside Claude Code the `/` menu knew nothing about AMG). One source template
    (entrypoint/commands/amg.md); the profile names the destination, the argument
    placeholder ($ARGUMENTS / {{args}} / whatever a future environment reads), and
    whether the file goes verbatim (`command_full` — Claude Code) or trimmed: the
    trimmed form keeps only `description` of the frontmatter (foreign keys are
    another environment's to reject) and drops the Claude-only tail paragraph.
    A profile without a command surface returns None."""
    if prof.command_dest is None:
        return None
    dest = engine_root / prof.command_dest.format(agent_dir=agent_dir)
    src = (REPO / "entrypoint" / "commands" / "amg.md").read_text(encoding="utf-8")
    if prof.command_full:
        text = render_control_text(src, agent_dir, entrypoint, scope)
    else:
        m = re.match(r"(?s)^---\n(.*?)\n---\n?(.*)$", src)
        fm_text, body = (m.group(1), m.group(2)) if m else ("", src)
        dm = re.search(r"(?m)^description:\s*(.+)$", fm_text)
        desc = dm.group(1).strip() if dm else "AMG memory control"
        cut = body.find(_COMMAND_CLAUDE_TAIL)
        if cut != -1:
            body = body[:cut].rstrip() + (
                "\n\n(Paths above are rendered for this environment; the work verbs "
                "ride the amg-* skills it discovers.)\n")
        body = render_control_text(body, agent_dir, entrypoint, scope)
        text = f"---\ndescription: {json.dumps(desc)}\n---\n\n" + body.strip() + "\n"
    text = text.replace("$ARGUMENTS", prof.command_args)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


def _read_models(config_path: Path, agent_dir: str) -> dict:
    """The `models` block as the LOADERS see it: the machine-wide global config
    (~/<agent_dir>/amg/config.yml, if any) deep-merged under the project's local one.
    Empty dict if nothing is readable or PyYAML is missing."""
    if yaml is None:
        return {}

    def _load(p: Path) -> dict:
        if not p.exists():
            return {}
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return cfg if isinstance(cfg, dict) else {}
        except (OSError, ValueError):
            return {}

    merged = _deep_merge(_load(Path.home() / agent_dir / "amg" / "config.yml"),
                         _load(config_path))
    models = merged.get("models")
    return models if isinstance(models, dict) else {}


# Claude family aliases. For a CODEX install these in config.yml mean the unchanged
# Claude default (not a deliberate Codex model), so they are NOT written into the Codex
# TOML — Codex falls back to its session model; a real id (gpt-5.5, ...) passes through.
_CLAUDE_ALIASES = {"opus", "sonnet", "haiku", "fable", "best", "default", "opusplan"}


def _md_frontmatter_body(text: str) -> tuple[dict, str]:
    """Split an agent .md into (frontmatter dict, body)."""
    m = re.match(r"(?s)^---\n(.*?)\n---\n?(.*)$", text)
    if not m:
        return {}, text.strip()
    fm = (yaml.safe_load(m.group(1)) or {}) if yaml else {}
    return fm, m.group(2).strip()


def _toml_basic(s: str) -> str:
    """Escape a one-line TOML basic string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def render_codex_agents(repo_agents_dir: Path, dest_dir: Path, models_cfg: dict,
                        env: str) -> List[str]:
    """Render agents/amg-*.md into Codex TOML subagents in <dest_dir> (.codex/agents).
    Each TOML carries name, description, developer_instructions (the prompt body, paths
    rendered to the codex skills/graph dir), and — from config.yml `models` — the
    reasoning effort and (when the configured model is a real id, not a Claude alias) the
    model. Codex honors these TOML fields; a subagent's .md frontmatter does not apply in
    Codex. Reinstall replaces the amg-*.toml set, never duplicating it."""
    agent_role = {a: r for r, ags in ROLE_AGENTS.items() for a in ags}
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob("amg-*.toml"):          # reinstall: replace, don't duplicate
        old.unlink()
    written: List[str] = []
    for ag in sorted(repo_agents_dir.glob("amg-*.md")):
        if ag.stem == "amg-retriever-fork":
            continue                 # `context: fork` is Claude-Code-only; no TOML analog
        fm, body = _md_frontmatter_body(ag.read_text(encoding="utf-8"))
        name = str(fm.get("name") or ag.stem)
        desc = render_control_text(str(fm.get("description") or ""), ".agents",
                                   "AGENTS.md", "local").strip().replace("\n", " ")
        model, eff = _resolve_role(models_cfg.get(agent_role.get(name, "")))
        if model is None:
            model = fm.get("model")                  # fall back to the .md default
        eff = _clamp_effort(eff, env)
        # Render the prompt body's .claude paths to the codex skills/graph dir (.agents).
        instr = render_control_text(body, ".agents", "AGENTS.md", "local").replace("'''", "''")
        lines = [f'name = "{_toml_basic(name)}"', f'description = "{_toml_basic(desc)}"']
        if model and model not in _CLAUDE_ALIASES:
            lines.append(f'model = "{_toml_basic(str(model))}"')
        if eff:
            lines.append(f'model_reasoning_effort = "{_toml_basic(eff)}"')
        lines.append("developer_instructions = '''\n" + instr + "\n'''")
        (dest_dir / f"{name}.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(name)
    if written:
        print(f"  codex   rendered {len(written)} TOML subagent(s) -> {dest_dir}")
    return written


# --------------------------------------------------------------------------- #
# Engine placement, digest seed, deps, verify
# --------------------------------------------------------------------------- #

def place_engine(dest_agent_dir: Path, agent_dir: str, entrypoint: str, scope: str,
                 env: str) -> None:
    """Install the engine, replacing ONLY the AMG skills and agents. A shared agent dir
    (especially a global ~/.claude) keeps the user's other skills and agents, and a
    reinstall refreshes just AMG. Never touches __pycache__.

    The shipped PROMPTS (each skill's SKILL.md and agents/*.md) carry `.claude`/`CLAUDE.md`
    as the Claude Code default and are RENDERED to the configured agent dir on copy, exactly
    like the entry templates. Copying them verbatim would leave `.claude/...`
    command paths that are wrong under any other agent dir, and relative script paths that
    are wrong for a global install. (References/*.md stay verbatim — they are docs; the
    docs-neutralization pass is the translation stage.)"""
    skills_dest = dest_agent_dir / "skills"
    skills_dest.mkdir(parents=True, exist_ok=True)
    for sk in SKILL_NAMES:
        src = REPO / "skills" / sk
        if not src.is_dir():
            raise SystemExit(f"missing engine skill in repo: {src}")
        dest = skills_dest / sk
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
        smd = dest / "SKILL.md"
        if smd.exists():
            smd.write_text(render_control_text(smd.read_text(encoding="utf-8"),
                                               agent_dir, entrypoint, scope), encoding="utf-8")
    # Ship the engine version so the installed engine can report itself
    # (lifecycle.py version / the status header): the repo's VERSION rides inside the
    # amg-bootstrap skill, which every install carries.
    ver = REPO / "VERSION"
    if ver.exists():
        (skills_dest / "amg-bootstrap" / "VERSION").write_text(
            ver.read_text(encoding="utf-8").strip() + "\n", encoding="utf-8")
    prof = ENVS[_env_kind(env)]
    if prof.subagents == "codex-toml":
        return                       # no .md copies: the TOML render is the only form
    agents_dest = dest_agent_dir / "agents"
    agents_dest.mkdir(parents=True, exist_ok=True)
    for ag in sorted((REPO / "agents").glob("amg-*.md")):
        # The forked retriever rides on a conversation-inheriting fork mechanism —
        # a profile without one must not ship it, or the file would document a
        # capability the environment cannot deliver.
        if ag.stem == "amg-retriever-fork" and not prof.ships_fork:
            continue
        agents_dest.joinpath(ag.name).write_text(
            render_control_text(ag.read_text(encoding="utf-8"), agent_dir, entrypoint, scope),
            encoding="utf-8")


def seed_digest(dest_amg: Path) -> None:
    """Seed an empty digest.md so the block's @import resolves before the first
    consolidation. Never overwrites a real digest."""
    dest_amg.mkdir(parents=True, exist_ok=True)
    digest = dest_amg / "digest.md"
    if not digest.exists():
        digest.write_text(DIGEST_PLACEHOLDER, encoding="utf-8")


def install_deps(groups: List[str]) -> None:
    """pip-install the requested optional groups (opt-in via --deps)."""
    pkgs: List[str] = []
    for g in groups:
        if g not in DEP_GROUPS:
            print(f"  deps: unknown group {g!r} (known: {', '.join(DEP_GROUPS)})")
            continue
        pkgs += DEP_GROUPS[g]
    if not pkgs:
        return
    print(f"  deps: pip install {' '.join(pkgs)}")
    subprocess.run([sys.executable, "-m", "pip", "install", *pkgs], check=False)


def verify_store(engine_agent_dir: Path, graph_agent_dir: Path) -> int:
    """Initialize and verify the LOCAL store with the INSTALLED engine (no graph build).
    engine_agent_dir holds skills/ (global: home); graph_agent_dir holds amg/ (project)."""
    gs = engine_agent_dir / "skills" / "amg-bootstrap" / "scripts" / "graph_store.py"
    root = str(graph_agent_dir)
    subprocess.run([sys.executable, str(gs), "init", "--root", root], check=False)
    r = subprocess.run([sys.executable, str(gs), "verify", "--repair", "--root", root])
    return r.returncode


def build_graph(engine_agent_dir: Path, project_root: Path, graph_agent_dir: Path) -> None:
    """Optionally build the STRUCTURAL graph now (--build), so the memory is ready in the
    same session instead of waiting for the activation loop. This is the deterministic
    skeleton only; semantic enrichment (summaries + edges) is model-driven (the
    amg-bootstrap skill / activation loop), since a script cannot run subagents."""
    rc = engine_agent_dir / "skills" / "amg-bootstrap" / "scripts" / "reconcile.py"
    print("  build   reconcile bootstrap (structural skeleton)")
    subprocess.run([sys.executable, str(rc), "bootstrap", str(project_root),
                    "--root", str(graph_agent_dir)], check=False)


# --------------------------------------------------------------------------- #
# Install / uninstall
# --------------------------------------------------------------------------- #

def install(target: Path, scope: str, agent_dir: str, entrypoint: str,
            mirror: List[str], absorb: List[str], absorb_once: List[str],
            exclude: List[str], scalars: Dict[str, str],
            set_global: Dict[str, str], deps: List[str], verify: bool,
            build: bool = False, project_only: bool = False,
            env: str = "claude-code") -> None:
    target = target.resolve()
    # project_only adds a project to an existing GLOBAL install, so the engine it
    # verifies/builds with lives in the home dir, not under the new project.
    engine_root = Path.home() if (scope == "global" or project_only) else target
    engine_agent_dir = engine_root / agent_dir
    graph_agent_dir = target / agent_dir                         # the graph is ALWAYS local
    kind = _env_kind(env)
    prof = ENVS[kind]
    # A LOCAL block sits at the project's entry point; a GLOBAL one goes where the
    # environment actually reads user-level instructions (~/.claude/CLAUDE.md,
    # ~/.codex/AGENTS.md, ~/.config/opencode/AGENTS.md, ~/.qwen/QWEN.md) — a file at
    # the home ROOT is read only when a project happens to live under it, which no
    # profile relies on.
    if scope == "global" and prof.global_entry:
        entry_path = engine_root / prof.global_entry.format(agent_dir=agent_dir)
    else:
        entry_path = engine_root / entrypoint

    if project_only:
        # Add a project to an EXISTING (usually global) install: only the local config +
        # digest; the engine, block, hooks and command are already in place and untouched.
        print(f"add project to existing install -> graph {graph_agent_dir / 'amg'} (engine untouched)")
    else:
        print(f"install AMG ({scope}, env={env}) -> engine {engine_agent_dir}, "
              f"graph {graph_agent_dir / 'amg'}")
        place_engine(engine_agent_dir, agent_dir, entrypoint, scope, env)
        print(f"  engine  amg-* skills/{' + agents/' if prof.subagents != 'codex-toml' else ''} "
              f"-> {engine_agent_dir} (other skills kept)")

        # The activation block, hooks, /amg command, and event plugin are all named by
        # the environment profile; from here on the flow is env-agnostic executors.
        block = render_control_text(_block_body((REPO / "entrypoint" / prof.block).read_text(
            encoding="utf-8")), agent_dir, entrypoint, scope)
        inject_block(entry_path, block)
        print(f"  block   {entrypoint} ({prof.label}) -> {entry_path}")

        if prof.hooks_template and prof.hooks_dest:
            hooks_tpl = render_control_text(
                (REPO / "entrypoint" / prof.hooks_template).read_text(encoding="utf-8"),
                agent_dir, entrypoint, scope)
            hooks_path = engine_root / prof.hooks_dest.format(agent_dir=agent_dir)
            merge_settings(hooks_path, json.loads(hooks_tpl))
            print(f"  hooks   merged -> {hooks_path}")
        cmd_path = render_command(engine_root, prof, agent_dir, entrypoint, scope)
        if cmd_path:
            print(f"  command /amg -> {cmd_path}")
        plug_path = render_plugin(engine_root, prof, agent_dir, entrypoint, scope)
        if plug_path:
            print(f"  plugin  {plug_path} (event-driven session upkeep; loaded by the "
                  "environment at startup)")
        if prof.note:
            print(prof.note.format(agent_dir=agent_dir))

    # Config layers. A GLOBAL install (and --project-only, which
    # belongs to one) also maintains the machine-wide defaults config
    # ~/<agent_dir>/amg/config.yml — the PERSONAL layer (models tiering, embeddings)
    # every project's local config inherits per key. The local config then OMITS those
    # blocks so the inheritance actually shows through (a full local template would
    # shadow the global layer on every key). A local-scope install stays self-contained:
    # full local template, no global file.
    global_layer = scope == "global" or project_only
    if global_layer:
        wrote_global = write_global_config(Path.home() / agent_dir, set_global)
        gpath = Path.home() / agent_dir / "amg" / "config.yml"
        print(f"  global  {gpath} "
              f"({'written (machine-wide defaults)' if wrote_global else 'kept existing'})")
    elif set_global:
        print("  global  --set-global ignored for a local-scope install "
              "(the global layer exists for --scope global / --project-only; use --set)")
    strip = global_layer and (Path.home() / agent_dir / "amg" / "config.yml").exists()

    cfg_path = graph_agent_dir / "amg" / "config.yml"
    updated = write_config(graph_agent_dir / "amg", agent_dir, entrypoint,
                           mirror, absorb, absorb_once, exclude, scalars,
                           strip_personal=strip)
    if updated is None:
        print(f"  config  {cfg_path} (written)")
    else:
        kept = "kept existing" + (f"; updated keys: {', '.join(updated)}" if updated else "")
        print(f"  config  {cfg_path} ({kept})")
        summary = _existing_config_summary(cfg_path)
        if summary:
            # Show what stays in force, so the flow confirms instead of silently
            # keeping unknown state. Explicit flags passed on this run were applied
            # above; everything else: edit config.yml, or delete it and reinstall.
            print(f"          in force: {summary}")
            print("          (explicit --set/--mirror/... flags apply to it; the rest: "
                  "edit config.yml, or delete it and reinstall)")
    seed_digest(graph_agent_dir / "amg")

    # Render the models block into the profile's subagent format: "codex-toml" ->
    # TOML subagents (model + model_reasoning_effort); every markdown profile ->
    # per-role model/effort into the installed agents/*.md frontmatter (this runs for
    # guidance copies too: frontmatter diverging from config.yml `models` misleads —
    # a field failure), plus the native renders a profile declares. The models are
    # read as the loaders see them: global defaults under the local config.
    if not project_only:
        if yaml is None:
            print("  models  skipped (PyYAML not importable; reinstall after pip install pyyaml)")
        elif prof.subagents == "codex-toml" and prof.native_agents:
            render_codex_agents(REPO / "agents", engine_root / prof.native_agents[0],
                                _read_models(cfg_path, agent_dir), env)
        else:
            models = _read_models(cfg_path, agent_dir)
            render_agent_models(engine_agent_dir / "agents", models, env)
            if prof.subagents == "copies+opencode" and prof.native_agents:
                render_opencode_agents(REPO / "agents",
                                       engine_root / prof.native_agents[0], models)

    if deps:
        install_deps(deps)
    if verify:
        rc = verify_store(engine_agent_dir, graph_agent_dir)
        print(f"  verify  store {'clean' if rc == 0 else 'reported issues (see above)'}")
    if build:
        build_graph(engine_agent_dir, target, graph_agent_dir)

    if build:
        print("done. STRUCTURAL skeleton built (deterministic, no model — that is all "
              "--build does). The semantic layer (summaries + edges) still needs the "
              "model: restart the session, then `/amg sync` (or the first task under "
              "automation) finishes it.")
    elif scalars.get("active", "true").lower() in ("true", "on", "yes"):
        print("done. AMG is ACTIVE but the graph is NOT built yet — `/amg on` only sets the "
              "flag. It builds on your first task in a NEW session, or now via `/amg sync` "
              "(or re-run install with --build for the instant structural skeleton).")
    else:
        print("done. AMG installed but inactive — `/amg on` to activate, then `/amg sync`.")


def uninstall(target: Path, agent_dir: str, entrypoint: str,
              scope: str, purge_graph: bool) -> None:
    target = target.resolve()
    engine_root = Path.home() if scope == "global" else target
    engine_agent_dir = engine_root / agent_dir
    entry_path = engine_root / entrypoint

    # 1. strip the activation block, keep the user's content. Under --scope global
    # every profile's user-level entry is swept too (plus the legacy ~/<entrypoint>
    # spot older installs used) — same rule as the artifacts below: removal must not
    # depend on remembering which mode installed the block.
    # A "{agent_dir}" placeholder is expanded BOTH with each profile's own preset
    # and with the agent dir passed to this run: the sweep must reach ~/.qwen/QWEN.md
    # even when the uninstall was invoked as --env opencode, and a custom --agent-dir
    # must still be honored for the current environment.
    entry_candidates = [engine_root / entrypoint]
    if scope == "global":
        entry_candidates += [engine_root / p.global_entry.format(agent_dir=ad)
                             for p in ENVS.values() if p.global_entry
                             for ad in (p.agent_dir, agent_dir)]
    for entry_path in dict.fromkeys(entry_candidates):
        if not entry_path.exists():
            continue
        text = entry_path.read_text(encoding="utf-8")
        if BEGIN not in text:
            continue
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.S)
        entry_path.write_text(new.rstrip() + "\n" if new.strip() else "", encoding="utf-8")
        print(f"  block   removed from {entry_path}")
    # 2. remove only AMG skills/agents (the dir may hold the user's other skills),
    # plus every profile's native artifacts — an uninstall must not depend on
    # remembering which --env installed them, so ALL profiles' destinations are
    # swept (a missing one is simply absent).
    for sk in SKILL_NAMES:
        shutil.rmtree(engine_agent_dir / "skills" / sk, ignore_errors=True)
    for ag in (engine_agent_dir / "agents").glob("amg-*.md") if (engine_agent_dir / "agents").is_dir() else []:
        ag.unlink()
    for prof in ENVS.values():
        if prof.native_agents:
            nd = engine_root / prof.native_agents[0]
            if nd.is_dir():
                for f in nd.glob(prof.native_agents[1]):
                    f.unlink()
        for rel in (prof.command_dest, prof.plugin_dest):
            if rel:
                for ad in (prof.agent_dir, agent_dir):
                    p = engine_root / rel.format(agent_dir=ad)
                    if p.exists():
                        p.unlink()
    print(f"  engine  amg-* skills/agents/commands removed from {engine_agent_dir}")
    # 3. drop AMG hooks (matched by the lifecycle.py signature) from every profile's
    # hooks carrier, keeping foreign entries
    hook_files = {engine_root / prof.hooks_dest.format(agent_dir=ad)
                  for prof in ENVS.values() if prof.hooks_dest
                  for ad in (prof.agent_dir, agent_dir)}
    for settings in sorted(hook_files):
        if not settings.exists():
            continue
        try:
            obj = json.loads(settings.read_text(encoding="utf-8"))
            for event, entries in (obj.get("hooks") or {}).items():
                obj["hooks"][event] = [e for e in entries if not _is_amg_hook(e)]
            settings.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
            print(f"  hooks   AMG hooks removed from {settings}")
        except (OSError, ValueError):
            pass
    # 4. the local graph is preserved unless explicitly purged
    graph = target / agent_dir / "amg"
    if purge_graph and graph.exists():
        shutil.rmtree(graph, ignore_errors=True)
        print(f"  graph   purged {graph}")
    else:
        print(f"  graph   kept {graph} (pass --purge-graph to remove)")
    # 5. the machine-wide defaults config (the global layer) is the user's
    # preference data, like the graph: kept, never auto-removed.
    gcfg = Path.home() / agent_dir / "amg" / "config.yml"
    if scope == "global" and gcfg.exists():
        print(f"  global  kept {gcfg} (machine-wide defaults; delete by hand if unwanted)")
    print("done (uninstall).")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse(argv: List[str]) -> dict:
    args = {"target": None, "scope": "local", "agent_dir": None, "entrypoint": None,
            "env": "claude-code", "mirror": [], "absorb": [], "absorb_once": [],
            "exclude": [], "scalars": {}, "set_global": {},
            "deps": [], "verify": True, "build": False, "project_only": False,
            "uninstall": False, "purge_graph": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--target":
            args["target"] = argv[i + 1]; i += 2
        elif a == "--scope":
            args["scope"] = argv[i + 1]; i += 2
        elif a == "--agent-dir":
            args["agent_dir"] = argv[i + 1]; i += 2
        elif a == "--entrypoint":
            args["entrypoint"] = argv[i + 1]; i += 2
        elif a == "--env":
            args["env"] = argv[i + 1]; i += 2
        elif a in ("--mirror", "--absorb", "--exclude"):
            args[a[2:]] = [s for s in argv[i + 1].split(",") if s.strip()]; i += 2
        elif a == "--absorb-once":               # one-shot frozen sources (absorb_once_path)
            args["absorb_once"] = [s for s in argv[i + 1].split(",") if s.strip()]; i += 2
        elif a == "--set":                       # repeatable: --set key=value (dots = nested)
            k, _, v = argv[i + 1].partition("=")
            args["scalars"][k.strip()] = v.strip(); i += 2
        elif a == "--set-global":                # repeatable: into the GLOBAL defaults config
            k, _, v = argv[i + 1].partition("=")
            args["set_global"][k.strip()] = v.strip(); i += 2
        elif a == "--deps":
            args["deps"] = [s for s in argv[i + 1].split(",") if s.strip()]; i += 2
        elif a == "--no-verify":
            args["verify"] = False; i += 1
        elif a == "--build":
            args["build"] = True; i += 1
        elif a == "--project-only":
            args["project_only"] = True; i += 1
        elif a == "--uninstall":
            args["uninstall"] = True; i += 1
        elif a == "--purge-graph":
            args["purge_graph"] = True; i += 1
        else:
            i += 1
    return args


def main(argv: List[str]) -> int:
    if not argv or "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    a = _parse(argv)
    if not a["target"]:
        print("install.py: --target <project> is required.\n", __doc__)
        return 2
    target = Path(a["target"])
    # agent_dir / entrypoint default to the environment profile's convention when not
    # given explicitly (Claude Code -> .claude / CLAUDE.md; Qwen Code -> .qwen /
    # QWEN.md; the rest -> .agents / AGENTS.md, the cross-tool skills location).
    prof = ENVS[_env_kind(a["env"])]
    agent_dir = a["agent_dir"] or prof.agent_dir
    entrypoint = a["entrypoint"] or prof.entrypoint
    if a["uninstall"]:
        uninstall(target, agent_dir, entrypoint, a["scope"], a["purge_graph"])
        return 0
    install(target, a["scope"], agent_dir, entrypoint,
            a["mirror"], a["absorb"], a["absorb_once"], a["exclude"], a["scalars"],
            a["set_global"], a["deps"], a["verify"],
            build=a["build"], project_only=a["project_only"], env=a["env"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
