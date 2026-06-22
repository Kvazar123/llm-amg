#!/usr/bin/env python3
"""
export_graph.py — export the AMG graph to JSON for visual inspection (roadmap Stage 15).

READ-ONLY with respect to the graph: it scans nodes/*.md and emits a {meta, nodes,
links} document. Its only write is the output file (default under cache/, which is
disposable and rebuildable) — never a node, edge, or journal entry.

Unlike retrieve.load_nodes (which projects only the fields BM25/PPR need and drops
source_kind / policy / provenance / lang / tags / qualname / created / updated), the
export carries the FULL frontmatter of every node, because the viewer's side panel
shows it (Stage 15, task 4: "show frontmatter"). The cost — a one-shot full scan
instead of the disposable read-index — is irrelevant for an on-demand inspection tool
(the index exists to speed the PER-QUERY load, not a single export).

The same {meta, nodes, links} core feeds the SELF-CONTAINED HTML viewer: render_html
inlines the data, the vendored 3d-force-graph library, and the viewer glue into one
file that opens by double-click — no server, no network, read-only (the graph data is
embedded, not fetched, because a file:// page cannot fetch a sibling .json under CORS).

CLI:
    python export_graph.py                       # -> <store>/cache/graph.html (the viewer)
    python export_graph.py --open                # render the viewer and open it in a browser
    python export_graph.py --json                # -> <store>/cache/graph.json (data only)
    python export_graph.py --store <path> --json out.json --html out.html
    python export_graph.py --stdout              # print JSON to stdout

What it carries (from the data model, 02-data-model.md):
  * nodes: id, type, status, summary, bucket (the real nodes/<bucket>/ dir), group
    (cluster key — the heaviest part_of topic, else the bucket), degree (incident
    links), body, and the full `frontmatter` dict (source_path/lineno/line_end,
    source_kind, policy, confidence, provenance, verification, part_of, edges, lang,
    tags, created/updated, qualname, ...). Stage 14 statuses disputed/rejected ride
    along for the viewer to surface.
  * links: one per typed edge whose TARGET node exists (a dangling edge — an external
    import or an unresolved cross-file call — is dropped, exactly as retrieve does),
    plus one per part_of membership whose topic resolves to a real node id (a hub).
    rel / w / origin are kept so the viewer can color structural vs semantic vs
    conflict (contradicts/supersedes) edges. A part_of topic that is a directory
    string (not a node) is not a link — it is kept only as the node's cluster `group`.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml                                # store config.yml -> meta.viewer (engine dep)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retrieve as R                       # reuse the same store resolver + frontmatter parser

VIEWER_DIR = HERE / "viewer"              # vendored 3d-force-graph + template + glue
_DATA_MARK = "__AMG_DATA__"
_LIB_MARK = "/*__AMG_LIB__*/"
_VIEWER_MARK = "/*__AMG_VIEWER__*/"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass


def _scan_full(store: Path) -> Dict[str, Dict[str, Any]]:
    """Read every nodes/*.md and return id -> {meta, body, bucket}. The FULL frontmatter
    (not retrieve's projection), so the viewer can show every field. Mirrors
    retrieve._scan_nodes but keeps all meta keys; same FRONTMATTER_RE via R._parse."""
    out: Dict[str, Dict[str, Any]] = {}
    nodes_dir = store / "nodes"
    if not nodes_dir.exists():
        return out
    for p in sorted(nodes_dir.rglob("*.md")):
        parsed = R._parse(p.read_text(encoding="utf-8", errors="replace"))
        if not parsed:
            continue
        meta, body = parsed
        nid = meta.get("id")
        if not nid:
            continue
        rel = p.relative_to(nodes_dir).as_posix()          # <bucket>/<file>.md
        bucket = rel.split("/", 1)[0] if "/" in rel else ""
        out[str(nid)] = {"meta": meta, "body": body, "bucket": bucket}
    return out


def _group_of(meta: Dict[str, Any], bucket: str) -> str:
    """The node's cluster key: the heaviest part_of topic (be it a hub id or a directory
    string), falling back to the bucket. Drives large-graph clustering in the viewer."""
    best_topic: Optional[str] = None
    best_w = -1.0
    for m in meta.get("part_of") or []:
        if not isinstance(m, dict) or not m.get("topic"):
            continue
        try:
            w = float(m.get("w") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        if w > best_w:
            best_w, best_topic = w, str(m["topic"])
    return best_topic or bucket or "?"


def _viewer_cfg(store: Path) -> Dict[str, Any]:
    """The store's top-level `viewer:` config block (or {}). A thin friendly layer: a few
    AMG keys (quality / large_graph_nodes / min_edge_weight) plus a raw `options` map the
    viewer applies verbatim to 3d-force-graph — so config.yml need not enumerate the
    library's options. Carried in meta so the inlined HTML honors the user's settings."""
    f = store / "config.yml"
    if not f.exists():
        return {}
    try:
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    v = raw.get("viewer") if isinstance(raw, dict) else None
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _project_name(store: Path) -> str:
    """The project the graph belongs to — the dir two levels up from the store
    (<project>/<agent_dir>/amg), shown in the viewer header. Falls back gracefully."""
    p = store.resolve()
    return p.parent.parent.name or p.parent.name or p.name


def _tally(values: List[Any]) -> Dict[str, int]:
    """Sorted {value: count}, with None rendered as an em dash. Feeds the viewer's
    filter UI (what types / statuses / buckets / rels actually exist) and the summary."""
    out: Dict[str, int] = {}
    for v in values:
        k = str(v) if v is not None else "—"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def build_graph_data(store: Path) -> Dict[str, Any]:
    """Scan the graph and assemble the {meta, nodes, links} document. Pure read; the
    shared core for both the JSON export and (Group 2) the inlined HTML viewer."""
    raw = _scan_full(store)
    ids = set(raw)
    degree: Dict[str, int] = {nid: 0 for nid in raw}
    links: List[Dict[str, Any]] = []

    for nid, rec in raw.items():
        meta = rec["meta"]
        for e in meta.get("edges") or []:                  # typed edges; drop dangling
            if not isinstance(e, dict):
                continue
            to = e.get("to")
            if isinstance(to, str) and to in ids and to != nid:
                # w is the conductance weight Hebbian learning tunes; coact is the raw
                # co-activation counter that feeds it — both carried so the viewer can
                # show edge strength (width) and expose the Hebbian substrate.
                links.append({"source": nid, "target": to,
                              "rel": e.get("rel", "relates_to"), "w": e.get("w"),
                              "coact": e.get("coact"), "last_used": e.get("last_used"),
                              "origin": e.get("origin", "semantic")})
                degree[nid] += 1
                degree[to] += 1
        for m in meta.get("part_of") or []:                # membership in a real hub node
            if not isinstance(m, dict):
                continue
            topic = m.get("topic")
            if isinstance(topic, str) and topic in ids and topic != nid:
                links.append({"source": nid, "target": topic, "rel": "part_of",
                              "w": m.get("w"), "origin": "part_of"})
                degree[nid] += 1
                degree[topic] += 1

    nodes: List[Dict[str, Any]] = []
    for nid, rec in raw.items():
        meta = rec["meta"]
        nodes.append({
            "id": nid,
            "type": meta.get("type", "node"),
            "status": meta.get("status"),
            "summary": meta.get("summary", ""),
            "bucket": rec["bucket"],
            "group": _group_of(meta, rec["bucket"]),
            "degree": degree.get(nid, 0),
            "body": rec["body"],
            "frontmatter": {k: v for k, v in meta.items() if not str(k).startswith("_")},
        })

    return {
        "meta": {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "store": str(store),
            "project": _project_name(store),
            "node_count": len(nodes),
            "link_count": len(links),
            "types": _tally([n["type"] for n in nodes]),
            "statuses": _tally([n["status"] for n in nodes]),
            "buckets": _tally([n["bucket"] for n in nodes]),
            "rels": _tally([lk["rel"] for lk in links]),
            "viewer": _viewer_cfg(store),
        },
        "nodes": nodes,
        "links": links,
    }


def _dumps(data: Dict[str, Any]) -> str:
    """Serialize to pretty JSON. `default=str` is defensive armor: a well-formed graph
    has only JSON scalars (serialize_node quotes timestamp-like strings), but a stray
    datetime/date in a hand-edited node degrades to its string form instead of crashing
    the export — an inspection tool must never fail on a graph it can read."""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _write_json(data: Dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_dumps(data), encoding="utf-8")


def render_html(data: Dict[str, Any]) -> str:
    """Inline the graph data, the vendored 3d-force-graph library, and the viewer glue
    into ONE self-contained HTML string — no server, no network, read-only.

    The data goes into a <script type="application/json"> node, with `</` escaped to
    `<\\/` so a summary or body containing </script> cannot break out of the tag (and
    `\\/` stays valid JSON — JSON.parse restores it). The library is replaced FIRST and
    the data LAST, so a node whose content happens to contain a marker cannot corrupt
    the lib/glue injection."""
    template = (VIEWER_DIR / "viewer.template.html").read_text(encoding="utf-8")
    lib = (VIEWER_DIR / "3d-force-graph.min.js").read_text(encoding="utf-8")
    glue = (VIEWER_DIR / "viewer.js").read_text(encoding="utf-8")
    html = template.replace(_LIB_MARK, lib).replace(_VIEWER_MARK, glue)
    return html.replace(_DATA_MARK, _dumps(data).replace("</", "<\\/"))


def _opt(flag: str) -> Tuple[bool, Optional[str]]:
    """(present, value): value is the token after `flag` unless that is another --flag."""
    if flag not in sys.argv:
        return (False, None)
    i = sys.argv.index(flag)
    if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
        return (True, sys.argv[i + 1])
    return (True, None)


def main() -> int:
    _, store_arg = _opt("--store")
    store = Path(store_arg) if store_arg else R._default_store()
    data = build_graph_data(store)
    m = data["meta"]

    if "--stdout" in sys.argv:
        print(_dumps(data))
        return 0

    wrote: List[Path] = []
    json_present, json_val = _opt("--json")
    if json_present:
        out = Path(json_val) if json_val else (store / "cache" / "graph.json")
        _write_json(data, out)
        wrote.append(out)

    # HTML (the viewer) is the default action; skipped only when ONLY --json was asked.
    html_present, html_val = _opt("--html")
    if html_present or not json_present:
        out = Path(html_val) if html_val else (store / "cache" / "graph.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(data), encoding="utf-8")
        wrote.append(out)
        if "--open" in sys.argv:
            import webbrowser
            webbrowser.open(out.resolve().as_uri())

    for w in wrote:
        print(f"graph: {m['node_count']} node(s), {m['link_count']} link(s) -> {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
