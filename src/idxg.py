#!/usr/bin/env python3
"""idxg: query a Swift/clang index-store knowledge graph (codebase-memory shaped)."""
import argparse, json, os, re, sqlite3, sys, textwrap

KIND_BOOST = {"Function": 10, "InstanceMethod": 10, "ClassMethod": 10, "StaticMethod": 10,
              "Constructor": 8, "Class": 5, "Struct": 5, "Protocol": 5, "Enum": 5, "Extension": 3}
EDGE_KINDS = ["CALLS", "REFERENCES", "CONTAINS", "INHERITS", "OVERRIDES", "EXTENDS",
              "ACCESSOR_OF", "RECEIVED_BY", "SPECIALIZES", "IB_TYPE_OF"]
ROLE_BITS = [(1, "declaration"), (2, "definition"), (4, "reference"), (8, "read"), (16, "write"),
             (32, "call"), (64, "dynamic"), (128, "addressof"), (256, "implicit")]


def path_layer(rel, depth=2):
    """Group a repo-relative path into a layer: its first `depth` segments."""
    if not rel:
        return "(external)"
    parts = rel.split("/")
    if len(parts) <= depth:
        return parts[0]
    return "/".join(parts[:depth])


def db_path(arg):
    if arg:
        return os.path.expanduser(arg)
    slug = os.path.basename(os.path.realpath(os.getcwd()))
    p = os.path.expanduser(f"~/.cache/indexstore-graph/{slug}.db")
    if os.path.exists(p):
        return p
    cands = [f for f in os.listdir(os.path.expanduser("~/.cache/indexstore-graph")) if f.endswith(".db")]
    if len(cands) == 1:
        return os.path.expanduser(f"~/.cache/indexstore-graph/{cands[0]}")
    raise SystemExit(f"no graph for {slug}; build one with idxg build, or pass --db")


def connect(arg, write=False):
    p = db_path(arg)
    uri = f"file:{p}" + ("" if write else "?mode=ro")
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.create_function("regexp", 2, lambda pat, val: 1 if val is not None and re.search(pat, val) else 0)
    return db


def meta(db):
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}


def roles_str(mask):
    return "|".join(n for b, n in ROLE_BITS if mask & b) or str(mask)


def rel(db, path_hash, cache={}):
    if path_hash in cache:
        return cache[path_hash]
    r = db.execute("SELECT COALESCE(rel, path) AS p FROM files WHERE path_hash = ?", (path_hash,)).fetchone()
    cache[path_hash] = r["p"] if r else "?"
    return cache[path_hash]


def qname(db, usr_hash, depth=6):
    """Qualified name by walking CONTAINS parents."""
    parts, seen, cur = [], set(), usr_hash
    for _ in range(depth):
        row = db.execute("SELECT name FROM symbols WHERE usr_hash = ?", (cur,)).fetchone()
        if not row:
            break
        parts.append(row["name"])
        p = db.execute("""SELECT e.src FROM edges e WHERE e.dst = ? AND e.kind = 'CONTAINS' LIMIT 1""",
                       (cur,)).fetchone()
        if not p or p["src"] in seen:
            break
        seen.add(p["src"]); cur = p["src"]
    mod = db.execute("SELECT module FROM symbols WHERE usr_hash = ?", (usr_hash,)).fetchone()
    head = [mod["module"]] if mod and mod["module"] else []
    return ".".join(head + list(reversed(parts)))


