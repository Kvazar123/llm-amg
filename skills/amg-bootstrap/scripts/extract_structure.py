#!/usr/bin/env python3
"""
extract_structure.py - deterministic source -> structural units. (Stage 1: unified input)

The graph engine is domain-blind: it stores nodes/edges/text and does not care whether
a unit came from code, prose, or data. Only THIS file is type-aware. It is the "sensory
cortex" of the system: it routes each file to the right chunker by modality, then the
single associative graph is built from the result.

Pipeline per file:
  1. ignore?      built-in defaults (caches, deps, binaries) + the repo's .gitignore
  2. classify     by extension, then a content sniff for the ambiguous/extensionless
  3. chunk        a registry maps (chunker -> how to split into content-hashed units):
       python      stdlib `ast`  -> module + functions/classes/methods, + imports + CALLS
       <lang>code  tree-sitter   -> functions/classes + calls   (OPTIONAL; see below)
                   if tree-sitter or the grammar is unavailable -> one unit per file
       markdown    headings      -> one unit per section
       text        paragraphs    -> one unit per blank-line block
       json/yaml   records       -> one unit per top-level entry
  4. tag          each unit carries `category` (code|doc|data) -> physical node bucket,
                  and `policy` (mirror|absorb) inherited from its source.

tree-sitter is OPTIONAL. Python is handled fully by the stdlib `ast`, with NO dependency.
Other languages get function-level granularity + call edges ONLY if
`tree-sitter-language-pack` is importable and the grammar loads; otherwise the file
becomes a single unit (coarser, but never an error). Install it to get the most:
    pip install tree-sitter tree-sitter-language-pack

The content hash is what makes the whole system idempotent: reconcile.py compares these
hashes to the graph to decide what changed.

CLI:
    python extract_structure.py <project_root> [<amg_root>]            # dump units JSON
    python extract_structure.py <project_root> [<amg_root>] --stats     # human summary
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yaml
except ImportError:                       # pragma: no cover
    sys.stderr.write("extract_structure.py needs PyYAML: pip install pyyaml\n")
    raise

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# --------------------------------------------------------------------------- #
# Classification tables
# --------------------------------------------------------------------------- #

# Directories never indexed (hygiene, not meaning): a cheap pre-semantic filter,
# like sensory gating. The repo's .gitignore is honoured on top of this.
DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".claude", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", "out", ".next", ".nuxt", ".cache", "vendor",
    "site-packages", ".idea", ".vscode", ".gradle", "coverage", ".terraform",
    "bin", "obj",
}

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff",
    ".doc", ".xls", ".pptx", ".ppt",        # legacy/other office formats: not extracted
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".jar", ".war",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo", ".class", ".o", ".a",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac", ".ogg", ".webm",
    ".db", ".sqlite", ".sqlite3", ".pdb", ".lock", ".svg",
}

# extension -> tree-sitter grammar name (for non-Python code)
CODE_LANG_BY_EXT = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".php": "php", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".java": "java", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".cs": "c_sharp", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".lua": "lua", ".sh": "bash", ".bash": "bash", ".sql": "sql",
    ".pl": "perl", ".r": "r", ".dart": "dart", ".ex": "elixir", ".exs": "elixir",
}

PROSE_EXT = {".md": "headings", ".mdx": "headings", ".markdown": "headings",
             ".rst": "headings", ".txt": "paragraphs", ".text": "paragraphs"}

DATA_EXT = {".json": "json", ".ndjson": "json", ".yaml": "json", ".yml": "json"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Config + source resolution (mirror_path / absorb_path, str or list)
# --------------------------------------------------------------------------- #

def load_config(amg_root: Path) -> dict:
    return yaml.safe_load((amg_root / "config.yml").read_text(encoding="utf-8")) or {}


def _as_list(val) -> List[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return [str(v) for v in val]


def resolve_sources(config: dict) -> List[Tuple[str, str]]:
    """Return [(path, policy)]. Prefers mirror_path/absorb_path; falls back to a
    legacy `sources:` dict so existing configs keep working."""
    out: List[Tuple[str, str]] = []
    if "mirror_path" in config or "absorb_path" in config:
        out += [(p, "mirror") for p in _as_list(config.get("mirror_path"))]
        out += [(p, "absorb") for p in _as_list(config.get("absorb_path"))]
        return out
    # legacy form: sources: {name: {path, policy}}
    for _, src in (config.get("sources") or {}).items():
        if isinstance(src, dict) and src.get("path"):
            out.append((src["path"], src.get("policy", "mirror")))
    return out


# --------------------------------------------------------------------------- #
# Ignore (.gitignore + defaults) and file walking
# --------------------------------------------------------------------------- #

def load_gitignore(project_root: Path) -> List[str]:
    f = project_root / ".gitignore"
    if not f.exists():
        return []
    pats = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("!"):
            pats.append(line.rstrip("/"))
    return pats


def _gitignored(rel: str, patterns: List[str]) -> bool:
    base = rel.split("/")[-1]
    segs = rel.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat):
            return True
        if pat in segs:                       # a directory name anywhere in the path
            return True
        if fnmatch.fnmatch(rel, pat + "/*"):
            return True
    return False


def iter_source_files(project_root: Path, base_rel: str,
                      extra_excludes: List[str], gitignore: List[str]) -> Iterable[Path]:
    base = (project_root / base_rel).resolve()
    if not base.exists():
        return
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if any(part in DEFAULT_IGNORE_DIRS for part in p.parts):
            continue
        rel = p.relative_to(project_root).as_posix()
        if _gitignored(rel, gitignore) or _gitignored(rel, extra_excludes):
            continue
        yield p


# --------------------------------------------------------------------------- #
# Classifier: (category, chunker, lang, ambiguous)
# --------------------------------------------------------------------------- #

def classify(path: Path) -> Tuple[str, str, Optional[str], bool]:
    ext = path.suffix.lower()
    if ext in BINARY_EXT:
        return ("binary", "skip", None, False)
    if ext == ".py":
        return ("code", "python", "python", False)
    if ext in CODE_LANG_BY_EXT:
        return ("code", "treesitter", CODE_LANG_BY_EXT[ext], False)
    if ext in PROSE_EXT:
        return ("doc", PROSE_EXT[ext], None, False)
    if ext in DATA_EXT:
        return ("data", "json", None, False)
    if ext == ".pdf":
        return ("doc", "pdf", "pdf", False)
    if ext == ".docx":
        return ("doc", "docx", "docx", False)
    if ext in (".xlsx", ".xlsm"):
        return ("data", "xlsx", "xlsx", False)
    # extensionless or unknown extension -> sniff content
    try:
        head = path.read_bytes()[:2048]
    except OSError:
        return ("binary", "skip", None, False)
    if b"\x00" in head:
        return ("binary", "skip", None, False)
    # readable text of unknown kind: treat as prose, flag as ambiguous so the
    # bootstrap skill may ask the classifier subagent to refine it.
    return ("doc", "paragraphs", None, True)


# --------------------------------------------------------------------------- #
# Classifier overrides: amg-classifier's verdict made effective in code (1.13)
# --------------------------------------------------------------------------- #

# Categories an override may assign (the amg-classifier subagent picks one of these).
_OVERRIDE_CATEGORIES = {"code", "doc", "data"}


def load_overrides(amg_root: Optional[Path]) -> Dict[str, dict]:
    """Read the amg-classifier category overrides for ambiguous files (audit 1.13).

    Format: { "<rel/path>": {"category": code|doc|data, "language": <grammar|null>} }.
    The bootstrap workflow writes this to work/classification-overrides.json after the
    amg-classifier subagent labels the files extract flagged ambiguous. A missing file
    -> {} (pure deterministic classification); a malformed file warns and is ignored, so
    a bad override never blocks bootstrap.
    """
    if amg_root is None:
        return {}
    f = amg_root / "work" / "classification-overrides.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.stderr.write(f"warning: ignoring malformed {f}\n")
        return {}
    return data if isinstance(data, dict) else {}


def _route_override(category: str, language: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """Map an amg-classifier override (category + optional grammar name) onto the
    (category, chunker, lang) the chunker registry expects: code+python -> the stdlib
    `ast` chunker; code+grammar -> tree-sitter (a null grammar degrades to a single file
    unit, exactly like any unavailable grammar); data -> the json/yaml record chunker;
    doc -> the prose paragraph chunker (the same safe default the content sniff uses — a
    'use headings' hint is not expressible, so structured markdown should keep its .md)."""
    if category == "code":
        if language == "python":
            return ("code", "python", "python")
        return ("code", "treesitter", language)
    if category == "data":
        return ("data", "json", None)
    return ("doc", "paragraphs", None)


def _classify_path(path: Path, rel: str, overrides: Dict[str, dict]
                   ) -> Tuple[str, str, Optional[str], bool, bool]:
    """classify(), but an explicit override for this rel-path wins over the
    deterministic guess (applied BEFORE the content sniff, so it routes the file
    straight to the chosen chunker). Returns classify()'s 4-tuple plus a final
    `overridden` flag; an override resolves ambiguity, so its `ambiguous` is False."""
    ov = overrides.get(rel)
    if isinstance(ov, dict) and ov.get("category") in _OVERRIDE_CATEGORIES:
        category, chunker, lang = _route_override(ov["category"], ov.get("language"))
        return (category, chunker, lang, False, True)
    category, chunker, lang, amb = classify(path)
    return (category, chunker, lang, amb, False)


# --------------------------------------------------------------------------- #
# Chunkers
# --------------------------------------------------------------------------- #

def _file_unit(rel: str, category: str, policy: str, text: str, lang: Optional[str] = None) -> dict:
    return {"id": f"{category}:{rel}", "kind": "file", "source_path": rel,
            "category": category, "policy": policy, "qualname": "", "lineno": 1,
            "lang": lang, "content_sha": _sha(text)}


def _python_units(path: Path, rel: str, policy: str) -> List[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [_file_unit(rel, "code", policy, text, "python")]

    imports: List[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module:
            imports.append(n.module)

    units = [{"id": f"code:{rel}", "kind": "module", "source_path": rel,
              "category": "code", "policy": policy, "qualname": "", "lineno": 1,
              "lang": "python", "content_sha": _sha(text),
              "imports": sorted(set(imports))}]

    def calls_in(node) -> List[str]:
        names = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    names.append(f.id)
                elif isinstance(f, ast.Attribute):
                    names.append(f.attr)
        return sorted(set(names))

    def slice_src(node) -> str:
        return "".join(lines[node.lineno - 1: getattr(node, "end_lineno", node.lineno)])

    def walk(node, prefix: str):
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}{child.name}"
                units.append({
                    "id": f"code:{rel}::{qual}",
                    "kind": "class" if isinstance(child, ast.ClassDef) else "function",
                    "source_path": rel, "category": "code", "policy": policy,
                    "qualname": qual, "lineno": child.lineno, "lang": "python",
                    "content_sha": _sha(slice_src(child)), "calls": calls_in(child)})
                if isinstance(child, ast.ClassDef):
                    walk(child, prefix=f"{qual}.")

    walk(tree, prefix="")
    return units


# tree-sitter node-type families (vary by grammar; matched broadly), mapped to
# the canonical node types so non-Python code gets the same retrieval tiers and
# path:line pointers as Python (roadmap 1.25; stage 1, task 8). Type-level
# containers (struct/impl/trait/interface/enum) canonicalize to `class`.
_TS_DEF = {
    "function_definition": "function", "function_declaration": "function",
    "method_definition": "function", "method_declaration": "function",
    "function_item": "function", "constructor_declaration": "function",
    "class_definition": "class", "class_declaration": "class",
    "class_specifier": "class", "struct_specifier": "class", "struct_item": "class",
    "impl_item": "class", "trait_item": "class",
    "interface_declaration": "class", "enum_declaration": "class",
}
_TS_CALL = {"call", "call_expression", "function_call_expression",
            "method_invocation", "invocation_expression"}


def _treesitter_units(path: Path, rel: str, policy: str, lang: str) -> Optional[List[dict]]:
    """Function/class granularity + calls via tree-sitter. Returns None to signal
    'unavailable -> fall back to a single file unit'.

    Two binding generations ship under the same package name: the classic pack
    mirrors py-tree-sitter (parse(bytes); node.type/.children/.text properties;
    start_point), the alef rewrite (>= 1.8) exposes a method-based API
    (parse(str); node.kind()/child(i); byte offsets; start_position()). The
    grammar node-type strings are identical, so only access is feature-detected.
    """
    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(lang)
    except Exception:
        return None
    try:
        data = path.read_bytes()
        try:
            tree = parser.parse(data)
        except TypeError:                     # alef binding wants str, not bytes
            tree = parser.parse(data.decode("utf-8", "replace"))
    except Exception:
        return None

    def _call(v):
        return v() if callable(v) else v

    def kind_of(node) -> str:
        t = getattr(node, "type", None)
        return t if isinstance(t, str) else _call(node.kind)

    def children_of(node) -> list:
        ch = getattr(node, "children", None)
        if isinstance(ch, list):
            return ch
        return [node.child(i) for i in range(_call(node.child_count))]

    def text_of(node) -> str:
        raw = getattr(node, "text", None)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return data[_call(node.start_byte):_call(node.end_byte)].decode("utf-8", "replace")

    def line_of(node) -> int:
        pt = _call(node.start_point if hasattr(node, "start_point")
                   else node.start_position)
        return (pt[0] if isinstance(pt, tuple) else _call(pt.row)) + 1

    text = data.decode("utf-8", "replace")
    units = [_file_unit(rel, "code", policy, text, lang)]
    units[0]["kind"] = "module"

    def name_of(node) -> Optional[str]:
        n = node.child_by_field_name("name")
        if n is not None:
            return text_of(n)
        for c in children_of(node):
            if kind_of(c) in ("name", "identifier", "type_identifier", "field_identifier"):
                return text_of(c)
        return None

    def calls_in(node) -> List[str]:
        out = []
        stack = [node]
        while stack:
            n = stack.pop()
            ch = children_of(n)
            if kind_of(n) in _TS_CALL:
                f = n.child_by_field_name("function")
                if f is None and ch:
                    f = ch[0]
                if f is not None:
                    t = text_of(f)
                    out.append(t.replace("(", " ").split()[0].split(".")[-1].split("::")[-1])
            stack.extend(ch)
        return sorted({c for c in out if c.isidentifier()})

    def walk(node):
        for child in children_of(node):
            k = kind_of(child)
            if k in _TS_DEF:
                nm = name_of(child)
                if nm:
                    src = data[_call(child.start_byte):_call(child.end_byte)] \
                        .decode("utf-8", "replace")
                    units.append({
                        "id": f"code:{rel}::{nm}", "kind": _TS_DEF[k],
                        "source_path": rel,
                        "category": "code", "policy": policy, "qualname": nm,
                        "lineno": line_of(child), "lang": lang,
                        "content_sha": _sha(src), "calls": calls_in(child)})
            walk(child)

    root = tree.root_node
    walk(_call(root))
    return units


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# Code-fence delimiter (CommonMark minimum): ``` or ~~~, 3+ chars, indented up
# to 3 spaces. A closing fence is the same character, at least as long.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _slug(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title.strip().lower())
    return re.sub(r"[\s-]+", "-", s)[:60] or "section"


def _markdown_units(path: Path, rel: str, policy: str) -> List[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    sections, cur_title, cur_start = [], None, 0
    fence = None                 # opening fence marker while inside a code block
    for i, line in enumerate(lines):
        raw = line.rstrip("\n")
        fmatch = _FENCE.match(raw)
        if fmatch:
            marker = fmatch.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None     # a foreign marker inside a block closes nothing
            continue
        if fence is not None:
            continue             # inside a fence: '# ...' is code, not a heading
        m = _HEADING.match(raw)
        if m:
            if cur_title is not None or i > 0:
                sections.append((cur_title, cur_start, i))
            cur_title, cur_start = m.group(2), i
    sections.append((cur_title, cur_start, len(lines)))

    units, seen = [], {}
    for title, start, end in sections:
        chunk = "".join(lines[start:end]).strip()
        if not chunk:
            continue
        if title is None:
            qual = "_preamble"
        else:
            base = _slug(title)
            n = seen.get(base, 0)
            seen[base] = n + 1
            qual = base if n == 0 else f"{base}-{n}"
        units.append({"id": f"doc:{rel}::{qual}", "kind": "section", "source_path": rel,
                      "category": "doc", "policy": policy, "qualname": qual,
                      "lineno": start + 1, "lang": "markdown", "content_sha": _sha(chunk)})
    return units or [_file_unit(rel, "doc", policy, text, "markdown")]


def _text_units(path: Path, rel: str, policy: str) -> List[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks, idx, n = [], 0, 0
    for raw in re.split(r"\n\s*\n", text):
        block = raw.strip()
        if not block:
            continue
        n += 1
        blocks.append({"id": f"doc:{rel}::b{n}", "kind": "block", "source_path": rel,
                       "category": "doc", "policy": policy, "qualname": f"b{n}",
                       "lineno": 1, "lang": "text", "content_sha": _sha(block)})
    return blocks or [_file_unit(rel, "doc", policy, text, "text")]


def _data_units(path: Path, rel: str, policy: str) -> List[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        obj = yaml.safe_load(text)            # parses JSON and YAML
    except Exception:
        return [_file_unit(rel, "data", policy, text)]
    units = []
    if isinstance(obj, dict):
        items = list(obj.items())
    elif isinstance(obj, list):
        items = list(enumerate(obj))
    else:
        return [_file_unit(rel, "data", policy, text)]
    for key, val in items[:500]:
        frag = json.dumps({str(key): val}, ensure_ascii=False, sort_keys=True)
        units.append({"id": f"data:{rel}::{str(key)[:48]}", "kind": "record",
                      "source_path": rel, "category": "data", "policy": policy,
                      "qualname": str(key)[:48], "lineno": 1, "lang": "json",
                      "content_sha": _sha(frag)})
    return units or [_file_unit(rel, "data", policy, text)]


# --- captured sessions (Stage 9) ---------------------------------------------
# The dump writer (lifecycle.py) and this chunker SHARE the role markers below — the
# format contract; lifecycle imports these helpers so the two never drift apart.
_SESSION_ROLE_RE = re.compile(r"^=== (Human|Assistant) ===\s*$")


def session_role_marker(role: str) -> str:
    """Turn delimiter (`=== Human ===` / `=== Assistant ===`) shared by the session
    dump writer and the session chunker."""
    return f"=== {role} ==="


def session_attachment_marker(n: int, label: str = "") -> str:
    """One marker per omitted attachment (tool call/result, image, file), numbered
    sequentially — so several attachments in one message stay distinct (e.g. a chat
    export where a user turn carries many files), not collapsed into a count. Writer-
    only in practice: the chunker keeps it inside the turn (it splits on role markers,
    not these)."""
    return f"== Attachment {n}: {label} ==" if label else f"== Attachment {n} =="


def _session_units(path: Path, rel: str, policy: str) -> List[dict]:
    """One unit per conversation turn in a captured session dump. Turns are split on
    the role markers the dump writer emits; the leading frontmatter (everything before
    the first marker) is skipped. Each turn is a `section` so it is episodic — a long
    chat is ingested at conversational granularity and consolidation can summarize /
    compact piled-up sessions (THEORY §13). Falls back to one file unit if no markers
    are present (e.g. a hand-dropped chat export the writer did not format)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if _SESSION_ROLE_RE.match(ln.rstrip("\n"))]
    if not starts:
        return [_file_unit(rel, "doc", policy, text, "session")]
    bounds = starts + [len(lines)]
    units = []
    for n, (s, e) in enumerate(zip(bounds, bounds[1:]), 1):
        chunk = "".join(lines[s:e]).strip()
        if not chunk:
            continue
        units.append({"id": f"doc:{rel}::m{n}", "kind": "section", "source_path": rel,
                      "category": "doc", "policy": policy, "qualname": f"m{n}",
                      "lineno": s + 1, "lang": "session", "content_sha": _sha(chunk)})
    return units or [_file_unit(rel, "doc", policy, text, "session")]


