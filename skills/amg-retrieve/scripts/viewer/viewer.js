/* AMG memory graph — viewer glue (roadmap Stage 15).
 *
 * Vanilla JS over the vendored 3d-force-graph (global `ForceGraph3D`). Reads the graph
 * from the inlined <script id="amg-data"> JSON that export_graph.py wrote — never fetches
 * anything (works offline, from file://). READ-ONLY: it only renders; it writes nothing.
 *
 * Visual encoding (functional, dark theme):
 *   node color  — by bucket (code/doc/data/notes/_hubs); the Stage 14 arbitration
 *                 verdicts override so they POP regardless of bucket: superseded (grey),
 *                 disputed (amber), rejected (red); stale is dimmed.
 *   node size   — by degree (+ a boost for hub/overview), so hubs read large ("hubs first").
 *   link color  — conflict (contradicts/supersedes) red + particles; part_of dim green;
 *                 follows faint; structural steel-blue; semantic teal. Width by w.
 *
 * Large-graph mode (auto above a threshold): start with hubs/overviews + their neighbors,
 * expand a node's neighborhood on click, and hide edges below the weight slider — so a big
 * graph is explorable, not a hairball.
 */
(function () {
  "use strict";

  function readData() {
    try { return JSON.parse(document.getElementById("amg-data").textContent); }
    catch (e) { return { meta: {}, nodes: [], links: [] }; }
  }
  var DATA = readData();
  var NODES = DATA.nodes || [];
  var LINKS_RAW = (DATA.links || []).map(function (l) { return Object.assign({}, l); });
  var META = DATA.meta || {};

  // ---- palettes ----------------------------------------------------------------
  var BUCKET_COLOR = { code: "#4aa3ff", doc: "#37c98b", data: "#f0b429",
                       notes: "#b48ef0", _hubs: "#ff7a45" };
  var STATUS_COLOR = { superseded: "#7a8699", disputed: "#f59e0b", rejected: "#ef4444" };
  var CONFLICT_RELS = { contradicts: 1, supersedes: 1 };
  var DEFAULT_NODE = "#9aa5b8";
  var LARGE_THRESHOLD = (META.large_graph_nodes | 0) || 1500;

  // ---- indexes -----------------------------------------------------------------
  var byId = {};
  NODES.forEach(function (n) { byId[n.id] = n; });
  var adj = {};                                   // id -> [neighbor ids]
  LINKS_RAW.forEach(function (l) {
    (adj[l.source] = adj[l.source] || []).push(l.target);
    (adj[l.target] = adj[l.target] || []).push(l.source);
  });

  // ---- state -------------------------------------------------------------------
  function keysOf(o) { return Object.keys(o || {}); }
  var filt = {
    type: setFromList(keysOf(META.types)),
    status: setFromList(keysOf(META.statuses)),
    bucket: setFromList(keysOf(META.buckets)),
  };
  var minW = 0;
  var term = "";
  var clusterMode = false;                        // color by cluster (group) vs by bucket
  var largeMode = NODES.length > LARGE_THRESHOLD;
  var visible = {};                               // used only in large mode

  function setFromList(arr) { var s = {}; arr.forEach(function (k) { s[k] = true; }); return s; }
  function statusKey(n) { return n.status == null ? "—" : n.status; }

  function seedVisible() {
    visible = {};
    var hubs = NODES.filter(function (n) { return n.type === "hub" || n.type === "overview"; });
    if (!hubs.length) {                           // no hubs — fall back to the most-connected
      hubs = NODES.slice().sort(function (a, b) { return (b.degree || 0) - (a.degree || 0); })
                  .slice(0, Math.min(40, NODES.length));
    }
    hubs.forEach(function (h) {
      visible[h.id] = true;
      (adj[h.id] || []).forEach(function (x) { visible[x] = true; });
    });
  }
  if (largeMode) seedVisible();

  // ---- accessors ---------------------------------------------------------------
  function groupColor(g) {                         // stable hue per cluster key
    g = String(g || "?"); var h = 0;
    for (var i = 0; i < g.length; i++) h = (h * 31 + g.charCodeAt(i)) >>> 0;
    return "hsl(" + (h % 360) + ",62%,62%)";
  }
  function nodeColor(n) {
    if (term) return matches(n) ? "#ffffff" : "rgba(120,130,145,0.18)";
    if (clusterMode) return groupColor(n.group);
    if (STATUS_COLOR[n.status]) return STATUS_COLOR[n.status];
    var c = BUCKET_COLOR[n.bucket] || DEFAULT_NODE;
    return n.status === "stale" ? "rgba(154,165,184,0.7)" : c;
  }
  function nodeVal(n) {
    var boost = (n.type === "hub" || n.type === "overview") ? 8 : 0;
    return 1 + (n.degree || 0) + boost + (term && matches(n) ? 12 : 0);
  }
  function nodeLabel(n) {
    return '<div style="max-width:320px;padding:6px 8px;background:#0d1322;border:1px solid #2a3450;'
      + 'border-radius:8px;color:#e6eaf2;font-size:12px">'
      + '<b>' + esc(n.id) + "</b><br>"
      + '<span style="color:#9aa5b8">' + esc(n.type) + " · " + esc(statusKey(n))
      + " · " + esc(n.bucket || "") + "</span><br>"
      + esc(trunc(n.summary || "", 160)) + "</div>";
  }
  function linkColor(l) {
    if (CONFLICT_RELS[l.rel]) return "#ef4444";
    if (l.rel === "part_of") return "#2f7a55";
    if (l.rel === "follows") return "#39425a";
    if (l.origin === "structural") return "#5b7aa8";
    return "#3fae9e";                              // semantic
  }
  function linkWidth(l) {
    var w = (l.w == null) ? 0.5 : l.w;
    return CONFLICT_RELS[l.rel] ? 1.5 + 2 * w : 0.4 + 1.6 * w;
  }

  // ---- filtering ---------------------------------------------------------------
  function matches(n) {
    if (!term) return false;
    return n.id.toLowerCase().indexOf(term) >= 0
        || (n.summary || "").toLowerCase().indexOf(term) >= 0;
  }
  function passes(n) {
    return filt.type[n.type] && filt.status[statusKey(n)] && filt.bucket[n.bucket || ""];
  }
  function nodeShown(n) {
    if (!passes(n)) return false;
    if (largeMode && !visible[n.id] && !(term && matches(n))) return false;
    return true;
  }
  function linkShown(l, ids) {
    if (!ids[l.source] || !ids[l.target]) return false;
    if (CONFLICT_RELS[l.rel]) return true;        // conflicts always show
    return (l.w == null) || l.w >= minW;
  }
  function currentData() {
    var kept = NODES.filter(nodeShown);
    var ids = {}; kept.forEach(function (n) { ids[n.id] = true; });
    var links = LINKS_RAW.filter(function (l) { return linkShown(l, ids); })
                         .map(function (l) { return Object.assign({}, l); });
    return { nodes: kept, links: links, ids: ids };
  }

  // ---- build the graph ---------------------------------------------------------
  var Graph = ForceGraph3D()(document.getElementById("graph"))
    .backgroundColor("#0b0e14")
    .nodeId("id")
    .nodeLabel(nodeLabel)
    .nodeColor(nodeColor)
    .nodeVal(nodeVal)
    .nodeOpacity(0.92)
    .nodeResolution(8)
    .linkColor(linkColor)
    .linkWidth(linkWidth)
    .linkOpacity(0.6)
    .linkDirectionalParticles(function (l) { return CONFLICT_RELS[l.rel] ? 2 : 0; })
    .linkDirectionalParticleWidth(1.6)
    .cooldownTime(15000)
    .onNodeClick(onNodeClick)
    .onBackgroundClick(function () { closePanel(); });

  function refresh() {
    var d = currentData();
    Graph.graphData({ nodes: d.nodes, links: d.links });
    setMeta(d.nodes.length, d.links.length);
    document.getElementById("empty").style.display = NODES.length ? "none" : "flex";
  }

  function focusNode(n) {
    var x = n.x || 0, y = n.y || 0, z = n.z || 0;
    var dist = 90, r = 1 + dist / (Math.hypot(x, y, z) || 1);
    Graph.cameraPosition({ x: x * r, y: y * r, z: z * r }, n, 700);
  }
  function onNodeClick(n) {
    if (largeMode) {                              // expand-on-click: pull in the neighborhood
      visible[n.id] = true;
      (adj[n.id] || []).forEach(function (x) { visible[x] = true; });
      refresh();
    }
    openPanel(n);
    focusNode(n);
  }

  // ---- side panel --------------------------------------------------------------
  function openPanel(n) {
    document.getElementById("p-id").textContent = n.id;
    var badges = [["type", n.type], ["status", statusKey(n)], ["bucket", n.bucket || ""]];
    var fm = n.frontmatter || {};
    if (fm.source_kind) badges.push(["kind", fm.source_kind]);
    if (fm.policy) badges.push(["policy", fm.policy]);
    if (fm.confidence != null) badges.push(["confidence", fm.confidence]);
    var vstat = fm.verification && fm.verification.status;
    if (vstat) badges.push(["verification", vstat]);
    document.getElementById("p-badges").innerHTML = badges.map(function (b) {
      return '<span class="badge">' + esc(b[0]) + ": " + esc(String(b[1])) + "</span>";
    }).join("");
    document.getElementById("p-summary").textContent = n.summary || "(no summary yet — stale)";
    document.getElementById("p-ptr").textContent = pointer(fm);

    var edges = (fm.edges || []).filter(function (e) { return e && e.to; });
    var ew = document.getElementById("p-edges-wrap");
    if (edges.length) {
      document.getElementById("p-edges").innerHTML = edges.map(function (e) {
        var known = byId[e.to];
        var to = known
          ? '<a data-go="' + esc(e.to) + '">' + esc(e.to) + "</a>"
          : '<span style="color:#7a8699">' + esc(e.to) + " (external)</span>";
        var w = e.w == null ? "" : ' <span style="color:#9aa5b8">w=' + esc(String(e.w)) + "</span>";
        return '<div class="edge"><span class="rel">' + esc(e.rel || "rel") + "</span>" + to + w + "</div>";
      }).join("");
      ew.style.display = "";
    } else { ew.style.display = "none"; }

    var bw = document.getElementById("p-body-wrap");
    if (n.body && n.body.trim()) {
      document.getElementById("p-body").textContent = n.body;
      bw.style.display = "";
    } else { bw.style.display = "none"; }

    document.getElementById("p-fm").textContent = JSON.stringify(fm, null, 2);
    document.getElementById("panel").classList.add("open");
  }
  function closePanel() { document.getElementById("panel").classList.remove("open"); }
  function pointer(fm) {
    if (!fm.source_path) return "";
    var p = fm.source_path;
    if (fm.lineno != null) p += ":" + fm.lineno + (fm.line_end != null && fm.line_end !== fm.lineno ? "-" + fm.line_end : "");
    return p;
  }

  // ---- controls ----------------------------------------------------------------
  function setMeta(nShown, lShown) {
    document.getElementById("meta").textContent =
      nShown + "/" + NODES.length + " nodes · " + lShown + "/" + LINKS_RAW.length + " links"
      + (largeMode ? " · large mode" : "");
  }
  function buildFilters() {
    var box = document.getElementById("filters");
    function section(title, dim, tally, colorOf) {
      var h = '<h4>' + title + "</h4>";
      keysOf(tally).sort().forEach(function (k) {
        var sw = colorOf ? '<span class="swatch" style="background:' + (colorOf(k) || "#556") + '"></span>' : "";
        h += '<label><input type="checkbox" data-dim="' + dim + '" data-key="' + esc(k) + '" checked>'
          + sw + '<span>' + esc(k) + '</span><span class="count">' + tally[k] + "</span></label>";
      });
      return h;
    }
    box.innerHTML =
      section("Bucket", "bucket", META.buckets, function (k) { return BUCKET_COLOR[k]; })
      + section("Status", "status", META.statuses, function (k) { return STATUS_COLOR[k]; })
      + section("Type", "type", META.types, null)
      + '<h4>Min edge weight</h4><div class="row"><input type="range" id="minw" min="0" max="1" '
      + 'step="0.05" value="0"><span id="minw-val" class="count">0.00</span></div>';
    box.querySelectorAll("input[type=checkbox]").forEach(function (cb) {
      cb.addEventListener("change", function () {
        filt[cb.getAttribute("data-dim")][cb.getAttribute("data-key")] = cb.checked;
        refresh();
      });
    });
    var mw = document.getElementById("minw");
    mw.addEventListener("input", function () {
      minW = parseFloat(mw.value);
      document.getElementById("minw-val").textContent = minW.toFixed(2);
      refresh();
    });
  }
  function buildLegend() {
    function dot(c) { return '<span class="swatch" style="background:' + c + '"></span>'; }
    var b = "<h4>Nodes — bucket</h4>";
    Object.keys(BUCKET_COLOR).forEach(function (k) { b += '<div class="row">' + dot(BUCKET_COLOR[k]) + k + "</div>"; });
    b += "<h4>Nodes — arbitration status</h4>";
    Object.keys(STATUS_COLOR).forEach(function (k) { b += '<div class="row">' + dot(STATUS_COLOR[k]) + k + "</div>"; });
    b += '<div class="row">' + dot("rgba(154,165,184,0.7)") + "stale (dimmed)</div>";
    b += "<h4>Edges</h4>"
      + '<div class="row">' + dot("#ef4444") + "contradicts / supersedes</div>"
      + '<div class="row">' + dot("#3fae9e") + "semantic (documents, depends_on…)</div>"
      + '<div class="row">' + dot("#5b7aa8") + "structural (calls, imports…)</div>"
      + '<div class="row">' + dot("#2f7a55") + "part_of</div>"
      + '<div class="row">' + dot("#39425a") + "follows</div>";
    document.getElementById("legend").innerHTML = b;
  }
  function togglePop(id, btn) {
    var el = document.getElementById(id);
    var open = el.style.display === "block";
    document.getElementById("filters").style.display = "none";
    document.getElementById("legend").style.display = "none";
    document.getElementById("btn-filters").classList.remove("on");
    document.getElementById("btn-legend").classList.remove("on");
    if (!open) { el.style.display = "block"; btn.classList.add("on");
      el.style.right = id === "legend" ? "12px" : ""; el.style.left = id === "filters" ? "12px" : ""; }
  }

  // ---- wire up -----------------------------------------------------------------
  buildFilters();
  buildLegend();
  document.getElementById("btn-filters").addEventListener("click", function () { togglePop("filters", this); });
  document.getElementById("btn-legend").addEventListener("click", function () { togglePop("legend", this); });
  document.getElementById("panel-close").addEventListener("click", closePanel);
  document.getElementById("btn-reset").addEventListener("click", function () { Graph.zoomToFit(600, 40); });

  var clusterBtn = document.getElementById("btn-cluster");
  clusterBtn.addEventListener("click", function () {
    clusterMode = !clusterMode;
    clusterBtn.classList.toggle("on", clusterMode);
    Graph.nodeColor(nodeColor);                   // re-apply accessor -> recolor in place
  });

  var largeBtn = document.getElementById("btn-large");
  function syncLargeBtn() { largeBtn.classList.toggle("on", largeMode); largeBtn.textContent = largeMode ? "Show all" : "Large mode"; }
  largeBtn.addEventListener("click", function () {
    largeMode = !largeMode;
    if (largeMode) seedVisible();
    syncLargeBtn(); refresh();
  });
  syncLargeBtn();

  var search = document.getElementById("search");
  search.addEventListener("input", function () {
    term = search.value.trim().toLowerCase();
    if (term && largeMode) {                      // reveal matches (+ neighbors) in large mode
      NODES.forEach(function (n) {
        if (matches(n)) { visible[n.id] = true; (adj[n.id] || []).forEach(function (x) { visible[x] = true; }); }
      });
    }
    refresh();
    if (term) {
      var hit = NODES.filter(matches)[0];
      if (hit && hit.x != null) focusNode(hit);
    }
  });
  document.getElementById("panel").addEventListener("click", function (e) {
    var go = e.target && e.target.getAttribute && e.target.getAttribute("data-go");
    if (go && byId[go]) {
      if (largeMode) { visible[go] = true; refresh(); }
      openPanel(byId[go]);
      var t = byId[go]; if (t.x != null) focusNode(t);
    }
  });

  // ---- helpers -----------------------------------------------------------------
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function trunc(s, n) { s = String(s); return s.length > n ? s.slice(0, n) + "…" : s; }

  refresh();
  if (NODES.length) setTimeout(function () { Graph.zoomToFit(800, 50); }, 600);
})();