def resolve(db, ident, kind=None, limit=25):
    """Resolve a name / qualified name / USR to symbol rows."""
    if ident.startswith(("s:", "c:")):
        rows = db.execute("SELECT * FROM symbols WHERE usr = ?", (ident,)).fetchall()
        if rows:
            return rows
    name, module = ident, None
    if "." in ident:
        module, name = ident.rsplit(".", 1)
    q = "SELECT * FROM symbols WHERE name = ?"
    args = [name]
    if module:
        q += " AND (module = ? OR module LIKE ?)"
        args += [module, f"%{module}%"]
    if kind:
        q += " AND kind IN (%s)" % ",".join("?" * len(kind.split(",")))
        args += kind.split(",")
    q += " ORDER BY in_repo DESC, (in_deg + out_deg) DESC LIMIT ?"
    args.append(limit)
    rows = db.execute(q, args).fetchall()
    if not rows:
        rows = db.execute("""SELECT * FROM symbols WHERE name LIKE ? ORDER BY in_repo DESC,
                             (in_deg+out_deg) DESC LIMIT ?""", (f"{name}%", limit)).fetchall()
    return rows


def sym_line(db, r, show_qn=True):
    loc = f"{rel(db, r['def_path_hash'])}:{r['def_line']}" if r["def_path_hash"] else "(no def site)"
    nm = qname(db, r["usr_hash"]) if show_qn else r["name"]
    return f"{nm}  {r['kind']}/{r['lang']}  {loc}  in={r['in_deg']} out={r['out_deg']} refs={r['ref_count']}"