# --- binary document formats (optional pure-Python libs; graceful skip) ------
# Each returns units that CARRY the extracted `text`, so the builder can summarize
# without re-opening the binary. Returns None when the library is absent or the
# file can't be read (e.g. a scanned PDF with no text layer) -> the file is skipped.

def _pdf_units(path: Path, rel: str, policy: str) -> Optional[List[dict]]:
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        reader = PdfReader(str(path))
    except Exception:
        return None
    units = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if not text:
            continue
        units.append({"id": f"doc:{rel}::p{i}", "kind": "page", "source_path": rel,
                      "category": "doc", "policy": policy, "qualname": f"p{i}",
                      "lineno": 1, "lang": "pdf", "content_sha": _sha(text), "text": text})
    return units or None          # no extractable text (scanned?) -> skip


def _docx_units(path: Path, rel: str, policy: str) -> Optional[List[dict]]:
    try:
        import docx                                  # python-docx
    except Exception:
        return None
    try:
        document = docx.Document(str(path))
    except Exception:
        return None
    sections, title, body = [], None, []             # chunk by heading, like markdown
    for para in document.paragraphs:
        txt = para.text.strip()
        style = (para.style.name or "") if para.style else ""
        if style.lower().startswith("heading") or style.lower() == "title":
            if title is not None or body:
                sections.append((title, body))
            title, body = txt, []
        elif txt:
            body.append(txt)
    sections.append((title, body))

    units, seen = [], {}
    for title, paras in sections:
        chunk = "\n".join(([title] if title else []) + paras).strip()
        if not chunk:
            continue
        base = _slug(title) if title else "_preamble"
        n = seen.get(base, 0)
        seen[base] = n + 1
        qual = base if n == 0 else f"{base}-{n}"
        units.append({"id": f"doc:{rel}::{qual}", "kind": "section", "source_path": rel,
                      "category": "doc", "policy": policy, "qualname": qual,
                      "lineno": 1, "lang": "docx", "content_sha": _sha(chunk), "text": chunk})
    return units or None


