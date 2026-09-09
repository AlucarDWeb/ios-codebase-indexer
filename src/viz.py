"""Generate a self-contained HTML explorer from an index-store graph db."""
import json, os, sqlite3

TEMPLATE_HEAD = """<meta charset="utf-8">
<title>__TITLE__</title>
<style>
:root{
  --bg:#0d1117; --panel:#141b24; --panel2:#1b2430; --line:#243040; --fg:#d6e2f0;
  --dim:#7d8da3; --accent:#4dd4c0; --accent2:#7aa2f7; --warn:#e0af68; --pink:#f7768e;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
:root[data-theme="light"]{
  --bg:#f6f8fa; --panel:#fff; --panel2:#eef2f6; --line:#d5dde6; --fg:#1b2430;
  --dim:#5b6b80; --accent:#0f8b7a; --accent2:#2f5fd0;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--mono);font-size:13px;line-height:1.5}
header{display:flex;align-items:baseline;gap:14px;padding:12px 18px;border-bottom:1px solid var(--line);
  background:var(--panel);flex-wrap:wrap}
header h1{font-size:14px;margin:0;font-weight:600;letter-spacing:.02em}
header .path{color:var(--dim);font-size:11px;word-break:break-all}
nav{display:flex;gap:2px;padding:0 18px;background:var(--panel);border-bottom:1px solid var(--line)}
nav button{background:none;border:none;border-bottom:2px solid transparent;color:var(--dim);
  font-family:var(--mono);font-size:12px;padding:9px 14px;cursor:pointer}
nav button.on{color:var(--accent);border-bottom-color:var(--accent)}
main{padding:18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.tile .n{font-size:20px;font-weight:600;color:var(--accent)}
.tile .k{color:var(--dim);font-size:11px;text-transform:lowercase}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px 14px;overflow:auto}
.card h2{font-size:12px;margin:0 0 10px;color:var(--dim);font-weight:600;
  letter-spacing:.04em}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{text-align:left;padding:3px 8px 3px 0;white-space:nowrap}
th{color:var(--dim);font-weight:500;border-bottom:1px solid var(--line)}
tbody tr:hover{background:var(--panel2)}
.bar{height:6px;background:var(--accent2);border-radius:3px;display:inline-block;vertical-align:middle}
.num{text-align:right;font-variant-numeric:tabular-nums}
.explorer{display:grid;grid-template-columns:minmax(340px,1fr) minmax(420px,1.2fr);gap:14px;align-items:start}
@media(max-width:1100px){.explorer{grid-template-columns:1fr}}
input[type=text],select{background:var(--panel2);border:1px solid var(--line);color:var(--fg);
  font-family:var(--mono);font-size:12px;padding:6px 8px;border-radius:4px;width:100%}
.filters{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.filters>*{flex:1 1 140px}
.rows{max-height:70vh;overflow:auto}
.row{padding:5px 8px;border-radius:4px;cursor:pointer;display:flex;gap:8px;align-items:baseline}
.row:hover{background:var(--panel2)}
.row.sel{background:var(--panel2);outline:1px solid var(--accent)}
.row .nm{color:var(--fg)}
.row .meta{color:var(--dim);font-size:11px;margin-left:auto;white-space:nowrap}
.badge{font-size:10px;padding:1px 5px;border-radius:3px;background:var(--panel2);color:var(--accent2);
  border:1px solid var(--line)}
.group{color:var(--dim);font-size:11px;margin:10px 0 3px;border-bottom:1px dotted var(--line)}
.loc{color:var(--dim);font-size:11px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:12px;margin-bottom:12px}
.kv div:nth-child(odd){color:var(--dim)}
.tree{font-size:12px;white-space:pre;overflow-x:auto}
.tree a{color:var(--accent2);text-decoration:none;cursor:pointer}
.tree a:hover{text-decoration:underline}
svg{width:100%;height:420px;background:var(--panel);border:1px solid var(--line);border-radius:6px}
svg text{font-family:var(--mono);font-size:10px;fill:var(--fg)}
svg line{stroke:var(--line)}
.empty{color:var(--dim);padding:20px 0}
footer{color:var(--dim);font-size:11px;padding:14px 18px;border-top:1px solid var(--line)}
.pill{display:inline-block;font-size:10px;color:var(--dim);border:1px solid var(--line);border-radius:10px;
  padding:0 7px;margin-right:5px}
.prose{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:14px;
  line-height:1.6;max-width:78ch;color:var(--fg)}
.prose p{margin:0 0 12px}
.era{border-left:2px solid var(--line);padding:2px 0 2px 14px;margin:0 0 16px}
.era.on{border-left-color:var(--accent)}
.era h3{font-size:13px;margin:0 0 6px;font-family:var(--mono);color:var(--accent);cursor:pointer}
.era .facts{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--dim);margin-bottom:6px}
.chart{width:100%;height:160px;background:var(--panel);border:1px solid var(--line);border-radius:6px}
.chart rect{fill:var(--accent2);fill-opacity:.7}
.chart rect.hi{fill:var(--accent);fill-opacity:1}
.chart text{font-size:9px;fill:var(--dim)}
.subj{color:var(--fg)}
.sha{color:var(--dim);font-size:11px}
a.ext{color:var(--accent2);text-decoration:none}
a.ext:hover{text-decoration:underline}
</style>
"""


def _path_layer(rel, depth=2):
    if not rel:
        return "(external)"
    parts = rel.split("/")
    return parts[0] if len(parts) <= depth else "/".join(parts[:depth])