# ---------------------------------------------------------------- commands
def cmd_status(a):
    db = connect(a.db)
    m = meta(db)
    counts = {t: db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("symbols", "edges", "occurrences", "defs", "files", "units")}
    per_kind = db.execute("""SELECT kind, COUNT(*) c FROM edges GROUP BY kind ORDER BY c DESC""").fetchall()
    in_repo = db.execute("SELECT COUNT(*) c FROM files WHERE in_repo = 1").fetchone()["c"]
    swift = db.execute("SELECT COUNT(*) c FROM symbols WHERE lang='Swift'").fetchone()["c"]
    root = m.get("repo_root", "")
    tracked = covered = 0
    if os.path.isdir(root):
        import subprocess
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", "*.swift", "*.m", "*.h", "*.mm",
                              "*.c", "*.cpp"], capture_output=True, text=True).stdout.split("\0")
        out = [f for f in out if f]
        tracked = len(out)
        have = {r["rel"] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1")}
        covered = sum(1 for f in out if f in have)
    if a.json:
        print(json.dumps({"meta": m, "counts": counts, "edges_by_kind": {r["kind"]: r["c"] for r in per_kind},
                          "files_in_repo": in_repo, "swift_symbols": swift,
                          "coverage": {"tracked_sources": tracked, "covered": covered,
                                       "pct": round(100 * covered / tracked, 1) if tracked else None}}, indent=2))
        return
    print(f"project:   {m.get('project')}")
    print(f"repo root: {m.get('repo_root')}")
    print(f"store:     {m.get('store_path')}")
    print(f"built:     {m.get('built_at')}  (format v{m.get('format_version')}, {m.get('build_seconds')}s)")
    print(f"db:        {db_path(a.db)}  ({os.path.getsize(db_path(a.db))/1e9:.2f} GB)")
    print("\ncounts")
    for k, v in counts.items():
        print(f"  {k:<12} {v:>10,}")
    print(f"  {'swift syms':<12} {swift:>10,}")
    print(f"  {'files in repo':<12} {in_repo:>10,}")
    print("\nedges by kind")
    for r in per_kind:
        print(f"  {r['kind']:<12} {r['c']:>10,}")
    if tracked:
        print(f"\ncoverage: {covered:,}/{tracked:,} tracked sources have index records "
              f"({100*covered/tracked:.1f}%)")
        print("  a file with no records was never compiled in the indexed build; grep it instead.")


def cmd_coverage(a):
    db = connect(a.db)
    m = meta(db)
    root = m.get("repo_root", "")
    out = []
    for p in a.paths:
        rp = os.path.relpath(os.path.realpath(p), root) if os.path.isabs(p) else p
        if os.path.isdir(os.path.join(root, rp)):
            rows = db.execute("""SELECT rel, (SELECT COUNT(*) FROM symbols s WHERE s.def_path_hash = f.path_hash) n
                                 FROM files f WHERE in_repo = 1 AND rel LIKE ?""", (rp.rstrip("/") + "/%",)).fetchall()
            import subprocess
            tracked = subprocess.run(["git", "-C", root, "ls-files", "-z", rp],
                                     capture_output=True, text=True).stdout.split("\0")
            src = [f for f in tracked if f.endswith((".swift", ".m", ".h", ".mm", ".c", ".cpp"))]
            have = {r["rel"] for r in rows}
            miss = [f for f in src if f not in have]
            out.append({"scope": rp, "kind": "dir", "sources": len(src), "covered": len(src) - len(miss),
                        "missing": miss[:40], "missing_count": len(miss)})
        else:
            r = db.execute("""SELECT path_hash, rel FROM files WHERE rel = ? OR path LIKE ?""",
                           (rp, f"%{rp}")).fetchone()
            if not r:
                out.append({"scope": rp, "kind": "file", "covered": False,
                            "reason": "no index record: file not compiled in the indexed build"})
            else:
                n = db.execute("SELECT COUNT(*) c FROM symbols WHERE def_path_hash = ?", (r["path_hash"],)).fetchone()["c"]
                occ = db.execute("SELECT COUNT(*) c FROM occurrences WHERE path_hash = ?", (r["path_hash"],)).fetchone()["c"]
                out.append({"scope": r["rel"], "kind": "file", "covered": True, "definitions": n, "occurrences": occ})
    if a.json:
        print(json.dumps(out, indent=2)); return
    for o in out:
        if o["kind"] == "file":
            if o["covered"]:
                print(f"COVERED  {o['scope']}  defs={o['definitions']} occurrences={o['occurrences']}")
            else:
                print(f"MISSING  {o['scope']}  {o['reason']}")
        else:
            print(f"DIR      {o['scope']}  {o['covered']}/{o['sources']} sources covered")
            for f in o["missing"]:
                print(f"           missing: {f}")
            if o["missing_count"] > len(o["missing"]):
                print(f"           ... {o['missing_count']-len(o['missing'])} more")
    print("\nnote: coverage reflects what the BSP build compiled. Absence is not proof a symbol does not exist.")


def cmd_search(a):
    db = connect(a.db)
    where, args = [], []
    if a.name:
        where.append("regexp(?, s.name)"); args.append(a.name)
    if a.kind:
        ks = a.kind.split(",")
        where.append("s.kind IN (%s)" % ",".join("?" * len(ks))); args += ks
    if a.module:
        where.append("s.module = ?"); args.append(a.module)
    if a.file:
        where.append("f.rel GLOB ?"); args.append(a.file)
    if a.lang:
        where.append("s.lang = ?"); args.append(a.lang)
    if a.min_degree:
        where.append("(s.in_deg + s.out_deg) >= ?"); args.append(a.min_degree)
    if a.max_degree is not None:
        where.append("(s.in_deg + s.out_deg) <= ?"); args.append(a.max_degree)
    if not a.all:
        where.append("s.in_repo = 1")
    base = """FROM symbols s LEFT JOIN files f ON f.path_hash = s.def_path_hash"""
    if a.query:
        base = """FROM symbols_fts x JOIN fts_map m ON m.rowid = x.rowid
                  JOIN symbols s ON s.usr_hash = m.usr_hash
                  LEFT JOIN files f ON f.path_hash = s.def_path_hash"""
        where.insert(0, "symbols_fts MATCH ?")
        args.insert(0, " OR ".join(a.query.split()))
        order = "bm25(symbols_fts) - (s.in_deg + s.out_deg) / 50.0"
    else:
        order = "-(s.in_deg + s.out_deg)"
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) c {base} {w}", args).fetchone()["c"]
    rows = db.execute(f"""SELECT s.*, f.rel {base} {w} ORDER BY {order} LIMIT ? OFFSET ?""",
                      args + [a.limit, a.offset]).fetchall()
    if a.json:
        print(json.dumps({"total": total, "returned": len(rows), "offset": a.offset,
                          "has_more": total > a.offset + len(rows),
                          "results": [{"qn": qname(db, r["usr_hash"]), "name": r["name"], "kind": r["kind"],
                                       "lang": r["lang"], "usr": r["usr"], "file": r["rel"], "line": r["def_line"],
                                       "in": r["in_deg"], "out": r["out_deg"], "calls_in": r["call_in"],
                                       "calls_out": r["call_out"], "refs": r["ref_count"], "module": r["module"]}
                                      for r in rows]}, indent=2))
        return
    print(f"total: {total}  returned: {len(rows)}  offset: {a.offset}")
    if a.detail == "ids":
        for r in rows:
            print(qname(db, r["usr_hash"]) + f"  [{r['usr']}]")
        print(f"has_more: {total > a.offset + len(rows)}")
        return
    group = None
    for r in rows:
        g = f"{r['module'] or '?'} ({r['rel'] or 'external'})"
        if g != group:
            group = g
            print(f"\n{group}:")
        loc = f":{r['def_line']}" if r["def_line"] else ""
        print(f"  {r['name']}  {r['kind']}{loc}  in={r['in_deg']} out={r['out_deg']} "
              f"calls={r['call_in']}/{r['call_out']} refs={r['ref_count']}")
        if a.usr:
            print(f"      {r['usr']}")
    print(f"\nhas_more: {total > a.offset + len(rows)}")