def _xlsx_units(path: Path, rel: str, policy: str) -> Optional[List[dict]]:
    """One unit per SHEET. A spreadsheet is data, not prose, so the stored text is a
    STRUCTURAL description (sheet name, size, column headers, a few sample rows),
    not a dump of every cell."""
    try:
        import openpyxl
    except Exception:
        return None
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception:
        return None
    units = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None) or ()
        cols = ", ".join(str(h) for h in header if h is not None)
        sample = []
        for r in rows:
            sample.append(" | ".join("" if v is None else str(v) for v in r))
            if len(sample) >= 3:
                break
        desc = (f"Spreadsheet sheet '{ws.title}': {ws.max_row or 0} rows x "
                f"{ws.max_column or 0} columns.\nColumns: {cols}\n"
                f"Sample rows:\n" + "\n".join(sample))
        units.append({"id": f"data:{rel}::{ws.title}", "kind": "sheet", "source_path": rel,
                      "category": "data", "policy": policy, "qualname": ws.title,
                      "lineno": 1, "lang": "xlsx", "content_sha": _sha(desc), "text": desc})
    wb.close()
    return units or None


CHUNKERS = {"python": _python_units, "treesitter": _treesitter_units,
            "headings": _markdown_units, "paragraphs": _text_units, "json": _data_units,
            "pdf": _pdf_units, "docx": _docx_units, "xlsx": _xlsx_units,
            "session": _session_units}