def slice_data(db, scope=None, limit=3000, edge_cap=60000, per_node_cap=45, detail_cap=12,
               dead_cap=600, file_cap=10):
    cur = db.cursor()
    cur.row_factory = None
    meta = {r[0]: r[1] for r in cur.execute("SELECT key, value FROM meta")}
    counts = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("symbols", "edges", "occurrences", "files", "units")}
    ekinds = dict(cur.execute("SELECT kind, COUNT(*) FROM edges GROUP BY kind ORDER BY 2 DESC").fetchall())
    kinds = dict(cur.execute("""SELECT kind, COUNT(*) FROM symbols WHERE in_repo=1
                                GROUP BY kind ORDER BY 2 DESC LIMIT 18""").fetchall())
    db.create_function("layer", 1, _path_layer)
    layers = cur.execute("""SELECT layer(rel), COUNT(*) FROM files WHERE in_repo=1
                            GROUP BY 1 ORDER BY 2 DESC LIMIT 15""").fetchall()
    layers = [list(r) for r in layers]
    modules = cur.execute("""SELECT module, COUNT(*) syms, SUM(call_in), SUM(call_out)
                             FROM symbols WHERE in_repo=1 AND module IS NOT NULL AND module != ''
                             GROUP BY module ORDER BY syms DESC""").fetchall()
    modules = [list(r) for r in modules]
    lookup = db.cursor()
    lookup.row_factory = None
    ecur = db.cursor()
    ecur.row_factory = None
    ph_cache = {}

    def relpath(ph):
        if ph not in ph_cache:
            r = lookup.execute("SELECT COALESCE(rel, path) FROM files WHERE path_hash=?", (ph,)).fetchone()
            ph_cache[ph] = r[0] if r else ""
        return ph_cache[ph]
    mod_edges = cur.execute("""SELECT a.module, b.module, COUNT(*) n FROM edges e
                               JOIN symbols a ON a.usr_hash = e.src
                               JOIN symbols b ON b.usr_hash = e.dst
                               WHERE e.kind='CALLS' AND a.in_repo=1 AND b.in_repo=1
                                 AND a.module IS NOT NULL AND b.module IS NOT NULL
                                 AND a.module != b.module
                               GROUP BY 1,2 HAVING COUNT(*) >= 3 ORDER BY 3 DESC LIMIT 1200""").fetchall()
    mod_edges = [list(r) for r in mod_edges]

    # The panel only renders pairs among the top modules, so only those need detail.
    size_by_mod = {m: n for m, n, *_ in modules}
    shown = set(sorted({m for e in mod_edges for m in e[:2]},
                       key=lambda m: -size_by_mod.get(m, 0))[:45])
    wanted = {(a, b) for a, b, n in mod_edges if a in shown and b in shown and n >= 8}
    mod_edge_details = {}
    mod_edge_files = {}
    if wanted:
        rows = cur.execute("""SELECT a.module, b.module, a.name, b.name, COUNT(*) n,
                                     MIN(e.path_hash), MIN(e.line)
                              FROM edges e
                              JOIN symbols a ON a.usr_hash = e.src
                              JOIN symbols b ON b.usr_hash = e.dst
                              WHERE e.kind='CALLS' AND a.in_repo=1 AND b.in_repo=1
                                AND a.module IS NOT NULL AND b.module IS NOT NULL
                                AND a.module <> b.module
                              GROUP BY e.src, e.dst
                              ORDER BY 1, 2, 5 DESC""").fetchall()
        for am, bm, caller, callee, n, ph, line in rows:
            if (am, bm) not in wanted:
                continue
            bucket = mod_edge_details.setdefault(f"{am}>{bm}", [])
            if len(bucket) >= detail_cap:
                continue
            bucket.append([caller, callee, relpath(ph) if ph else "", line or 0, n])

        file_rows = cur.execute("""SELECT a.module, b.module, e.path_hash, COUNT(*) n
                                   FROM edges e
                                   JOIN symbols a ON a.usr_hash = e.src
                                   JOIN symbols b ON b.usr_hash = e.dst
                                   WHERE e.kind='CALLS' AND a.in_repo=1 AND b.in_repo=1
                                     AND a.module IS NOT NULL AND b.module IS NOT NULL
                                     AND a.module <> b.module
                                   GROUP BY a.module, b.module, e.path_hash
                                   ORDER BY 1, 2, 4 DESC""").fetchall()
        for am, bm, ph, n in file_rows:
            if (am, bm) not in wanted:
                continue
            bucket = mod_edge_files.setdefault(f"{am}>{bm}", [])
            if len(bucket) >= file_cap:
                continue
            bucket.append([relpath(ph) if ph else "(unknown)", n])

    where = "s.in_repo = 1"
    args = []
    if scope:
        where += " AND (s.module = ? OR f.rel GLOB ?)"
        args += [scope, scope if "*" in scope else scope + "*"]
    rows = cur.execute(f"""SELECT s.usr_hash, s.name, s.kind, s.module, f.rel, s.def_line, s.in_deg, s.out_deg,
                           s.call_in, s.call_out, s.ref_count
                           FROM symbols s LEFT JOIN files f ON f.path_hash = s.def_path_hash
                           WHERE {where} ORDER BY (s.in_deg + s.out_deg) DESC LIMIT ?""",
                       args + [limit if limit else -1]).fetchall()
    ids = {r[0]: i for i, r in enumerate(rows)}
    nodes = [{"n": r[1], "k": r[2], "m": r[3] or "", "f": r[4] or "", "l": r[5] or 0,
              "i": r[6], "o": r[7], "ci": r[8], "co": r[9], "rc": r[10]} for r in rows]
    extra, edges = {}, []

    def node_id(uh):
        if uh in ids:
            return ids[uh]
        if uh in extra:
            return extra[uh]
        r = lookup.execute("""SELECT s.name, s.kind, s.module, f.rel, s.def_line, s.in_deg, s.out_deg,
                           s.call_in, s.call_out, s.ref_count FROM symbols s
                           LEFT JOIN files f ON f.path_hash = s.def_path_hash WHERE s.usr_hash=?""",
                        (uh,)).fetchone()
        if not r:
            return None
        idx = len(nodes)
        nodes.append({"n": r[0], "k": r[1], "m": r[2] or "", "f": r[3] or "", "l": r[4] or 0,
                      "i": r[5], "o": r[6], "ci": r[7], "co": r[8], "rc": r[9], "x": 1})
        extra[uh] = idx
        return idx

    keys = list(ids.keys())
    pairs, per_anchor = {}, {}
    for i in range(0, len(keys), 300):
        batch = keys[i:i + 300]
        holder = ",".join("?" * len(batch))
        for col in ("src", "dst"):
            q = f"""SELECT src, dst, kind, COUNT(*) n, MIN(path_hash), MIN(line) FROM edges
                    WHERE {col} IN ({holder}) GROUP BY src, dst, kind"""
            for src, dst, kind, n, ph, line in ecur.execute(q, batch).fetchall():
                key = (src, dst, kind)
                pairs[key] = (n, ph, line)
                anchor = src if col == "src" else dst
                per_anchor.setdefault(anchor, []).append((n, key))
    keep = set()
    for anchor, lst in per_anchor.items():
        lst.sort(key=lambda t: -t[0])
        for _, key in lst[:per_node_cap]:
            keep.add(key)
    selected = sorted(keep, key=lambda k: -pairs[k][0])[:edge_cap]
    for src, dst, kind in selected:
        n, ph, line = pairs[(src, dst, kind)]
        a, b = node_id(src), node_id(dst)
        if a is None or b is None:
            continue
        edges.append([a, b, kind, relpath(ph) if ph else "", line or 0, n])
    dead, total_dead = [], 0
    import sqlite3 as _sq
    prior_factory = db.row_factory
    try:
        import deadcode
        db.row_factory = _sq.Row
        total_dead, rows = deadcode.candidates(db, limit=dead_cap)  # Swift, non-vendored
        dead = [[r["name"], r["kind"], r["module"] or "", r["rel"] or "", r["def_line"] or 0]
                for r in rows]
    except Exception:
        pass
    finally:
        db.row_factory = prior_factory

    return {"meta": meta, "counts": counts, "edge_kinds": ekinds, "sym_kinds": kinds,
            "dead": dead, "dead_total": total_dead,
            "layers": layers, "modules": modules, "mod_edges": mod_edges,
            "mod_edge_details": mod_edge_details,
            "mod_edge_files": mod_edge_files,
            "nodes": nodes, "edges": edges, "slice_size": len(ids), "scope": scope or "repo"}