def _neighbors(db, uh, direction, kinds):
    ks = ",".join("?" * len(kinds))
    if direction == "in":
        q = f"""SELECT e.src AS other, e.kind, e.path_hash, e.line, s.name, s.kind AS skind, s.module,
                s.in_deg, s.out_deg FROM edges e JOIN symbols s ON s.usr_hash = e.src
                WHERE e.dst = ? AND e.kind IN ({ks}) GROUP BY e.src, e.kind, e.path_hash, e.line"""
    else:
        q = f"""SELECT e.dst AS other, e.kind, e.path_hash, e.line, s.name, s.kind AS skind, s.module,
                s.in_deg, s.out_deg FROM edges e JOIN symbols s ON s.usr_hash = e.dst
                WHERE e.src = ? AND e.kind IN ({ks}) GROUP BY e.dst, e.kind, e.path_hash, e.line"""
    return db.execute(q, [uh] + kinds).fetchall()


def cmd_trace(a):
    db = connect(a.db)
    kinds = a.kind.split(",")
    cands = resolve(db, a.symbol)
    if not cands:
        raise SystemExit(f"no symbol matched {a.symbol!r}; try idxg search --name '{a.symbol}'")
    if len(cands) > 1 and not a.first:
        print(f"{len(cands)} candidates for {a.symbol!r} (use --first or a USR):")
        for r in cands[:10]:
            print("  " + sym_line(db, r))
        return
    root = cands[0]
    directions = ["in", "out"] if a.direction == "both" else [a.direction]
    tree = {"symbol": qname(db, root["usr_hash"]), "usr": root["usr"], "kind": root["kind"],
            "file": rel(db, root["def_path_hash"]) if root["def_path_hash"] else None, "line": root["def_line"]}
    for d in directions:
        seen = set()

        def walk(uh, depth):
            if depth > a.depth or uh in seen:
                return []
            seen.add(uh)
            out = []
            rows = _neighbors(db, uh, d, kinds)
            agg = {}
            for r in rows:
                key = (r["other"], r["kind"])
                agg.setdefault(key, {"row": r, "sites": []})
                if r["path_hash"]:
                    agg[key]["sites"].append(f"{rel(db, r['path_hash'])}:{r['line']}")
            for (other, ekind), v in sorted(agg.items(), key=lambda kv: -len(kv[1]["sites"]))[:a.fanout]:
                r = v["row"]
                node = {"name": r["name"], "kind": r["skind"], "edge": ekind, "module": r["module"],
                        "sites": v["sites"][:3], "site_count": len(v["sites"]),
                        "children": walk(other, depth + 1)}
                out.append(node)
            return out

        tree[d] = walk(root["usr_hash"], 1)
    if a.json:
        print(json.dumps(tree, indent=2)); return
    loc = f"{tree['file']}:{tree['line']}" if tree["file"] else "external"
    print(f"{tree['symbol']}  [{tree['kind']}]  {loc}")
    print(f"edges: {','.join(kinds)}  depth: {a.depth}")

    def show(nodes, prefix=""):
        for i, n in enumerate(nodes):
            last = i == len(nodes) - 1
            branch = "└─ " if last else "├─ "
            site = f"  {n['sites'][0]}" if n["sites"] else ""
            extra = f" (x{n['site_count']})" if n["site_count"] > 1 else ""
            print(f"{prefix}{branch}{n['name']}  {n['kind']} <{n['edge']}>{site}{extra}")
            show(n["children"], prefix + ("   " if last else "│  "))

    for d in directions:
        label = "callers / inbound" if d == "in" else "callees / outbound"
        print(f"\n{label}:")
        if not tree[d]:
            print("  (none)")
        show(tree[d])