# --------------------------------------------------------------------------- #
# Captured sessions as a source (Stage 9)
# --------------------------------------------------------------------------- #

def session_dir(project_root: Path, config: dict, amg_root: Optional[Path]) -> Optional[Path]:
    """Resolve the captured-sessions folder. `config['sessions']` is an optional
    project-relative override; by default it DERIVES as <store>/sessions from the
    resolved root, so it is correct under any agent dir (.claude / .agents / ...) with
    no installer dependency. Shared by the dump writer (lifecycle) and the chunker —
    one source of truth, so writer and reader can never diverge."""
    s = config.get("sessions")
    if s:
        return (project_root / str(s)).resolve()
    return (Path(amg_root).resolve() / "sessions") if amg_root else None


def _iter_session_files(base: Path) -> Iterable[Path]:
    """Yield files under the sessions dir, filtering only junk BELOW it. The dir lives
    inside the store (under the ignored agent dir), so — unlike iter_source_files — its
    prefix is NOT subject to DEFAULT_IGNORE_DIRS / .gitignore: it is an opted-in
    AMG-internal source. Without this, iter would silently drop every dump (audit 1.18)."""
    if not base.exists():
        return
    for p in base.rglob("*"):
        if p.is_file() and not any(part in DEFAULT_IGNORE_DIRS
                                   for part in p.relative_to(base).parts):
            yield p