BODY = """
<header>
  <h1>__PROJECT__ <span class="pill">index-store graph</span></h1>
  <span class="path" id="storePath" title="__STORE__"></span>
  <span style="margin-left:auto"><span class="pill" id="themeToggle" style="cursor:pointer">theme</span></span>
</header>
<nav>
  <button data-tab="overview" class="on">overview</button>
  <button data-tab="modules">modules</button>
  <button data-tab="symbols">symbols</button>
  <button data-tab="dead">dead code</button>
  <button data-tab="history">history</button>
  <button data-tab="docs">docs</button>
</nav>
<main>
  <section id="overview"></section>
  <section id="modules" hidden></section>
  <section id="symbols" hidden></section>
  <section id="dead" hidden></section>
  <section id="history" hidden></section>
  <section id="docs" hidden></section>
</main>
<footer>
  built __BUILT__ from index-store format v__FMT__ &middot; slice: __SLICE__ symbols, __EDGECOUNT__ edges
  &middot; history __HISTORY__ &middot; regenerate with <code>idxg viz</code>
</footer>
<script>
const D = __DATA__;
const fmt = n => (n ?? 0).toLocaleString();
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c;
  if (txt !== undefined) e.textContent = txt; return e; };

/* ---------- adjacency ---------- */
const out = new Map(), inn = new Map();
for (const [a, b, k, f, l, n] of D.edges) {
  if (!out.has(a)) out.set(a, []); out.get(a).push([b, k, f, l, n]);
  if (!inn.has(b)) inn.set(b, []); inn.get(b).push([a, k, f, l, n]);
}

/* ---------- overview ---------- */
function overview() {
  const s = document.getElementById('overview');
  const cov = D.meta.coverage_pct;
  const tiles = [
    ['symbols', D.counts.symbols], ['edges', D.counts.edges],
    ['occurrences', D.counts.occurrences], ['indexed files', D.counts.files],
    ['units', D.counts.units]
  ];
  const t = el('div', 'tiles');
  for (const [k, v] of tiles) {
    const d = el('div', 'tile'); d.append(el('div', 'n', fmt(v)), el('div', 'k', k)); t.append(d);
  }
  s.append(t);
  const g = el('div', 'grid2');
  g.append(barCard('edges by kind', Object.entries(D.edge_kinds)));
  g.append(barCard('symbol kinds (in repo)', Object.entries(D.sym_kinds)));
  g.append(barCard('indexed files by layer', D.layers));
  const c = el('div', 'card'); c.append(el('h2', null, 'top modules'));
  const tb = el('table');
  tb.innerHTML = '<thead><tr><th>module</th><th class="num">symbols</th><th class="num">calls in</th>' +
    '<th class="num">calls out</th></tr></thead>';
  const body = el('tbody');
  for (const [m, n, ci, co] of D.modules.slice(0, 25)) {
    const tr = el('tr');
    const a = el('td'); const link = el('a', null, m); link.style.cursor = 'pointer';
    link.style.color = 'var(--accent2)'; link.onclick = () => { showTab('symbols'); setModule(m); };
    a.append(link);
    tr.append(a, numTd(n), numTd(ci), numTd(co)); body.append(tr);
  }
  tb.append(body); c.append(tb); g.append(c); s.append(g);
}
const numTd = v => { const d = el('td', 'num', fmt(v)); return d; };
function barCard(title, pairs) {
  const c = el('div', 'card'); c.append(el('h2', null, title));
  const max = Math.max(...pairs.map(p => p[1]));
  const tb = el('table'); const body = el('tbody');
  for (const [k, v] of pairs) {
    const tr = el('tr');
    tr.append(el('td', null, k), numTd(v));
    const bd = el('td'); const b = el('span', 'bar');
    b.style.width = Math.max(2, 120 * v / max) + 'px'; bd.append(b); tr.append(bd);
    body.append(tr);
  }
  tb.append(body); c.append(tb); return c;
}

/* ---------- module graph ---------- */
let modState = null;
function modulesTab() {
  const s = document.getElementById('modules');
  s.innerHTML = '';
  const wrap = el('div', 'card');
  const head = el('div');
  head.style.display = 'flex'; head.style.alignItems = 'baseline'; head.style.gap = '10px';
  head.append(el('h2', null, 'cross-module call graph (top 45 modules, pairs with 8+ call sites)'));
  const hint = el('span', 'loc', 'click a module to isolate its calls, click empty space to reset');
  hint.style.marginLeft = 'auto'; head.append(hint);
  wrap.append(head);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 1200 640'); svg.style.height = '640px';
  wrap.append(svg);
  s.append(wrap);
  const info = el('div', 'card'); info.id = 'modinfo'; info.style.marginTop = '14px';
  info.append(el('div', 'empty', 'no module selected'));
  s.append(info);

  const size = new Map(D.modules.map(([m, n]) => [m, n]));
  const names = [...new Set(D.mod_edges.flatMap(([a, b]) => [a, b]))]
    .sort((a, b) => (size.get(b) || 0) - (size.get(a) || 0)).slice(0, 45);
  const idx = new Map(names.map((n, i) => [n, i]));
  const links = D.mod_edges.filter(([a, b, n]) => idx.has(a) && idx.has(b) && n >= 8)
    .map(([a, b, n]) => ({ s: idx.get(a), t: idx.get(b), n }));
  const nodes = names.map((n, i) => ({ n, r: Math.min(22, 4 + Math.sqrt(size.get(n) || 1) / 5),
    x: 600 + 460 * Math.cos(2 * Math.PI * i / names.length),
    y: 320 + 260 * Math.sin(2 * Math.PI * i / names.length), vx: 0, vy: 0 }));
  simulate(nodes, links, 1200, 640, 320, 420, 9000, 210);

  const maxW = Math.max(...links.map(l => l.n));
  const NS = 'http://www.w3.org/2000/svg';
  const linkEls = links.map(l => {
    const ln = document.createElementNS(NS, 'line');
    ln.setAttribute('x1', nodes[l.s].x); ln.setAttribute('y1', nodes[l.s].y);
    ln.setAttribute('x2', nodes[l.t].x); ln.setAttribute('y2', nodes[l.t].y);
    ln.setAttribute('stroke-linecap', 'round');
    svg.append(ln);
    return ln;
  });
  const nodeEls = nodes.map((nd, i) => {
    const g = document.createElementNS(NS, 'g');
    const ci = document.createElementNS(NS, 'circle');
    ci.setAttribute('cx', nd.x); ci.setAttribute('cy', nd.y); ci.setAttribute('r', nd.r);
    const tx = document.createElementNS(NS, 'text');
    tx.setAttribute('x', nd.x + nd.r + 3); tx.setAttribute('y', nd.y + 3);
    tx.textContent = nd.n;
    const tt = document.createElementNS(NS, 'title');
    tt.textContent = `${nd.n} - ${fmt(size.get(nd.n))} symbols`;
    g.append(ci, tx, tt); g.style.cursor = 'pointer';
    g.onclick = ev => { ev.stopPropagation(); selectModule(i); };
    svg.append(g);
    return { g, ci, tx };
  });
  svg.onclick = () => selectModule(null);
  modState = { svg, nodes, links, linkEls, nodeEls, size, idx, names, maxW, sel: null };
  paintModules();
}

function paintModules() {
  const { nodes, links, linkEls, nodeEls, maxW, sel } = modState;
  const touching = new Set();
  let localMax = 1;
  if (sel !== null) {
    links.forEach(l => {
      if (l.s === sel) { touching.add(l.t); localMax = Math.max(localMax, l.n); }
      if (l.t === sel) { touching.add(l.s); localMax = Math.max(localMax, l.n); }
    });
    touching.add(sel);
  }
  links.forEach((l, i) => {
    const ln = linkEls[i];
    const on = sel === null || l.s === sel || l.t === sel;
    const outgoing = l.s === sel;
    if (sel === null) {
      ln.setAttribute('stroke', 'var(--accent2)');
      ln.setAttribute('stroke-width', 0.4 + 2.6 * l.n / maxW);
      ln.setAttribute('stroke-opacity', 0.28);
    } else if (on) {
      ln.setAttribute('stroke', outgoing ? 'var(--warn)' : 'var(--accent)');
      ln.setAttribute('stroke-width', 2.2 + 5.5 * Math.sqrt(l.n / localMax));
      ln.setAttribute('stroke-opacity', 1);
    } else {
      ln.setAttribute('stroke', 'var(--line)');
      ln.setAttribute('stroke-width', 0.4);
      ln.setAttribute('stroke-opacity', 0.07);
    }
  });
  nodeEls.forEach((e, i) => {
    const isSel = i === sel;
    const near = sel === null || touching.has(i);
    e.ci.setAttribute('fill', isSel ? 'var(--pink)' : 'var(--accent)');
    e.ci.setAttribute('fill-opacity', isSel ? 0.5 : near ? 0.25 : 0.08);
    e.ci.setAttribute('stroke', isSel ? 'var(--pink)' : 'var(--accent)');
    e.ci.setAttribute('stroke-width', isSel ? 2 : 1);
    e.ci.setAttribute('stroke-opacity', near ? 1 : 0.15);
    e.g.setAttribute('opacity', near ? 1 : 0.35);
    e.tx.setAttribute('fill', isSel ? 'var(--pink)' : nodes[i].r < 7 && sel === null ? 'var(--dim)' : 'var(--fg)');
    e.tx.setAttribute('opacity', near ? 1 : 0.25);
    e.g.parentNode.append(e.g);
  });
}

function selectModule(i) {
  modState.sel = i;
  paintModules();
  const info = document.getElementById('modinfo');
  info.innerHTML = '';
  if (i === null) { info.append(el('div', 'empty', 'no module selected')); return; }
  const { names, links, size } = modState;
  const name = names[i];
  const t = el('h2', null, name); t.style.color = 'var(--pink)'; t.style.fontSize = '13px';
  info.append(t);
  const outs = links.filter(l => l.s === i).sort((a, b) => b.n - a.n);
  const ins = links.filter(l => l.t === i).sort((a, b) => b.n - a.n);
  const kv = el('div', 'kv');
  for (const [k, v] of [['symbols', fmt(size.get(name))],
                        ['calls out', `${fmt(outs.reduce((s, l) => s + l.n, 0))} sites to ${outs.length} modules`],
                        ['calls in', `${fmt(ins.reduce((s, l) => s + l.n, 0))} sites from ${ins.length} modules`]]) {
    kv.append(el('div', null, k), el('div', null, v));
  }
  info.append(kv);
  // Stacked, not side by side: call-site paths are wide and a two-column grid clipped them.
  const g = el('div');
  g.style.display = 'grid';
  g.style.gap = '16px';
  g.append(modTree(`callers (inbound)`, ins, l => l.s, 'var(--accent)', true));
  g.append(modTree(`callees (outbound)`, outs, l => l.t, 'var(--warn)', false));
  info.append(g);
  const jump = el('div');
  const b = el('a', null, `browse ${name} symbols`);
  b.style.color = 'var(--accent2)'; b.style.cursor = 'pointer';
  b.onclick = () => { showTab('symbols'); setModule(name); };
  jump.style.marginTop = '10px'; jump.append(b);
  info.append(jump);
}

function modTree(title, rows, pick, color, inbound) {
  const c = el('div');
  const h = el('h2', null, `${title}: ${rows.length}`); h.style.color = color;
  c.append(h);
  if (!rows.length) { c.append(el('div', 'empty', '(none)')); return c; }
  const pre = el('div', 'tree');
  const shown = rows.slice(0, 40);
  shown.forEach((l, i) => {
    const other = modState.names[pick(l)];
    const self = modState.names[modState.sel];
    const last = i === shown.length - 1;
    const line = el('div');
    line.append(document.createTextNode(last ? '\u2514\u2500 ' : '\u251c\u2500 '));
    const a = el('a', null, other);
    a.onclick = () => selectModule(modState.idx.get(other));
    line.append(a);
    const tail = el('span', 'loc',
      `  Module <CALLS>  ${fmt(modState.size.get(other) || 0)} symbols (x${fmt(l.n)})`);
    line.append(tail);
    const key = inbound ? `${other}>${self}` : `${self}>${other}`;
    const detail = (D.mod_edge_details || {})[key] || [];
    if (detail.length) {
      const toggle = el('a', null, '  functions');
      toggle.style.color = 'var(--dim)';
      line.append(toggle);
      const kids = el('div');
      kids.hidden = true;
      kids.style.paddingLeft = '18px';
      kids.style.borderLeft = last ? 'none' : '1px solid var(--line)';
      const files = ((D.mod_edge_files || {})[key] || []);
      if (files.length) {
        const head = el('div', 'loc', inbound
          ? `called from these files in ${other}:` : `called from these files in ${self}:`);
        head.style.marginTop = '2px';
        kids.append(head);
        files.forEach(([f, n]) => {
          const row = el('div');
          const nm = el('span', null, f.split('/').slice(-2).join('/'));
          nm.style.color = 'var(--accent)';
          nm.title = f;
          row.append(document.createTextNode('   '), nm,
                     el('span', 'loc', `  ${fmt(n)} call site${n === 1 ? '' : 's'}`));
          kids.append(row);
        });
        const fnHead = el('div', 'loc', 'function pairs:');
        fnHead.style.marginTop = '4px';
        kids.append(fnHead);
      }
      detail.forEach((d, j) => {
        const [caller, callee, file, ln, n] = d;
        const row = el('div');
        const dlast = j === detail.length - 1;
        row.append(document.createTextNode(dlast ? '\u2514\u2500 ' : '\u251c\u2500 '));
        const cf = el('span', null, caller); cf.style.color = 'var(--accent2)';
        const ce = el('span', null, callee); ce.style.color = 'var(--warn)';
        row.append(cf, document.createTextNode(' \u2192 '), ce);
        const where = el('span', 'loc',
          `  ${file ? file.split('/').pop() + ':' + ln : ''}${n > 1 ? `  (x${fmt(n)})` : ''}`);
        where.title = file ? `${file}:${ln}` : '';
        row.append(where);
        kids.append(row);
      });
      toggle.onclick = ev => {
        ev.stopPropagation();
        kids.hidden = !kids.hidden;
        toggle.textContent = kids.hidden ? '  functions' : '  hide';
      };
      pre.append(line, kids);
      return;
    }
    pre.append(line);
  });
  if (rows.length > shown.length) pre.append(el('div', 'loc', `... ${rows.length - shown.length} more`));
  c.append(pre);
  return c;
}

function edgeList(title, rows, pick, color) {
  const c = el('div');
  const h = el('h2', null, `${title}: ${rows.length}`); h.style.color = color;
  c.append(h);
  if (!rows.length) { c.append(el('div', 'empty', '(none)')); return c; }
  const tb = el('table'); const body = el('tbody');
  for (const l of rows.slice(0, 20)) {
    const other = modState.names[pick(l)];
    const tr = el('tr');
    const td = el('td');
    const a = el('a', null, other);
    a.style.color = 'var(--accent2)'; a.style.cursor = 'pointer';
    a.onclick = () => selectModule(modState.idx.get(other));
    td.append(a);
    const bar = el('td');
    const b = el('span', 'bar');
    b.style.width = Math.max(2, 90 * l.n / rows[0].n) + 'px';
    b.style.background = color;
    bar.append(b);
    tr.append(td, el('td', 'num', fmt(l.n)), bar);
    body.append(tr);
  }
  tb.append(body); c.append(tb);
  if (rows.length > 20) c.append(el('div', 'loc', `... ${rows.length - 20} more`));
  return c;
}

function simulate(nodes, links, w, h, cy, iters = 260, repel = 2600, dist = 120) {
  for (let it = 0; it < iters; it++) {
    const k = 1 - it / iters;
    for (const l of links) {
      const a = nodes[l.s], b = nodes[l.t];
      let dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      const f = (d - dist) * 0.012 * k * Math.min(1, l.n / 40 + .3);
      dx /= d; dy /= d;
      a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
    }
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy || 1;
      const f = repel * k / d2;
      const d = Math.sqrt(d2); dx /= d; dy /= d;
      a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
    }
    for (const n of nodes) {
      n.vx += (w / 2 - n.x) * 0.004 * k; n.vy += (cy - n.y) * 0.004 * k;
      n.x += n.vx * .5; n.y += n.vy * .5; n.vx *= .82; n.vy *= .82;
      n.x = Math.max(60, Math.min(w - 60, n.x)); n.y = Math.max(24, Math.min(h - 24, n.y));
    }
  }
}

/* ---------- symbol explorer ---------- */
let selected = null;
function symbolsTab() {
  const s = document.getElementById('symbols');
  if (s.dataset.init) return;
  s.dataset.init = '1';
  s.innerHTML = `<div class="explorer">
    <div class="card">
      <div class="filters">
        <input type="text" id="q" placeholder="filter by name (substring or /regex/)">
        <select id="kind"></select>
        <select id="module"></select>
        <select id="sort">
          <option value="deg">sort: degree</option>
          <option value="ci">sort: calls in</option>
          <option value="co">sort: calls out</option>
          <option value="rc">sort: references</option>
          <option value="name">sort: name</option>
        </select>
      </div>
      <div id="count" class="loc"></div>
      <div class="rows" id="rows"></div>
    </div>
    <div class="card" id="detail"><div class="empty">select a symbol</div></div>
  </div>`;
  const kinds = [...new Set(D.nodes.map(n => n.k))].sort();
  const mods = [...new Set(D.nodes.map(n => n.m).filter(Boolean))].sort();
  fill('kind', 'all kinds', kinds); fill('module', 'all modules', mods);
  for (const id of ['q', 'kind', 'module', 'sort'])
    document.getElementById(id).addEventListener('input', render);
  render();
}
function fill(id, label, vals) {
  const sel = document.getElementById(id);
  sel.innerHTML = `<option value="">${label}</option>` + vals.map(v => `<option>${v}</option>`).join('');
}
function setModule(m) { symbolsTab(); document.getElementById('module').value = m; render(); }
function render() {
  const q = document.getElementById('q').value.trim();
  const kind = document.getElementById('kind').value;
  const mod = document.getElementById('module').value;
  const sort = document.getElementById('sort').value;
  let re = null, sub = '';
  if (q.startsWith('/') && q.lastIndexOf('/') > 0) {
    try { re = new RegExp(q.slice(1, q.lastIndexOf('/')), 'i'); } catch (e) { re = null; }
  } else sub = q.toLowerCase();
  let list = [];
  for (let i = 0; i < D.nodes.length; i++) {
    const n = D.nodes[i];
    if (n.x) continue;
    if (kind && n.k !== kind) continue;
    if (mod && n.m !== mod) continue;
    if (re && !re.test(n.n)) continue;
    if (sub && !n.n.toLowerCase().includes(sub) && !(n.f || '').toLowerCase().includes(sub)) continue;
    list.push(i);
  }
  const key = { deg: n => -(n.i + n.o), ci: n => -n.ci, co: n => -n.co, rc: n => -n.rc,
                name: n => n.n.toLowerCase() }[sort];
  list.sort((a, b) => { const x = key(D.nodes[a]), y = key(D.nodes[b]); return x < y ? -1 : x > y ? 1 : 0; });
  document.getElementById('count').textContent =
    `${fmt(list.length)} of ${fmt(D.nodes.filter(n => !n.x).length)} sliced symbols` +
    (list.length > 400 ? ' (showing first 400)' : '');
  const box = document.getElementById('rows'); box.innerHTML = '';
  let group = null;
  for (const i of list.slice(0, 400)) {
    const n = D.nodes[i];
    const g = `${n.m || '?'} (${n.f || 'external'})`;
    if (g !== group) { group = g; box.append(el('div', 'group', g)); }
    const r = el('div', 'row'); r.dataset.i = i;
    r.append(el('span', 'nm', n.n), el('span', 'badge', n.k),
             el('span', 'meta', `:${n.l}  in=${n.i} out=${n.o} calls=${n.ci}/${n.co}`));
    r.onclick = () => select(i);
    if (i === selected) r.classList.add('sel');
    box.append(r);
  }
}
function select(i) {
  if (i < 0 || !D.nodes[i]) return;
  selected = i;
  for (const r of document.querySelectorAll('.row')) r.classList.toggle('sel', +r.dataset.i === i);
  const n = D.nodes[i];
  const d = document.getElementById('detail'); d.innerHTML = '';
  const t = el('h2', null, n.n); t.style.color = 'var(--accent)'; t.style.fontSize = '13px'; d.append(t);
  const kv = el('div', 'kv');
  const pairs = [['kind', n.k], ['module', n.m || '(none)'], ['file', n.f ? `${n.f}:${n.l}` : 'external'],
    ['degree', `in ${fmt(n.i)} / out ${fmt(n.o)}`], ['calls', `in ${fmt(n.ci)} / out ${fmt(n.co)}`],
    ['occurrences', fmt(n.rc)]];
  for (const [k, v] of pairs) { kv.append(el('div', null, k), el('div', null, v)); }
  d.append(kv);
  d.append(tree('callers (inbound)', inn.get(i) || []));
  d.append(tree('callees (outbound)', out.get(i) || []));
  d.append(neighborhood(i));
}
function tree(title, rows) {
  const c = el('div');
  c.append(el('h2', null, `${title}: ${rows.length}`));
  if (!rows.length) { c.append(el('div', 'empty', '(none)')); return c; }
  const agg = new Map();
  for (const [o, k, f, l, n] of rows) {
    const key = o + '|' + k;
    if (!agg.has(key)) agg.set(key, { o, k, site: f ? `${f}:${l}` : '', n: 0 });
    agg.get(key).n += n || 1;
  }
  const arr = [...agg.values()].sort((a, b) => b.n - a.n).slice(0, 40);
  const pre = el('div', 'tree');
  arr.forEach((r, idx) => {
    const last = idx === arr.length - 1;
    const nd = D.nodes[r.o];
    const line = el('div');
    line.append(document.createTextNode((last ? '└─ ' : '├─ ')));
    const a = el('a', null, nd.n); a.onclick = () => select(r.o); line.append(a);
    const short = r.site ? r.site.split('/').pop() : '';
    const tail = el('span', 'loc', `  ${nd.k} <${r.k}>  ${short}` + (r.n > 1 ? ` (x${r.n})` : ''));
    tail.title = r.site;
    line.append(tail);
    pre.append(line);
  });
  if (agg.size > 40) pre.append(el('div', 'loc', `... ${agg.size - 40} more`));
  c.append(pre);
  return c;
}
function neighborhood(i) {
  const c = el('div');
  c.append(el('h2', null, 'neighbourhood'));
  const ids = new Set([i]);
  for (const [o] of (inn.get(i) || []).slice(0, 14)) ids.add(o);
  for (const [o] of (out.get(i) || []).slice(0, 14)) ids.add(o);
  const arr = [...ids];
  const pos = new Map(arr.map((id, k) => [id, k]));
  const nodes = arr.map((id, k) => ({ id, n: D.nodes[id].n, r: id === i ? 9 : 5,
    x: 500 + (id === i ? 0 : 300 * Math.cos(2 * Math.PI * k / arr.length)),
    y: 200 + (id === i ? 0 : 150 * Math.sin(2 * Math.PI * k / arr.length)), vx: 0, vy: 0 }));
  const links = [];
  for (const [a, b] of D.edges.map(e => [e[0], e[1]]))
    if (pos.has(a) && pos.has(b) && a !== b) links.push({ s: pos.get(a), t: pos.get(b), n: 10 });
  simulate(nodes, links, 1000, 400, 200, 180);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 1000 400');
  for (const l of links) {
    const ln = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    ln.setAttribute('x1', nodes[l.s].x); ln.setAttribute('y1', nodes[l.s].y);
    ln.setAttribute('x2', nodes[l.t].x); ln.setAttribute('y2', nodes[l.t].y);
    ln.setAttribute('stroke-opacity', .5); svg.append(ln);
  }
  for (const nd of nodes) {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    const ci = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    ci.setAttribute('cx', nd.x); ci.setAttribute('cy', nd.y); ci.setAttribute('r', nd.r);
    ci.setAttribute('fill', nd.id === i ? 'var(--warn)' : 'var(--accent2)');
    ci.setAttribute('fill-opacity', .3);
    ci.setAttribute('stroke', nd.id === i ? 'var(--warn)' : 'var(--accent2)');
    const tx = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    tx.setAttribute('x', nd.x + nd.r + 3); tx.setAttribute('y', nd.y + 3); tx.textContent = nd.n;
    g.append(ci, tx); g.style.cursor = 'pointer'; g.onclick = () => select(nd.id);
    svg.append(g);
  }
  c.append(svg);
  return c;
}

/* ---------- dead code ---------- */
function deadTab() {
  const s = document.getElementById('dead');
  if (s.dataset.init) return;
  s.dataset.init = '1';
  const cov = D.meta.coverage ? `${D.meta.coverage} tracked sources carry index records` +
    (D.meta.coverage_pct ? ` (${D.meta.coverage_pct}%)` : '') : 'coverage unknown';
  const warn = el('div', 'card');
  warn.style.borderColor = 'var(--warn)';
  warn.append(el('h2', null, 'read this first'));
  const p = el('div');
  p.innerHTML = `These are symbols that nothing in <em>the indexed build</em> reaches: no call,
    no reference, no override, and no occurrence beyond their own definition. Structural
    edges are ignored, and members of types conforming to Codable, Equatable, View and
    friends are excluded because the compiler synthesises their uses.
    <br><br>This is a candidate list, not a verdict. ${cov}, so a symbol used only from
    files the build never compiled appears here wrongly. Confirm with
    <code>idxg dead --verify</code>, which drops any candidate whose identifier appears in
    another file.`;
  warn.append(p);
  s.append(warn);

  const byKind = {}, byModule = {};
  for (const [, kind, mod] of D.dead) {
    byKind[kind] = (byKind[kind] || 0) + 1;
    if (mod) byModule[mod] = (byModule[mod] || 0) + 1;
  }
  const tiles = el('div', 'tiles');
  for (const [k, v] of [['candidates', D.dead_total], ['listed here', D.dead.length],
                        ['kinds', Object.keys(byKind).length],
                        ['modules', Object.keys(byModule).length]]) {
    const d = el('div', 'tile'); d.append(el('div', 'n', fmt(v)), el('div', 'k', k)); tiles.append(d);
  }
  s.append(tiles);

  const g = el('div', 'grid2');
  g.append(barCard('candidates by kind', Object.entries(byKind).sort((a, b) => b[1] - a[1])));
  g.append(barCard('candidates by module',
                   Object.entries(byModule).sort((a, b) => b[1] - a[1]).slice(0, 20)));
  s.append(g);

  const list = el('div', 'card');
  list.append(el('h2', null, `candidates (${D.dead.length} shown of ${fmt(D.dead_total)})`));
  const filters = el('div', 'filters');
  filters.innerHTML = `<input type="text" id="deadq" placeholder="filter by name, module or path">
    <select id="deadkind"></select>`;
  list.append(filters);
  const rows = el('div', 'rows'); rows.id = 'deadrows';
  list.append(rows);
  s.append(list);
  const sel = document.getElementById('deadkind');
  sel.innerHTML = '<option value="">all kinds</option>' +
    Object.keys(byKind).sort().map(k => `<option>${k}</option>`).join('');
  document.getElementById('deadq').addEventListener('input', renderDead);
  sel.addEventListener('input', renderDead);
  renderDead();
}

function renderDead() {
  const q = (document.getElementById('deadq').value || '').toLowerCase();
  const kind = document.getElementById('deadkind').value;
  const box = document.getElementById('deadrows');
  box.innerHTML = '';
  let group = null, n = 0;
  for (const [name, k, mod, file, line] of D.dead) {
    if (kind && k !== kind) continue;
    if (q && !(name.toLowerCase().includes(q) || (mod || '').toLowerCase().includes(q) ||
               (file || '').toLowerCase().includes(q))) continue;
    if (n++ > 400) break;
    const g = `${mod || '?'} (${file || 'external'})`;
    if (g !== group) { group = g; box.append(el('div', 'group', g)); }
    const row = el('div', 'row');
    row.append(el('span', 'nm', name), el('span', 'badge', k),
               el('span', 'meta', `:${line}`));
    row.onclick = () => { showTab('symbols'); document.getElementById('q').value = name; render(); };
    box.append(row);
  }
  if (!n) box.append(el('div', 'empty', 'nothing matches'));
}

/* ---------- history ---------- */
const H = D.history;
function historyTab() {
  const s = document.getElementById('history');
  if (s.dataset.init) return;
  s.dataset.init = '1';
  if (!H) {
    const c = el('div', 'card');
    c.append(el('h2', null, 'no history yet'));
    const p = el('div', 'prose');
    p.textContent = 'Run idxg history build to extract the main branch’s git log and the repository’s markdown docs, then idxg viz to include them here.';
    c.append(p); s.append(c); return;
  }
  const m = H.meta;
  const web = m.remote_web || '';
  const tiles = el('div', 'tiles');
  for (const [k, v] of [['commits on ' + (m.branch || 'main'), +m.count_commits],
                        ['authors', +m.authors], ['first commit', m.first_day], ['last commit', m.last_day],
                        ['repo docs', +m.count_docs]]) {
    const d = el('div', 'tile');
    d.append(el('div', 'n', typeof v === 'number' ? fmt(v) : v), el('div', 'k', k)); tiles.append(d);
  }
  s.append(tiles);
  s.append(weeklyCard());
  s.append(narratedCard());

  const story = el('div', 'card');
  story.append(el('h2', null, 'the story so far'));
  const intro = el('div', 'prose');
  const ip = el('p'); ip.textContent = H.overview.replace(/`/g, ''); intro.append(ip);
  story.append(intro);
  story.append(activityChart());
  const hint = el('div', 'loc', `one paragraph per ${H.granularity}, newest first; click a heading to see that period's largest changes`);
  hint.style.margin = '10px 0';
  story.append(hint);
  const eras = el('div');
  const byPeriod = new Map(H.eras.map(e => [e.period, e]));
  for (const p of [...H.narrative].reverse()) {
    const e = byPeriod.get(p.period);
    const box = el('div', 'era');
    const h = el('h3', null, p.label);
    const facts = el('div', 'facts');
    for (const f of [`${fmt(e.commits)} commits`, `${fmt(e.authors)} authors`, `+${fmt(e.ins)} / -${fmt(e.del)} lines`,
                     e.modules.length ? `top module ${e.modules[0][0]}` : null]) if (f) facts.append(el('span', null, f));
    const txt = el('div', 'prose'); const tp = el('p'); tp.textContent = p.text; txt.append(tp);
    const more = el('div'); more.hidden = true;
    if (e.biggest.length) {
      const t = el('table'); const tb = el('tbody');
      for (const b of e.biggest) {
        const tr = el('tr');
        tr.append(el('td', 'sha', b.sha), el('td', 'subj', b.subject), numTd(b.files), numTd(b.lines));
        tb.append(tr);
      }
      t.innerHTML = '<thead><tr><th>largest changes</th><th></th><th class="num">files</th><th class="num">lines</th></tr></thead>';
      t.append(tb); more.append(t);
    }
    if (e.modules.length) more.append(el('div', 'loc', 'modules by commits: ' + e.modules.map(([n, c]) => `${n} (${c})`).join(', ')));
    if (e.born.length) more.append(el('div', 'loc', 'first seen: ' + e.born.map(b => b[1]).join(', ')));
    if (e.died.length) more.append(el('div', 'loc', 'gone since: ' + e.died.map(b => b[1]).join(', ')));
    h.onclick = () => { more.hidden = !more.hidden; box.classList.toggle('on', !more.hidden); };
    box.append(h, facts, txt, more);
    eras.append(box);
  }
  story.append(eras);
  s.append(story);


  const g = el('div', 'grid2');
  const ch = el('div', 'card');
  ch.append(el('h2', null, `change by module since ${H.churn_since}`));
  ch.append(el('div', 'loc', 'modules come from the compiled index; uncompiled files are counted nowhere here'));
  const t = el('table');
  t.innerHTML = '<thead><tr><th>module</th><th class="num">commits</th><th class="num">+lines</th><th class="num">-lines</th><th class="num">authors</th><th>last</th></tr></thead>';
  const tb = el('tbody');
  for (const [key, commits, ins, del_, authors, last] of H.churn) {
    const tr = el('tr'); const td = el('td');
    const a = el('a', null, key); a.style.cursor = 'pointer'; a.style.color = 'var(--accent2)';
    a.onclick = () => { showTab('symbols'); setModule(key); };
    td.append(a); tr.append(td, numTd(commits), numTd(ins), numTd(del_), numTd(authors), el('td', 'loc', last));
    tb.append(tr);
  }
  t.append(tb); ch.append(t); g.append(ch);

  const rc = el('div', 'card');
  rc.append(el('h2', null, `recent changes on ${m.branch || 'main'}`));
  const rows = el('div', 'rows');
  for (const [sha, short, author, day, subject, pr, tickets, files, ins, del_] of H.recent) {
    const r = el('div', 'row'); r.style.cursor = 'default'; r.style.flexWrap = 'wrap';
    r.append(el('span', 'sha', day), el('span', 'subj', subject));
    const meta = el('span', 'meta');
    meta.append(document.createTextNode(`${author}  ${files} files +${fmt(ins)} -${fmt(del_)} `));
    if (pr) {
      if (web) { const a = el('a', 'ext', `#${pr}`); a.href = `${web}/pull/${pr}`; a.target = '_blank'; meta.append(a); }
      else meta.append(document.createTextNode(`#${pr}`));
    }
    r.append(meta); rows.append(r);
  }
  rc.append(rows); g.append(rc);
  s.append(g);

  const comps = el('div', 'card');
  const alive = H.components.filter(c => c[4]), gone = H.components.filter(c => !c[4]);
  comps.append(el('h2', null, `directories at module depth: ${alive.length} present, ${gone.length} gone (3+ commits each)`));
  const ct = el('table');
  ct.innerHTML = '<thead><tr><th>directory</th><th>first</th><th>last</th><th class="num">commits</th><th>state</th><th>module</th></tr></thead>';
  const cb = el('tbody');
  for (const [comp, first, last, commits, isAlive, mod] of H.components.slice(0, 120)) {
    const tr = el('tr');
    tr.append(el('td', null, comp), el('td', 'loc', first), el('td', 'loc', last), numTd(commits),
              el('td', isAlive ? 'loc' : 'subj', isAlive ? 'present' : 'gone'), el('td', 'loc', mod || ''));
    if (!isAlive) tr.style.color = 'var(--warn)';
    cb.append(tr);
  }
  ct.append(cb); comps.append(ct);
  if (H.components.length > 120) comps.append(el('div', 'loc', `... ${H.components.length - 120} more; idxg sql against the history db lists them all`));
  s.append(comps);
}

function weeklyCard() {
  const card = el('div', 'card');
  const head = el('div');
  head.style.display = 'flex'; head.style.alignItems = 'baseline'; head.style.gap = '12px'; head.style.flexWrap = 'wrap';
  head.append(el('h2', null, 'week by week'));
  const wk = (H.weeks || []);
  if (!wk.length) { card.append(head, el('div', 'empty', 'no weeks to show')); return card; }
  const sel = el('select'); sel.style.width = 'auto';
  for (const w of [...wk].reverse()) {
    const o = el('option', null, `${w.week}  ${w.digest.window.start} to ${w.digest.window.end}  (${w.commits} changes)`);
    o.value = w.week; sel.append(o);
  }
  const hint = el('span', 'loc', `${wk.length} most recent weeks, every change narrated; the same layout idxg history digest --html writes`);
  head.append(sel, hint);
  card.append(head);
  const frame = document.createElement('iframe');
  frame.style.width = '100%'; frame.style.border = '1px solid var(--line)'; frame.style.borderRadius = '6px';
  frame.style.height = '900px'; frame.style.background = 'transparent';
  card.append(frame);
  const byWeek = new Map(wk.map(w => [w.week, w]));
  const show = () => {
    const w = byWeek.get(sel.value);
    const theme = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
    const view = Object.assign({}, w.digest); delete view.window;
    const page = H.template.replace('{{TITLE}}', `${w.digest.eyebrow[0]} digest, ${w.digest.window.label}`)
      .replace('{{DIGEST_JSON}}', JSON.stringify(view).replace(/<\\//g, '<\\\\/'));
    frame.srcdoc = `<!doctype html><html data-theme="${theme}"><head><meta charset="utf-8"></head><body>${page}</body></html>`;
  };
  frame.onload = () => {
    try {
      const doc = frame.contentDocument;
      frame.style.height = Math.min(6000, doc.documentElement.scrollHeight + 20) + 'px';
      doc.body.addEventListener('click', () => setTimeout(() => {
        frame.style.height = Math.min(6000, doc.documentElement.scrollHeight + 20) + 'px'; }, 30));
    } catch (e) {}
  };
  sel.addEventListener('input', show);
  document.getElementById('themeToggle').addEventListener('click', () => setTimeout(show, 0));
  show();
  return card;
}

function narratedCard() {
  const card = el('div', 'card');
  card.append(el('h2', null, `commit by commit, most recent ${(H.recent_narrated || []).length}`));
  card.append(el('div', 'loc', 'each paragraph is computed from the commit, its pull request description and its files; older commits: idxg history log --narrate'));
  const box = el('div', 'rows'); box.style.maxHeight = '60vh';
  for (const c of (H.recent_narrated || [])) {
    const row = el('div', 'row'); row.style.cursor = 'default'; row.style.display = 'block';
    const top = el('div');
    top.append(el('span', 'sha', `${c.day}  ${c.short}  `));
    if (c.url) { const a = el('a', 'ext', `#${c.pr}`); a.href = c.url; a.target = '_blank'; top.append(a); }
    const p = el('div', 'prose'); p.style.fontSize = '13px'; p.style.maxWidth = 'none';
    p.textContent = c.text;
    row.append(top, p); box.append(row);
  }
  card.append(box);
  return card;
}

function activityChart() {
  const months = H.monthly;
  const W = 1200, Hh = 160, pad = 28;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'chart'); svg.setAttribute('viewBox', `0 0 ${W} ${Hh}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  if (!months.length) return svg;
  const max = Math.max(...months.map(m => m[1]));
  const bw = (W - 2 * pad) / months.length;
  const lastYear = months[months.length - 1][0].slice(0, 4);
  months.forEach(([month, n, authors], i) => {
    const h = Math.max(1, (Hh - 30) * n / max);
    const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    r.setAttribute('x', pad + i * bw); r.setAttribute('y', Hh - 18 - h);
    r.setAttribute('width', Math.max(1, bw - 1)); r.setAttribute('height', h);
    if (month.slice(0, 4) === lastYear) r.setAttribute('class', 'hi');
    const tt = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    tt.textContent = `${month}: ${fmt(n)} commits, ${authors} authors`;
    r.append(tt); svg.append(r);
    if (month.endsWith('-01')) {
      const tx = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      tx.setAttribute('x', pad + i * bw); tx.setAttribute('y', Hh - 5); tx.textContent = month.slice(0, 4);
      svg.append(tx);
    }
  });
  const lab = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  lab.setAttribute('x', pad); lab.setAttribute('y', 12); lab.textContent = `commits per month, peak ${fmt(max)}`;
  svg.append(lab);
  return svg;
}

/* ---------- docs ---------- */
function docsTab() {
  const s = document.getElementById('docs');
  if (s.dataset.init) return;
  s.dataset.init = '1';
  if (!H || !H.docs.length) {
    const c = el('div', 'card');
    c.append(el('h2', null, 'no docs indexed'));
    const p = el('div', 'prose');
    p.textContent = 'idxg history build collects the repository’s tracked markdown files; none are recorded yet.';
    c.append(p); s.append(c); return;
  }
  const web = H.meta.remote_web || '', branch = H.meta.branch || 'main';
  const card = el('div', 'card');
  card.append(el('h2', null, `${fmt(H.docs.length)} markdown docs tracked in the repository`));
  card.append(el('div', 'loc', 'date is the file’s last commit on the history branch, not the date its content is true; search their full text with idxg docs search'));
  const filters = el('div', 'filters');
  filters.innerHTML = `<input type="text" id="docq" placeholder="filter by path, title or module"><select id="dockind"></select>`;
  card.append(filters);
  const rows = el('div', 'rows'); rows.id = 'docrows'; card.append(rows);
  s.append(card);
  const kinds = [...new Set(H.docs.map(d => d[2]))].sort();
  document.getElementById('dockind').innerHTML = '<option value="">all kinds</option>' + kinds.map(k => `<option>${k}</option>`).join('');
  const render = () => {
    const q = document.getElementById('docq').value.toLowerCase();
    const kind = document.getElementById('dockind').value;
    rows.innerHTML = ''; let group = null, n = 0;
    for (const [path, title, k, mod, published, bytes] of H.docs) {
      if (kind && k !== kind) continue;
      if (q && !(path.toLowerCase().includes(q) || (title || '').toLowerCase().includes(q) || (mod || '').toLowerCase().includes(q))) continue;
      n++;
      const g = mod || k;
      if (g !== group) { group = g; rows.append(el('div', 'group', g)); }
      const r = el('div', 'row'); r.style.cursor = 'default';
      const nm = web ? el('a', 'ext', title || path) : el('span', 'nm', title || path);
      if (web) { nm.href = `${web}/blob/${branch}/${path}`; nm.target = '_blank'; }
      nm.title = path;
      r.append(nm, el('span', 'badge', k), el('span', 'meta', `${published || 'undated'}  ${Math.max(1, Math.round(bytes / 1024))}k  ${path}`));
      rows.append(r);
    }
    if (!n) rows.append(el('div', 'empty', 'nothing matches'));
  };
  document.getElementById('docq').addEventListener('input', render);
  document.getElementById('dockind').addEventListener('input', render);
  render();
}

/* ---------- tabs ---------- */
function showTab(name) {
  for (const b of document.querySelectorAll('nav button')) b.classList.toggle('on', b.dataset.tab === name);
  for (const id of ['overview', 'modules', 'symbols', 'dead', 'history', 'docs'])
    document.getElementById(id).hidden = id !== name;
  if (name === 'modules' && !modState) modulesTab();
  if (name === 'symbols') symbolsTab();
  if (name === 'dead') deadTab();
  if (name === 'history') historyTab();
  if (name === 'docs') docsTab();
}
for (const b of document.querySelectorAll('nav button')) b.onclick = () => showTab(b.dataset.tab);
document.getElementById('themeToggle').onclick = () => {
  const r = document.documentElement;
  r.dataset.theme = r.dataset.theme === 'light' ? 'dark' : 'light';
};
(() => {
  const p = document.getElementById('storePath');
  const parts = (p.title || '').split(' ; ');
  p.textContent = parts.length > 1 ? `${parts.length} index stores` : parts[0].split('/').slice(-3).join('/');
})();
overview();
</script>
"""