def cmd_refs(a):
    db = connect(a.db)
    cands = resolve(db, a.symbol)
    if not cands:
        raise SystemExit(f"no symbol matched {a.symbol!r}")
    r = cands[0]
    rows = db.execute("""SELECT o.*, f.rel FROM occurrences o LEFT JOIN files f ON f.path_hash = o.path_hash
                         WHERE o.usr_hash = ? ORDER BY f.rel, o.line LIMIT ?""",
                      (r["usr_hash"], a.limit)).fetchall()
    total = db.execute("SELECT COUNT(*) c FROM occurrences WHERE usr_hash = ?", (r["usr_hash"],)).fetchone()["c"]
    if a.json:
        print(json.dumps({"symbol": qname(db, r["usr_hash"]), "usr": r["usr"], "total": total,
                          "occurrences": [{"file": x["rel"], "line": x["line"], "col": x["col"],
                                           "roles": roles_str(x["roles"])} for x in rows]}, indent=2))
        return
    print(f"{qname(db, r['usr_hash'])}  [{r['kind']}]  {total} occurrences")
    cur = None
    for x in rows:
        if x["rel"] != cur:
            cur = x["rel"]; print(f"\n{cur}:")
        print(f"  {x['line']}:{x['col']}  {roles_str(x['roles'])}")