def _extract_sessions(project_root: Path, config: dict,
                      amg_root: Optional[Path]) -> List[dict]:
    """Captured session dumps as an ordinary source, routed to the session chunker.
    Policy comes from `session_policy` (absorb by default)."""
    base = session_dir(project_root, config, amg_root)
    if base is None:
        return []
    policy = str(config.get("session_policy", "absorb"))
    out: List[dict] = []
    for p in _iter_session_files(base):
        try:
            rel = p.relative_to(project_root).as_posix()
        except ValueError:                # an override outside the project tree
            continue
        out += _session_units(p, rel, policy)
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def extract(project_root: Path, config: dict, amg_root: Optional[Path] = None) -> List[dict]:
    gitignore = load_gitignore(project_root)
    extra = _as_list(config.get("exclude"))
    overrides = load_overrides(amg_root)
    units: List[dict] = []
    for base_rel, policy in resolve_sources(config):
        for path in iter_source_files(project_root, base_rel, extra, gitignore):
            rel = path.relative_to(project_root).as_posix()
            category, chunker, lang, _amb, _ov = _classify_path(path, rel, overrides)
            if chunker == "skip":
                continue
            if chunker == "treesitter":
                got = _treesitter_units(path, rel, policy, lang)
                if got is None:                       # graceful degradation
                    got = [_file_unit(rel, "code", policy,
                                      path.read_text(encoding="utf-8", errors="replace"), lang)]
                units += got
            elif chunker in ("pdf", "docx", "xlsx"):
                got = CHUNKERS[chunker](path, rel, policy)
                if got is None:                       # lib missing / unreadable -> skip
                    continue
                units += got
            else:
                units += CHUNKERS[chunker](path, rel, policy)
    units += _extract_sessions(project_root, config, amg_root)   # opted-in store source
    return units