def attach_history(data, graph_db_path, weeks=26):
    """Add the history slice when the project has one; the tab explains itself otherwise."""
    import history
    try:
        data["history"] = history.slice_for_viz(history.history_db_for(graph_db_path), weeks_limit=weeks)
    except Exception:
        data["history"] = None
    return data


def render(data, out_path, title=None):
    project = data["meta"].get("project", "index-store graph")
    head = TEMPLATE_HEAD.replace("__TITLE__", title or f"{project} graph")
    data.setdefault("history", None)
    h = data["history"]
    hist_note = (f"{int(h['meta'].get('count_commits') or 0):,} commits to {h['meta'].get('last_day', '')}"
                 if h else "not built")
    body = (BODY
            .replace("__HISTORY__", hist_note)
            .replace("__PROJECT__", project)
            .replace("__STORE__", data["meta"].get("store_path", ""))
            .replace("__BUILT__", data["meta"].get("built_at", ""))
            .replace("__FMT__", data["meta"].get("format_version", "?"))
            .replace("__SLICE__", f"{data['slice_size']:,}")
            .replace("__EDGECOUNT__", f"{len(data['edges']):,}")
            # The history slice carries the digest template, which ends in "</script>"; left
            # unescaped inside the JSON it would close the page's own script element early.
            .replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/")))
    with open(out_path, "w") as f:
        f.write(head + body)
    return out_path