def cmd_snippet(a):
    db = connect(a.db)
    m = meta(db)
    cands = resolve(db, a.symbol)
    if not cands:
        raise SystemExit(f"no symbol matched {a.symbol!r}")
    r = cands[0]
    if not r["def_path_hash"]:
        raise SystemExit("symbol has no definition site in this index (external or module symbol)")
    path = os.path.join(m["repo_root"], rel(db, r["def_path_hash"]))
    if not os.path.exists(path):
        raise SystemExit(f"file not found on disk: {path}")
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    start = max(0, r["def_line"] - 1)
    end = start
    depth, opened = 0, False
    for i in range(start, min(len(lines), start + a.max_lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if "{" in lines[i]:
            opened = True
        end = i
        if opened and depth <= 0:
            break
    if not opened:
        end = min(len(lines) - 1, start + 2)
    print(f"{rel(db, r['def_path_hash'])}:{r['def_line']}  [{r['kind']} {r['name']}]")
    for i in range(start, end + 1):
        print(f"{i+1:6}  {lines[i]}")


def cmd_sql(a):
    db = connect(a.db)
    q = a.query.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", q):
        raise SystemExit("only SELECT / WITH queries are allowed")
    if not re.search(r"(?i)\blimit\b", q):
        q += f" LIMIT {a.limit}"
    rows = db.execute(q).fetchall()
    if a.json:
        print(json.dumps([dict(r) for r in rows], indent=2)); return
    if not rows:
        print("(no rows)"); return
    cols = rows[0].keys()
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print(f"\n{len(rows)} rows")


def cmd_arch(a):
    db = connect(a.db)
    mods = db.execute("""SELECT module, COUNT(*) syms, SUM(call_in) cin, SUM(call_out) cout
                         FROM symbols WHERE in_repo = 1 AND module IS NOT NULL
                         GROUP BY module ORDER BY syms DESC LIMIT ?""", (a.limit,)).fetchall()
    db.create_function("layer", 1, path_layer)
    layers = db.execute("""SELECT layer(rel) layer, COUNT(*) files FROM files
                           WHERE in_repo = 1 GROUP BY layer ORDER BY files DESC LIMIT 15""").fetchall()
    hot_in = db.execute("""SELECT name, kind, module, call_in FROM symbols WHERE in_repo = 1
                           ORDER BY call_in DESC LIMIT 10""").fetchall()
    hot_out = db.execute("""SELECT name, kind, module, call_out FROM symbols WHERE in_repo = 1
                            ORDER BY call_out DESC LIMIT 10""").fetchall()
    targets = db.execute("SELECT target, COUNT(*) n FROM units GROUP BY target ORDER BY n DESC").fetchall()
    if a.json:
        print(json.dumps({"modules": [dict(r) for r in mods], "layers": [dict(r) for r in layers],
                          "top_called": [dict(r) for r in hot_in], "top_callers": [dict(r) for r in hot_out],
                          "targets": [dict(r) for r in targets]}, indent=2))
        return
    print("layers (indexed files)")
    w = max([len(r["layer"] or "") for r in layers] + [10])
    for r in layers:
        print(f"  {(r['layer'] or ''):<{w}} {r['files']:>7,}")
    print("\ntop modules by symbol count")
    for r in mods:
        print(f"  {r['module']:<32} syms={r['syms']:>7,}  calls in/out={r['cin']:>7,}/{r['cout']:>7,}")
    print("\nmost-called symbols")
    for r in hot_in:
        print(f"  {r['name']:<40} {r['kind']:<16} {r['module'] or '':<24} {r['call_in']:>6,}")
    print("\nbiggest callers")
    for r in hot_out:
        print(f"  {r['name']:<40} {r['kind']:<16} {r['module'] or '':<24} {r['call_out']:>6,}")
    print("\nbuild targets")
    for r in targets:
        print(f"  {r['target']:<40} {r['n']:>6,} units")


def cmd_viz(a):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import viz
    db = connect(a.db)
    limit = a.limit if a.limit is not None else (20000 if a.scope else 1500)
    data = viz.slice_data(db, scope=a.scope, limit=limit, edge_cap=a.edge_cap,
                          per_node_cap=a.per_node_cap)
    m = meta(db)
    root = m.get("repo_root", "")
    if os.path.isdir(root):
        import subprocess
        tracked = subprocess.run(["git", "-C", root, "ls-files", "-z", "*.swift", "*.m", "*.h", "*.mm"],
                                 capture_output=True, text=True).stdout.split("\0")
        tracked = [f for f in tracked if f]
        have = {r["rel"] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1")}
        cov = sum(1 for f in tracked if f in have)
        data["meta"]["coverage_pct"] = round(100 * cov / len(tracked), 1) if tracked else None
        data["meta"]["coverage"] = f"{cov}/{len(tracked)}"
    out = os.path.expanduser(a.out or f"~/.cache/indexstore-graph/{m.get('project','graph')}-explorer.html")
    viz.render(data, out, title=a.title)
    print(f"wrote {out}  ({os.path.getsize(out)/1e6:.1f} MB)")
    print(f"nodes: {len(data['nodes']):,} (slice {data['slice_size']:,})  edges: {len(data['edges']):,}")
    if a.open:
        subprocess_open = __import__("subprocess")
        subprocess_open.run(["open", out])


def cmd_schema(a):
    db = connect(a.db)
    for r in db.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"):
        print(f"-- {r['type']}: {r['name']}\n{r['sql']};\n")
    print("-- edge kinds:", ", ".join(EDGE_KINDS))
    print("-- occurrence role bits:", ", ".join(f"{b}={n}" for b, n in ROLE_BITS))
    print(textwrap.dedent("""
    -- edge direction: src --kind--> dst
    --   CALLS       caller -> callee (from index-store calledBy relations)
    --   REFERENCES  enclosing symbol -> referenced symbol (containedBy)
    --   CONTAINS    parent -> child (childOf: type -> member, func -> local type)
    --   INHERITS    subclass/conformer -> base class/protocol (baseOf)
    --   OVERRIDES   override -> overridden requirement
    --   EXTENDS     extension -> extended type (extendedBy)
    --   ACCESSOR_OF getter/setter -> property
    -- every edge row carries the source location of the relation (path_hash, line, col)
    """).strip())


def build_parser():
    ap = argparse.ArgumentParser(prog="idxg", description="query a Swift index-store graph")
    ap.add_argument("--db", help="graph db path (default ~/.cache/indexstore-graph/<cwd>.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="index status + coverage summary")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)

    p = sub.add_parser("coverage", help="check which paths the index actually covers")
    p.add_argument("paths", nargs="+"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_coverage)

    p = sub.add_parser("search", help="find symbols (regex, full-text, filters)")
    p.add_argument("query", nargs="?", help="full-text query (BM25 over camel-split names)")
    p.add_argument("--name", help="regex on the symbol name")
    p.add_argument("--kind", help="comma list, e.g. Struct,Class,InstanceMethod")
    p.add_argument("--module"); p.add_argument("--file", help="glob on repo-relative path")
    p.add_argument("--lang", help="Swift|ObjC|C|C++")
    p.add_argument("--min-degree", type=int, default=0); p.add_argument("--max-degree", type=int)
    p.add_argument("--limit", type=int, default=40); p.add_argument("--offset", type=int, default=0)
    p.add_argument("--all", action="store_true", help="include symbols defined outside the repo")
    p.add_argument("--usr", action="store_true", help="print USRs")
    p.add_argument("--detail", choices=["ids", "default"], default="default")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_search)

    p = sub.add_parser("trace", help="walk call/reference edges from a symbol")
    p.add_argument("symbol"); p.add_argument("--direction", choices=["in", "out", "both"], default="both")
    p.add_argument("--depth", type=int, default=2); p.add_argument("--fanout", type=int, default=25)
    p.add_argument("--kind", default="CALLS", help="edge kinds, comma list (%s)" % ",".join(EDGE_KINDS))
    p.add_argument("--first", action="store_true", help="use the best match instead of listing candidates")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_trace)

    p = sub.add_parser("refs", help="every occurrence of a symbol with roles")
    p.add_argument("symbol"); p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_refs)

    p = sub.add_parser("snippet", help="print a symbol's definition from disk")
    p.add_argument("symbol"); p.add_argument("--max-lines", type=int, default=200)
    p.set_defaults(fn=cmd_snippet)

    p = sub.add_parser("sql", help="read-only SQL over the graph")
    p.add_argument("query"); p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_sql)

    p = sub.add_parser("arch", help="layers, modules, hotspots, targets")
    p.add_argument("--limit", type=int, default=20); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_arch)

    p = sub.add_parser("viz", help="generate the self-contained HTML explorer")
    p.add_argument("--out", help="output html path")
    p.add_argument("--scope", help="module name or path glob to slice on")
    p.add_argument("--limit", type=int, default=None,
                   help="symbols in the slice (default: all in scope, or top 1500 repo-wide; 0 = all)")
    p.add_argument("--edge-cap", type=int, default=30000)
    p.add_argument("--per-node-cap", type=int, default=25, help="max edges kept per symbol per direction")
    p.add_argument("--title"); p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_viz)

    p = sub.add_parser("schema", help="print the db schema and edge/role vocabulary")
    p.set_defaults(fn=cmd_schema)
    return ap


if __name__ == "__main__":
    args = build_parser().parse_args()
    try:
        args.fn(args)
    except BrokenPipeError:
        pass
