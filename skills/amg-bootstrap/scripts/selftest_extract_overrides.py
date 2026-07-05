#!/usr/bin/env python3
"""
selftest_extract_overrides.py — proves the amg-classifier integration: the classifier's verdict is made effective in code.

Checks:
  1. route    : _route_override maps (category, language) onto the right chunker —
                code+python -> python ast, code+grammar -> tree-sitter, data ->
                json, doc -> paragraphs.
  2. load     : load_overrides reads work/classification-overrides.json; a missing
                file -> {}, a malformed file -> {} (ignored, never a crash).
  3. ambiguous: with NO override, an extensionless shell script sniffs to the prose
                default and is reported ambiguous (with a classifier hint).
  4. resolved : an override routes that same file to code BEFORE the fallback, so
                extract emits it in the code bucket and --stats lists it under
                resolved_by_override (not ambiguous_files).
  5. precedence: an override wins even over a KNOWN extension (a .txt forced to data).

Run:  python selftest_extract_overrides.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import extract_structure as ES

SHELL_SRC = "#!/usr/bin/env bash\nset -e\necho \"deploy the service\"\n"
PROSE_SRC = "Release notes\n\nThe team shipped the billing rework this week.\n"


def setup_project() -> Path:
    """A temp project: src/ holds an extensionless shell script (ambiguous), an
    extensionless prose file (ambiguous), and a .txt (a known doc extension)."""
    proj = Path(tempfile.mkdtemp(prefix="amg-ovr-"))
    (proj / "src").mkdir()
    (proj / "src" / "run").write_text(SHELL_SRC, encoding="utf-8")
    (proj / "src" / "CHANGELOG").write_text(PROSE_SRC, encoding="utf-8")
    (proj / "src" / "payload.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    amg = proj / ".claude" / "amg"
    (amg / "work").mkdir(parents=True)
    (amg / "config.yml").write_text("active: true\nworking_language: en\nmirror_path: src\n",
                                    encoding="utf-8")
    return proj


def _amg(proj: Path) -> Path:
    return proj / ".claude" / "amg"


def _unit_for(units, rel):
    return [u for u in units if u["source_path"] == rel]


def test_route():
    assert ES._route_override("code", "python") == ("code", "python", "python")
    assert ES._route_override("code", "bash") == ("code", "treesitter", "bash")
    assert ES._route_override("code", None) == ("code", "treesitter", None)
    assert ES._route_override("data", None) == ("data", "json", None)
    assert ES._route_override("doc", None) == ("doc", "paragraphs", None)
    print("PASS  route: override (category, language) maps to the right chunker")


def test_load(proj):
    amg = _amg(proj)
    assert ES.load_overrides(None) == {}, "no amg_root -> no overrides"
    assert ES.load_overrides(amg) == {}, "missing file -> {}"
    (amg / "work" / "classification-overrides.json").write_text("{ not json", encoding="utf-8")
    assert ES.load_overrides(amg) == {}, "malformed file -> {} (ignored, no crash)"
    (amg / "work" / "classification-overrides.json").unlink()
    print("PASS  load: missing/malformed overrides degrade to {} (never a crash)")


def test_ambiguous_then_resolved(proj):
    amg = _amg(proj)
    config = ES.load_config(amg)

    # (3) no override: the shell script and CHANGELOG sniff to prose, flagged ambiguous
    stats = ES._stats(proj, config, amg)
    assert "src/run" in stats["ambiguous_files"], stats
    assert "src/CHANGELOG" in stats["ambiguous_files"], stats
    assert stats["resolved_by_override"] == [], stats
    assert "classifier_hint" in stats and "amg-classifier" in stats["classifier_hint"], stats
    units = ES.extract(proj, config, amg)
    assert _unit_for(units, "src/run")[0]["category"] == "doc", "prose default before override"

    # (4) override: route src/run -> code/bash, CHANGELOG -> doc (confirm prose)
    (amg / "work" / "classification-overrides.json").write_text(json.dumps({
        "src/run": {"category": "code", "language": "bash"},
        "src/CHANGELOG": {"category": "doc", "language": None},
    }), encoding="utf-8")
    stats = ES._stats(proj, config, amg)
    assert "src/run" in stats["resolved_by_override"], stats
    assert "src/CHANGELOG" in stats["resolved_by_override"], stats
    assert "src/run" not in stats["ambiguous_files"], stats
    assert "classifier_hint" not in stats, "no hint once nothing is left ambiguous"
    units = ES.extract(proj, config, amg)
    run_units = _unit_for(units, "src/run")
    assert run_units and all(u["category"] == "code" for u in run_units), run_units
    print("PASS  resolved: an override routes an ambiguous file to its real chunker")


def test_precedence(proj):
    amg = _amg(proj)
    config = ES.load_config(amg)
    # a KNOWN extension (.txt -> doc/paragraphs) forced to data by an override
    (amg / "work" / "classification-overrides.json").write_text(json.dumps({
        "src/run": {"category": "code", "language": "bash"},
        "src/payload.txt": {"category": "data", "language": None},
    }), encoding="utf-8")
    units = ES.extract(proj, config, amg)
    pay = _unit_for(units, "src/payload.txt")
    assert pay and all(u["category"] == "data" for u in pay), pay
    print("PASS  precedence: an override wins even over a known extension")


if __name__ == "__main__":
    proj = setup_project()
    try:
        test_route()
        test_load(proj)
        test_ambiguous_then_resolved(proj)
        test_precedence(proj)
        print("\nALL OVERRIDE CHECKS PASSED")
    finally:
        shutil.rmtree(proj, ignore_errors=True)
