# Vendored viewer assets

The 3D graph viewer (roadmap Stage 15) is **self-contained and offline**: it never
fetches anything at view time. `export_graph.py` inlines all three files below into a
single HTML, so the output opens by double-click in a browser — no server, no network,
nothing leaves the machine (the memory graph can hold sensitive project knowledge).

## `3d-force-graph.min.js` — vendored library (kept byte-exact)

- **Package:** `3d-force-graph` — a 3D force-directed graph on ThreeJS + d3-force-3d.
- **Version:** `1.80.0`
- **Source:** https://unpkg.com/3d-force-graph@1.80.0/dist/3d-force-graph.min.js
- **License:** MIT (vasturiano/3d-force-graph).
- **Self-contained:** the standalone UMD bundle exposes the global `ForceGraph3D` and
  bundles its dependencies (ThreeJS, d3-force-3d) — no separate `three` needed, no
  external `require`. It contains no literal `</script>`, so it inlines safely.

This file is the upstream artifact unchanged. To re-vendor a newer version, download the
same `dist/3d-force-graph.min.js` for the pinned version and update the version above.

## `viewer.template.html` / `viewer.js` — the AMG glue (authored here)

`viewer.template.html` is the dark-theme shell + CSS with three injection markers;
`viewer.js` builds the graph from the inlined `window`-level data (read-only: it only
renders). Both are plain files — no build step, no Node.js.