def _stats(project_root: Path, config: dict, amg_root: Optional[Path] = None) -> dict:
    gitignore = load_gitignore(project_root)
    extra = _as_list(config.get("exclude"))
    overrides = load_overrides(amg_root)
    from collections import Counter
    cat, langs, skipped, ambiguous, resolved = Counter(), Counter(), 0, [], []
    for base_rel, _policy in resolve_sources(config):
        for path in iter_source_files(project_root, base_rel, extra, gitignore):
            rel = path.relative_to(project_root).as_posix()
            category, chunker, lang, amb, overridden = _classify_path(path, rel, overrides)
            if chunker == "skip":
                skipped += 1
                continue
            cat[category] += 1
            langs[lang or chunker] += 1
            if overridden:
                resolved.append(rel)            # ambiguity settled by amg-classifier
            elif amb:
                ambiguous.append(rel)           # still unresolved -> prose default
    sessions = 0
    sess_base = session_dir(project_root, config, amg_root)
    if sess_base:
        for _p in _iter_session_files(sess_base):
            cat["doc"] += 1
            langs["session"] += 1
            sessions += 1
    try:
        from tree_sitter_language_pack import get_parser
        get_parser("json")                    # attempt a real grammar load
        ts = "available (function-level units + calls for all languages)"
    except Exception:
        ts = "unavailable (non-Python code -> one unit per file; Python unaffected)"

    import importlib
    extraction = {}
    for fmt, mod in (("pdf", "pypdf"), ("docx", "docx"), ("xlsx", "openpyxl")):
        try:
            importlib.import_module(mod)
            extraction[fmt] = f"available ({mod})"
        except Exception:
            extraction[fmt] = f"unavailable (pip install {'python-docx' if mod=='docx' else mod}) -> files skipped"

    out = {"by_category": dict(cat), "by_language": dict(langs),
           "skipped_binary": skipped, "sessions": sessions,
           "ambiguous_files": ambiguous[:20],
           "resolved_by_override": resolved[:20],
           "tree_sitter": ts, "text_extraction": extraction}
    if ambiguous:
        ov_path = ((amg_root / "work" / "classification-overrides.json").as_posix()
                   if amg_root else "work/classification-overrides.json")
        out["classifier_hint"] = (
            f"{len(ambiguous)} ambiguous file(s) defaulted to prose. Spawn the "
            f"amg-classifier subagent on ambiguous_files, write its "
            f"{{path: {{category, language}}}} mapping to {ov_path}, then re-run.")
    return out


def main(argv: List[str]) -> int:
    project_root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    rest = [a for a in argv[2:] if not a.startswith("--")]
    amg_root = Path(rest[0]).resolve() if rest else project_root / ".claude" / "amg"
    config = load_config(amg_root)
    if "--stats" in argv:
        print(json.dumps(_stats(project_root, config, amg_root), ensure_ascii=False, indent=2))
        return 0
    json.dump(extract(project_root, config, amg_root), sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
