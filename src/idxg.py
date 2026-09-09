#!/usr/bin/env python3
"""idxg: query a Swift/clang index-store knowledge graph (codebase-memory shaped)."""
import argparse, json, os, re, shutil, sqlite3, subprocess, sys, textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project as prj
import deadcode

VERSION = "0.1.0"

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
    root = prj.find_root()
    db = prj.db_for(root)
    if os.path.exists(db):
        return db
    raise SystemExit(f"no graph for {root}\n"
                     f"  run: idxg init            (index this project, write the skill + CLAUDE.md note)\n"
                     f"  or:  idxg --db <path> ...  (query another project's graph)")


def connect(arg, write=False):
    p = db_path(arg)
    uri = f"file:{p}" + ("" if write else "?mode=ro")
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.create_function("regexp", 2, lambda pat, val: 1 if val is not None and re.search(pat, val) else 0)
    return db


def check_stale(args):
    """Warn on stderr when the graph is older than the index store; refresh if configured."""
    if getattr(args, "no_stale_check", False):
        return
    try:
        db = connect(getattr(args, "db", None))
        m = meta(db)
        db.close()
    except SystemExit:
        raise
    except Exception:
        return
    stale, reason, _ = prj.staleness(m)
    if not stale:
        return
    root = m.get("repo_root")
    cfg = prj.effective_config(root)
    if cfg.get("auto_refresh_on_query") or getattr(args, "refresh", False):
        print(f"graph is stale ({reason}); reindexing {root}", file=sys.stderr)
        build_now(root, quiet=True)
        return
    print(f"note: graph is stale ({reason}). run `idxg refresh` to reindex {root}", file=sys.stderr)


def build_now(root, jobs=None, quiet=False, viz=None):
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "build.py"),
           "--root", root, "--jobs", str(jobs or prj.effective_config(root).get("jobs", 4))]
    if viz is False:
        cmd.append("--no-viz")
    out = subprocess.run(cmd, capture_output=quiet, text=True)
    if out.returncode != 0:
        if quiet and out.stderr:
            print(out.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"indexing failed for {root}")
    if quiet and out.stdout:
        try:
            return json.loads(out.stdout[out.stdout.index("{"):])
        except (ValueError, json.JSONDecodeError):
            return None
    return None


def meta(db):
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}


REGEX_META = set(".^$*+?()[]{}|\\") | set("[]")


def literal_name_filter(pattern):
    """Translate ^Foo$ / ^Foo / Foo$ into an indexable comparison, else (None, None).

    GLOB, not LIKE: SQLite's LIKE is case-insensitive for ASCII, which would return more
    than the equivalent regex and quietly disagree with the slow path.
    """
    anchored_start = pattern.startswith("^")
    anchored_end = pattern.endswith("$") and not pattern.endswith("\\$")
    body = pattern[1:] if anchored_start else pattern
    if anchored_end:
        body = body[:-1]
    if not body or REGEX_META & set(body):
        return None, None
    if anchored_start and anchored_end:
        return "s.name = ?", body
    if anchored_start:
        return "s.name GLOB ?", body + "*"
    if anchored_end:
        return "s.name GLOB ?", "*" + body
    return "s.name GLOB ?", f"*{body}*"


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
    tables = ("symbols", "edges", "occurrences", "defs", "files", "units")
    cached = all(f"count_{t}" in m for t in tables) and not a.exact
    if cached:
        counts = {t: int(m[f"count_{t}"]) for t in tables}
    else:
        counts = {t: db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    if m.get("edge_kinds") and not a.exact:
        per_kind = [{"kind": k, "c": v} for k, v in json.loads(m["edge_kinds"]).items()]
    else:
        per_kind = db.execute("SELECT kind, COUNT(*) c FROM edges GROUP BY kind ORDER BY c DESC").fetchall()
    in_repo = db.execute("SELECT COUNT(*) c FROM files WHERE in_repo = 1").fetchone()["c"]
    stamp = "" if a.exact else "  (counts as of the last build; --exact recounts)"
    swift = int(m.get("count_swift_symbols") or 0) or db.execute(
        "SELECT COUNT(*) c FROM symbols WHERE lang='Swift'").fetchone()["c"]
    root = m.get("repo_root", "")
    tracked = int(m.get("coverage_tracked") or 0)
    covered = int(m.get("coverage_covered") or 0)
    recounted = not cached
    if (not tracked or a.exact) and os.path.isdir(root):
        recounted = True
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", "*.swift", "*.m", "*.h", "*.mm",
                              "*.c", "*.cpp"], capture_output=True, text=True).stdout.split("\0")
        out = [f for f in out if f]
        tracked = len(out)
        have = {r["rel"] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1")}
        covered = sum(1 for f in out if f in have)
    if recounted:
        cache_summary(a.db, counts, per_kind, tracked, covered)
    if a.json:
        print(json.dumps({"meta": m, "counts": counts, "history": _history_status(a.db),
                          "edges_by_kind": {r["kind"]: r["c"] for r in per_kind},
                          "files_in_repo": in_repo, "swift_symbols": swift,
                          "coverage": {"tracked_sources": tracked, "covered": covered,
                                       "pct": round(100 * covered / tracked, 1) if tracked else None}}, indent=2))
        return
    print(f"project:   {m.get('project')}")
    print(f"repo root: {m.get('repo_root')}")
    print(f"store:     {m.get('store_path')}")
    print(f"built:     {m.get('built_at')}  (format v{m.get('format_version')}, {m.get('build_seconds')}s)")
    print(f"db:        {db_path(a.db)}  ({os.path.getsize(db_path(a.db))/1e9:.2f} GB)")
    print(f"\ncounts{stamp}")
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
    hist_line = _history_status(a.db)
    if hist_line:
        print(f"\nhistory: {hist_line}")


def _history_status(db_arg):
    hist = _history_module()
    hp = hist.history_db_for(db_path(db_arg))
    if not os.path.exists(hp):
        return "not built (idxg history build adds commit history and repo docs)"
    h = hist.connect(hp)
    m = hist.meta(h)
    h.close()
    return (f"{int(m.get('count_commits') or 0):,} commits on {m.get('branch')} "
            f"({m.get('first_day')} to {m.get('last_day')}), {int(m.get('count_docs') or 0)} docs, "
            f"built {m.get('built_at')} at {m.get('head_sha', '')[:11]}")


def cache_summary(db_arg, counts, per_kind, tracked, covered):
    """Persist the expensive summaries so later calls read them instead of recounting.

    Best effort: a read-only volume or a concurrent build just means the next call
    recounts again.
    """
    try:
        w = sqlite3.connect(db_path(db_arg))
        # No WAL: a lingering journal beside the database breaks the build's atomic swap.
        w.execute("PRAGMA journal_mode=DELETE")
        with w:
            for name, value in counts.items():
                w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"count_{name}", str(value)))
            kinds = {(r["kind"] if not isinstance(r, dict) else r["kind"]):
                     (r["c"] if not isinstance(r, dict) else r["c"]) for r in per_kind}
            w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("edge_kinds", json.dumps(kinds)))
            if tracked:
                w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("coverage_tracked", str(tracked)))
                w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("coverage_covered", str(covered)))
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        w.close()
    except sqlite3.Error:
        pass


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
        # An anchored literal is by far the most common pattern an agent sends, and it can
        # use the name index instead of running the regex over every symbol.
        sql, val = literal_name_filter(a.name)
        if sql:
            where.append(sql); args.append(val)
        else:
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
    printed = [0]
    spent = [0]
    loc = f"{tree['file']}:{tree['line']}" if tree["file"] else "external"
    print(f"{tree['symbol']}  [{tree['kind']}]  {loc}")
    print(f"edges: {','.join(kinds)}  depth: {a.depth}")

    def show(nodes, prefix=""):
        for i, n in enumerate(nodes):
            if printed[0] >= a.max_rows or spent[0] >= a.max_bytes:
                return
            last = i == len(nodes) - 1
            branch = "└─ " if last else "├─ "
            site = f"  {n['sites'][0]}" if n["sites"] else ""
            extra = f" (x{n['site_count']})" if n["site_count"] > 1 else ""
            line = f"{prefix}{branch}{n['name']}  {n['kind']} <{n['edge']}>{site}{extra}"
            print(line)
            printed[0] += 1
            spent[0] += len(line) + 1
            show(n["children"], prefix + ("   " if last else "│  "))

    for d in directions:
        label = "callers / inbound" if d == "in" else "callees / outbound"
        print(f"\n{label}:")
        if not tree[d]:
            print("  (none)")
        before = printed[0]
        show(tree[d])
        if printed[0] >= a.max_rows or spent[0] >= a.max_bytes:
            why = "rows" if printed[0] >= a.max_rows else "size"
            print(f"  ... truncated on {why} ({printed[0]} rows, {spent[0]} bytes). Narrow with "
                  f"--fanout / --depth / --kind, or raise --max-rows / --max-bytes")


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
    spent = 0
    for i in range(start, end + 1):
        out = f"{i+1:6}  {lines[i]}"
        if spent + len(out) > a.max_bytes:
            print(f"  ... truncated at {i - start} lines / {spent} bytes "
                  f"(raise with --max-bytes)")
            break
        print(out)
        spent += len(out) + 1


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
    m = meta(db)
    cfgv = prj.effective_config(m.get("repo_root") or prj.find_root())
    scope = a.scope or (cfgv.get("viz_scope") or None)
    limit = a.limit if a.limit is not None else (20000 if scope else cfgv.get("viz_limit", 1500))
    data = viz.slice_data(db, scope=scope, limit=limit, edge_cap=a.edge_cap,
                          per_node_cap=a.per_node_cap if a.per_node_cap is not None
                          else cfgv.get("viz_per_node_cap", 25))
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
    db_file = db_path(a.db)
    viz.attach_history(data, db_file, weeks=cfgv.get("viz_history_weeks", 26))
    # Derive the default from the database path, the same way build.py does, so viz and a
    # build cannot write two different explorer files for one project.
    default_out = os.path.join(os.path.dirname(db_file),
                               os.path.basename(db_file).replace(".db", "-explorer.html"))
    out = os.path.expanduser(a.out) if a.out else default_out
    viz.render(data, out, title=a.title)
    if not a.out and root:
        entry = prj.load_registry().get(root, {})
        prj.register(root, entry.get("db", db_file), entry.get("stores") or [], {"html": out})
    print(f"wrote {out}  ({os.path.getsize(out)/1e6:.1f} MB)")
    print(f"nodes: {len(data['nodes']):,} (slice {data['slice_size']:,})  edges: {len(data['edges']):,}")
    if a.open:
        subprocess_open = __import__("subprocess")
        subprocess_open.run(["open", out])


def cmd_dead(a):
    db = connect(a.db)
    m = meta(db)
    root = m.get("repo_root", "")
    total, rows = deadcode.candidates(db, kinds=a.kind.split(",") if a.kind else None,
                                      module=a.module, include_tests=a.include_tests,
                                      limit=a.limit, offset=a.offset, lang=a.lang,
                                      include_vendor=a.include_vendor)
    out = []
    for r in rows:
        item = {"name": r["name"], "kind": r["kind"], "module": r["module"],
                "file": r["rel"], "line": r["def_line"], "usr": r["usr"]}
        if a.verify and root:
            others, same_file = deadcode.mentions(root, r["name"], r["rel"] or "")
            item["mentions"] = others[:5]
            item["mention_count"] = len(others)
            item["same_file_mentions"] = same_file
            if others:
                continue
        out.append(item)
    if a.json:
        print(json.dumps({"total_candidates": total, "returned": len(out),
                          "verified": bool(a.verify), "candidates": out}, indent=2))
        return
    cov = ""
    print(f"dead-code candidates: {total}" + (f", {len(out)} survive the text check" if a.verify else ""))
    print("only as good as the index: a symbol used solely from files the build never")
    print("compiled looks unreachable here. Check `idxg status` coverage first.\n")
    group = None
    for item in out:
        g = f"{item['module'] or '?'} ({item['file'] or 'external'})"
        if g != group:
            group = g
            print(f"\n{group}:")
        same = item.get("same_file_mentions") or 0
        hint = f"  ({same} same-file mention{'s' if same != 1 else ''}, check overloads)" if same else ""
        print(f"  {item['name']}  {item['kind']}:{item['line']}{hint}")
    if not a.verify:
        print("\nadd --verify to drop candidates whose name appears in any other file")


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
    hist = _history_module()
    hp = hist.history_db_for(db_path(a.db))
    if not os.path.exists(hp):
        print("\n-- history db: not built (idxg history build)")
        return
    h = hist.connect(hp)
    print(f"\n-- history db: {hp}")
    for r in h.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"):
        print(f"-- {r['type']}: {r['name']}\n{r['sql']};\n")
    print(textwrap.dedent("""
    -- commits: one row per first-parent commit of the history branch; pr_* columns come from
    --   GitHub when gh is logged in (pr_body NULL = not fetched, '' = PR not found)
    -- commit_files.path equals files.rel in the graph db: that is the join between the two
    -- commit_files.module comes from the graph's module directory prefixes, NULL when uncompiled
    -- components: module-depth directories; alive=0 means the branch no longer has the directory
    -- module_rank: cross-module CALLS in/out per module, used to order digest areas
    """).strip())
    h.close()



# ---------------------------------------------------------------- project setup
# ---------------------------------------------------------------- history and docs
def _history_module():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import history
    return history


def history_db(a):
    """Read-only connection to the history db that belongs to the graph in scope."""
    hist = _history_module()
    return hist, hist.connect(hist.history_db_for(db_path(getattr(a, "db", None))))


def _history_config(a):
    root = meta(connect(a.db)).get("repo_root") or prj.find_root()
    return root, prj.effective_config(root)


def cmd_history_build(a):
    hist = _history_module()
    graph = db_path(a.db)
    root, cfg = _history_config(a)
    manifest = cfg.get("docs_manifest") or None
    if manifest and not os.path.isabs(os.path.expanduser(manifest)):
        manifest = os.path.join(root, manifest)
    first_parent = cfg.get("history_first_parent", True) and not getattr(a, "all_commits", False)
    path, counts = hist.build(root, graph, branch=a.branch or (cfg.get("history_branch") or None),
                              first_parent=first_parent, full=a.full, since=a.since,
                              docs=a.docs, prs=a.prs and cfg.get("history_prs", True), manifest_path=manifest)
    if getattr(a, "json", False):
        print(json.dumps({"db": path, **counts}))


def _fmt_commit(r, web=""):
    pr = f"  #{r['pr']}" if r["pr"] else ""
    tickets = f"  [{r['tickets']}]" if r["tickets"] else ""
    return (f"{r['day']}  {r['short']}  {r['subject'][:100]}{pr}{tickets}\n"
            f"            {r['author']}  {r['files']} files  +{r['ins']:,} -{r['del']:,}")


def _symbol_paths(a):
    """The definition file of a symbol, so its history is the history of that file."""
    db = connect(a.db)
    cands = resolve(db, a.symbol)
    if not cands:
        raise SystemExit(f"no symbol matched {a.symbol!r}")
    r = cands[0]
    if not r["def_path_hash"]:
        raise SystemExit(f"{a.symbol} has no definition site in the index, so no file to follow")
    path = rel(db, r["def_path_hash"])
    db.close()
    return path, r


def cmd_history_log(a):
    hist, hdb = history_db(a)
    paths = list(getattr(a, "paths", None) or [])
    note = ""
    if getattr(a, "symbol", None):
        path, sym = _symbol_paths(a)
        paths.append(path)
        note = (f"following {sym['name']} through its definition file {path}; line-level history "
                f"is not tracked, so unrelated edits to the file appear too\n")
    rows = hist.commits_for(hdb, paths=paths or None, module=a.module, component=a.component,
                            author=a.author, since=a.since, until=a.until, query=a.grep, limit=a.limit)
    m = hist.meta(hdb)
    if getattr(a, "json", False):
        out = [dict(r) for r in rows]
        if a.files:
            for o in out:
                o["changed"] = [dict(f) for f in hist.files_of(hdb, o["sha"], 60)]
        if getattr(a, "narrate", False):
            for o in out:
                full = hdb.execute("SELECT * FROM commits WHERE sha = ?", (o["sha"],)).fetchone()
                o["narrative"] = hist.narrate_commit(full, hist.files_of(hdb, o["sha"]), m.get("remote_web", ""))
        print(json.dumps({"branch": m.get("branch"), "head": m.get("head_sha"), "commits": out}, indent=1))
        return
    print(f"{m.get('branch')} @ {m.get('head_sha', '')[:11]}, built {m.get('built_at')}"
          + (f"  (scope: {', '.join(filter(None, paths + [a.module, a.component]))})" if paths or a.module or a.component else ""))
    if note:
        print(note.rstrip())
    if not rows:
        print("no commits matched. Module filters only see files the compiled index attributes; "
              "try a path or directory instead.")
        return
    spent, shown = 0, 0
    budget = getattr(a, "max_bytes", 12000)
    web = m.get("remote_web", "")
    for r in rows:
        if getattr(a, "narrate", False):
            full = hdb.execute("SELECT * FROM commits WHERE sha = ?", (r["sha"],)).fetchone()
            block = [textwrap.fill(hist.narrate_commit(full, hist.files_of(hdb, r["sha"]), web), 100)
                     + (f"\n  {web}/pull/{r['pr']}" if web and r["pr"] else "")]
        else:
            block = [_fmt_commit(r)]
        if a.files:
            for f in hist.files_of(hdb, r["sha"], 8):
                mod = f"  [{f['module']}]" if f["module"] else ""
                block.append(f"              +{f['ins']:<5} -{f['del']:<5} {f['path']}{mod}")
            if r["files"] > 8:
                block.append(f"              ... {r['files'] - 8} more files (idxg history show {r['short']})")
        text = "\n".join(block)
        if spent + len(text) > budget and shown:
            print(f"... {len(rows) - shown} more commits not printed: {budget:,} byte budget reached "
                  f"(raise --max-bytes, or narrow with --since, --module or a path)")
            break
        print(text)
        spent += len(text) + 1
        shown += 1
    if shown == len(rows) == a.limit:
        print(f"(showing {a.limit}; raise --limit or narrow with --since, --module or a path)")


def cmd_history_show(a):
    hist, hdb = history_db(a)
    ident = a.sha.strip()
    if ident.startswith("#") and ident[1:].isdigit():
        r = hdb.execute("SELECT * FROM commits WHERE pr = ? ORDER BY committed DESC", (int(ident[1:]),)).fetchone()
    else:
        r = hdb.execute("SELECT * FROM commits WHERE sha = ? OR sha GLOB ? ORDER BY committed DESC",
                        (ident, ident + "*")).fetchone()
    if not r:
        raise SystemExit(f"no commit {ident} in the history of {hist.meta(hdb).get('branch')}")
    files = hist.files_of(hdb, r["sha"], a.max_files)
    web = hist.meta(hdb).get("remote_web", "")
    if getattr(a, "json", False):
        d = dict(r)
        d["changed"] = [dict(f) for f in files]
        print(json.dumps(d, indent=1))
        return
    print(f"{r['sha']}  {r['day']}  {r['author']} <{r['email']}>")
    print(f"{r['subject']}")
    if r["pr"]:
        print(f"pr: #{r['pr']}" + (f"  {web}/pull/{r['pr']}" if web else ""))
    if r["tickets"]:
        print(f"tickets: {r['tickets']}")
    print(f"{r['files']} files, +{r['ins']:,} -{r['del']:,}, {r['parents']} parent(s)")
    print("\n" + textwrap.fill(hist.narrate_commit(r, hist.files_of(hdb, r["sha"], 2000), web), 100))
    if r["pr_body"]:
        print(f"\npull request description" + (f" ({r['pr_labels']})" if r["pr_labels"] else ""))
        print(textwrap.indent(r["pr_body"][:a.max_body], "  "))
        if len(r["pr_body"]) > a.max_body:
            print(f"  ... truncated at {a.max_body:,} bytes (raise --max-body)")
    if r["body"]:
        print("\ncommit body")
        print(textwrap.indent(r["body"], "  "))
    print("\nfiles")
    for f in files:
        mod = f"  [{f['module']}]" if f["module"] else ""
        old = f"  (was {f['old_path']})" if f["old_path"] else ""
        print(f"  +{f['ins']:<5} -{f['del']:<5} {f['path']}{mod}{old}")
    if r["files"] > len(files):
        print(f"  ... {r['files'] - len(files)} more (raise --max-files)")


def cmd_history_churn(a):
    hist, hdb = history_db(a)
    import datetime as _dt
    since = a.since or (_dt.date.today() - _dt.timedelta(days=365)).isoformat()
    rows = hist.churn(hdb, since=since, by=a.by, limit=a.limit, ext=a.ext)
    if getattr(a, "json", False):
        print(json.dumps({"since": since, "by": a.by, "rows": [dict(r) for r in rows]}, indent=1))
        return
    print(f"change since {since}, by {a.by}" + (f", {a.ext} files only" if a.ext else ""))
    if a.by == "module":
        print("modules come from the compiled index; a file the build never compiled is counted nowhere here")
    w = max([len(str(r["key"])) for r in rows] + [10])
    w = min(w, 60)
    print(f"  {'':<{w}} {'commits':>8} {'+lines':>9} {'-lines':>9} {'authors':>8}  last")
    for r in rows:
        key = str(r["key"])
        key = key if len(key) <= w else "..." + key[-(w - 3):]
        print(f"  {key:<{w}} {r['commits']:>8,} {r['ins'] or 0:>9,} {r['del'] or 0:>9,} {r['authors']:>8}  {r['last']}")


def cmd_history_timeline(a):
    hist, hdb = history_db(a)
    m = hist.meta(hdb)
    granularity, all_eras = hist.eras(hdb, granularity=a.granularity)
    shown = all_eras[-a.periods:] if a.periods else all_eras
    paras = hist.narrate(m, granularity, shown, all_eras)
    overview = hist.overview_text(hdb)
    if getattr(a, "json", False):
        print(json.dumps({"overview": overview, "granularity": granularity,
                          "periods": [{**e, "text": p["text"]} for e, p in zip(shown, paras)]}, indent=1))
        return
    print(textwrap.fill(overview, 100))
    print(f"\none paragraph per {granularity}, {len(shown)} of {len(all_eras)} shown"
          + ("" if len(shown) == len(all_eras) else " (--periods 0 for all)"))
    for p in paras:
        print()
        print(textwrap.fill(p["text"], 100))


def _resolve_window(a, hist, hdb):
    m = hist.meta(hdb)
    if getattr(a, "week", None):
        return hist.week_bounds(a.week)
    if getattr(a, "since", None) or getattr(a, "until", None):
        return a.since or m.get("first_day"), a.until or m.get("last_day")
    return hist.week_bounds(hist.iso_week(m.get("last_day")))


def cmd_history_digest(a):
    hist, hdb = history_db(a)
    m = hist.meta(hdb)
    if getattr(a, "list", False):
        for w, n in hist.weeks(hdb, limit=a.limit):
            s, e = hist.week_bounds(w)
            print(f"  {w}  {s} to {e}  {n:>4} commits")
        return
    start, end = _resolve_window(a, hist, hdb)
    data = hist.week_digest(hdb, start, end, m.get("remote_web", ""))
    if getattr(a, "json", False):
        print(json.dumps(data, indent=1, ensure_ascii=False))
        return
    html_arg = getattr(a, "html", None)
    if html_arg:
        graph = db_path(a.db)
        out = (os.path.expanduser(html_arg) if html_arg != "auto" else
               os.path.join(os.path.dirname(graph), os.path.basename(graph).replace(".db", f"-digest-{start}.html")))
        with open(out, "w") as f:
            f.write(hist.render_digest(data))
        print(f"wrote {out}  ({os.path.getsize(out) / 1024:.0f} KB, {data['window']['commits']} changes)")
        if getattr(a, "open", False):
            subprocess.run(["open", out])
        return
    text = hist.digest_text(data)
    budget = getattr(a, "max_bytes", 0)
    if budget and len(text) > budget:
        print(text[:budget])
        print(f"\n... truncated at {budget:,} of {len(text):,} bytes (raise --max-bytes, or --json for the data)")
    else:
        print(text)


def cmd_history_vault(a):
    hist = _history_module()
    graph = db_path(a.db)
    hdb_path = hist.history_db_for(graph)
    root, cfg = _history_config(a)
    out = a.out or cfg.get("history_vault") or os.path.join(
        os.path.dirname(graph), os.path.basename(graph).replace(".db", "-vault"))
    written, superseded = hist.export_vault(hdb_path, out, docs=a.docs, history=a.history,
                                            modules_min_commits=a.min_commits, dry=a.dry_run)
    for w in written[:40]:
        print(f"  {w}")
    if len(written) > 40:
        print(f"  ... {len(written) - 40} more")
    for new, old in superseded[:20]:
        print(f"  {new} supersedes {old}")
    print(f"\nClippings/ follows the knowledge-vault contract: files are never rewritten, a changed source "
          f"becomes a date-suffixed clipping. Point a vault's compile step at {out}/Clippings, or pass "
          f"--out <vault> to write into the vault directly.")


def cmd_docs_list(a):
    hist, hdb = history_db(a)
    rows = hist.docs_list(hdb, module=a.module, kind=a.kind, path_glob=a.path, limit=a.limit)
    m = hist.meta(hdb)
    if getattr(a, "json", False):
        print(json.dumps([dict(r) for r in rows], indent=1))
        return
    total = int(m.get("count_docs") or 0)
    print(f"{len(rows)} of {total} repo docs (branch {m.get('branch')}, synced {m.get('built_at')})")
    kind = None
    for r in rows:
        if r["kind"] != kind:
            kind = r["kind"]
            print(f"\n{kind}")
        mod = f"  [{r['module']}]" if r["module"] else ""
        refreshed = f"  generated sections as of {r['mechanical_refreshed']}" if r["mechanical_refreshed"] else ""
        print(f"  {r['published'] or '----------'}  {r['path']}{mod}  {r['bytes'] // 1024}k{refreshed}")
    if len(rows) == a.limit:
        print(f"\n(showing {a.limit}; raise --limit or filter with --module, --kind, --path)")
    print("\n`published` is the file's last commit date on the history branch, not the date its content is true.")


def cmd_docs_search(a):
    hist, hdb = history_db(a)
    rows = hist.docs_search(hdb, a.query, limit=a.limit)
    if getattr(a, "json", False):
        print(json.dumps([dict(r) for r in rows], indent=1))
        return
    if not rows:
        print(f"no doc matched {a.query!r}. Docs are the repo's tracked markdown; try idxg docs list.")
        return
    for r in rows:
        mod = f"  [{r['module']}]" if r["module"] else ""
        print(f"{r['path']}{mod}  ({r['kind']}, {r['published'] or 'undated'})")
        print("    " + " ".join(r["snip"].split())[:220])


def cmd_docs_show(a):
    hist, hdb = history_db(a)
    r = hist.doc_get(hdb, a.path)
    if isinstance(r, list):
        if not r:
            raise SystemExit(f"no doc at {a.path!r}; idxg docs search finds one by content")
        print(f"{a.path!r} matches several docs:")
        for c in r:
            print(f"  {c['path']}")
        return
    body = r["content"]
    print(f"{r['path']}  ({r['kind']}, last commit {r['published'] or 'unknown'}"
          + (f", generated sections as of {r['mechanical_refreshed']}" if r["mechanical_refreshed"] else "")
          + (f", module {r['module']}" if r["module"] else "") + f", {r['bytes']:,} bytes)\n")
    if len(body) > a.max_bytes:
        print(body[:a.max_bytes])
        print(f"\n... truncated at {a.max_bytes:,} of {len(body):,} bytes (raise --max-bytes)")
    else:
        print(body)


def cmd_history(a):
    a.hfn(a)


def cmd_docs(a):
    a.hfn(a)


CLAUDE_START = "<!-- codebase-brain:start -->"
CLAUDE_END = "<!-- codebase-brain:end -->"
# Markers and label the tool wrote before it was renamed; init and deinit still recognise them.
LEGACY_CLAUDE_START = "<!-- ios-codebase-indexer:start -->"
LEGACY_CLAUDE_END = "<!-- ios-codebase-indexer:end -->"
LAUNCH_LABEL = "com.codebase-brain.autoindex"
LEGACY_LAUNCH_LABEL = "com.ios-codebase-indexer.autoindex"
PROJECT_SKILL = "codebase-brain"
LEGACY_PROJECT_SKILL = "codebase-index"
NOTES_MARKER = "## Project notes"


def _markers_in(text):
    for start, end in ((CLAUDE_START, CLAUDE_END), (LEGACY_CLAUDE_START, LEGACY_CLAUDE_END)):
        if start in text and end in text:
            return start, end
    return None, None


def _remove_skill_dir(path, keep_notes_into=None):
    """Delete a project skill directory the tool wrote under an older name, carrying its
    Project notes section over to the new skill file so nothing the user wrote is lost."""
    skill = os.path.join(path, "SKILL.md")
    if not os.path.exists(skill):
        return
    if keep_notes_into:
        with open(skill) as f:
            old = f.read()
        if NOTES_MARKER in old:
            notes = old[old.index(NOTES_MARKER) + len(NOTES_MARKER):].lstrip("\n")
            target = os.path.join(keep_notes_into, "SKILL.md")
            if notes.strip() and not os.path.exists(target):
                with open(target, "w") as f:
                    f.write(NOTES_MARKER + "\n\n" + notes)
    shutil.rmtree(path, ignore_errors=True)


def project_stats(db_file):
    db = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    m = meta(db)
    n = {t: db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
         for t in ("symbols", "edges", "occurrences", "files")}
    mods = [r["module"] for r in db.execute(
        """SELECT module FROM symbols WHERE in_repo=1 AND module IS NOT NULL AND module != ''
           GROUP BY module ORDER BY COUNT(*) DESC LIMIT 12""")]
    langs = {r["lang"]: r["c"] for r in db.execute(
        "SELECT lang, COUNT(*) c FROM symbols WHERE in_repo=1 GROUP BY lang")}
    db.close()
    return m, n, mods, langs


def install_project_skill(root, db_file):
    m, n, mods, langs = project_stats(db_file)
    name = m.get("project") or os.path.basename(root)
    d = os.path.join(root, ".claude", "skills", PROJECT_SKILL)
    os.makedirs(d, exist_ok=True)
    _remove_skill_dir(os.path.join(root, ".claude", "skills", LEGACY_PROJECT_SKILL), keep_notes_into=d)
    body = f"""---
name: codebase-brain
description: Query {name}'s compiler-accurate code graph, its commit history and its own docs instead of grepping. Use for who calls X, what X calls, where X is referenced, override and conformance chains, module coupling, dead-code candidates, or any structural question about this codebase. Also covers refreshing the index after a build.
---

# {name} code index

This project can be queried as a graph of its own source, extracted from the compiler's
index store: symbols, calls, references, overrides and conformances as the compiler
resolved them, each edge carrying the exact `file:line` of the relation. On the machine
where this was generated it held roughly {n['symbols']:,} symbols and {n['edges']:,} edges
across {n['files']:,} files; run `idxg status` for the current numbers, the database path,
and how much of the repo the last build actually covered.

If `idxg` is missing, install it from
https://github.com/AlucarDWeb/codebase-brain and run `idxg init` here.

## Use it before grepping

```bash
idxg trace <Symbol> --direction in --first     # who calls it, with call sites
idxg trace <Symbol> --direction out --first    # what it calls
idxg refs <Symbol>                             # every occurrence, with roles
idxg search "<words>"                          # full text over camel-split names
idxg search --name '<regex>' --kind Struct,Class
idxg snippet <Symbol>                          # definition from disk
idxg arch                                      # layers, modules, hotspots
idxg sql "SELECT ..."                          # raw SQL, see idxg schema
idxg history log --symbol <Symbol>             # commits that touched its file, with PRs
idxg history log --module <Module> --since 2026-01-01
idxg history log --narrate --since <date>      # one paragraph per commit: who, what, why
idxg history show #<PR>                        # one change in full: PR description, files, modules
idxg history digest [--week 2026-W36]          # the week, every change narrated, by area
idxg history churn --by module                 # where change concentrated this year
idxg history timeline                          # the project's history in prose
idxg docs search "<words>"                     # the repo's own markdown docs, full text
idxg docs list --module <Module>               # a module's README, CLAUDE.md, design docs
```

Add `--json` for machine-readable output. Symbols resolve by bare name, `Module.Name`, or
USR; ambiguous names list candidates unless you pass `--first`.

## Rules for this project

- Structural questions go through the graph, not ripgrep. Ripgrep is for literal text,
  comments, strings, and files the build never compiled.
- Before claiming nothing references a symbol, run `idxg coverage <path>`. A file with no
  index records was never compiled in the indexed build; absence there proves nothing.
- The graph is a snapshot of the last compile. After edits or a build, run `idxg refresh`.
  Line numbers drift until you do.
- `REFERENCES` edges are broad (enclosing symbol to everything it mentions, type
  annotations included). Default to `--kind CALLS` and add `REFERENCES` deliberately.
- Swift properties also exist as `getter:`/`setter:` methods; call edges land on the
  accessor. Use `--kind CALLS,ACCESSOR_OF` when a property looks unused.
- History is the main branch's `git log`, one row per merge. Module attribution follows
  the compiled index, so a commit to a file the build never compiled has no module. For
  "why was this done", read the commit body with `idxg history show`.
- Repo docs are the tracked markdown files; `published` is a file's last commit date,
  not the date its content is true. Prefer the code graph when the two disagree.

Largest modules: {', '.join(mods[:12])}.

Visual explorer: `idxg open` (cross-module call graph, symbol browser).

{NOTES_MARKER}

Anything below this line is yours: project-specific gotchas, build quirks, which targets
the index actually covers. `idxg init` regenerates everything above it and leaves this
section untouched.
"""
    path = os.path.join(d, "SKILL.md")
    keep = ""
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
        if NOTES_MARKER in old:
            keep = old[old.index(NOTES_MARKER) + len(NOTES_MARKER):].lstrip("\n")
    if keep:
        body = body[:body.index(NOTES_MARKER) + len(NOTES_MARKER)] + "\n\n" + keep
    with open(path, "w") as f:
        f.write(body)
    return path


def install_claude_md(root, db_file, target=None):
    m, n, _, _ = project_stats(db_file)
    name = m.get("project") or os.path.basename(root)
    note = f"""{CLAUDE_START}
## Code index and history (codebase-brain)

This repo can be queried as a compiler-accurate code graph, built from the index store
the compiler already writes. `idxg status` prints the database path and coverage.

**Check it before exploring the code.** For any question about how this codebase is
wired, who calls what, where something is used, what a change would affect, or which
modules depend on which, query the graph first and fall back to ripgrep only for literal
text or files the build never compiled.

```bash
idxg trace <Symbol> --direction in --first    # callers, with call sites
idxg trace <Symbol> --direction out --first   # callees
idxg refs <Symbol>                            # all occurrences with roles
idxg search "<words>" | idxg search --name '<regex>' --kind Struct,Class
idxg arch                                     # module coupling and hotspots
idxg coverage <path>                          # what the index actually covers
idxg refresh                                  # reindex after a build or edits
idxg history log --symbol <Symbol>            # who changed it, when, in which PR
idxg history timeline                         # the project's history in prose
idxg docs search "<words>"                    # the repo's own docs, full text
```

The graph reflects the last compile, so refresh after building, and treat a file with no
index records as unproven rather than unused. Details and caveats live in
`.claude/skills/codebase-brain/SKILL.md`. Missing `idxg`? Install it from
https://github.com/AlucarDWeb/codebase-brain and run `idxg init` here.
{CLAUDE_END}"""
    path = target or os.path.join(root, "CLAUDE.md")
    existing = ""
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
    start, end = _markers_in(existing)
    if start:
        head = existing[:existing.index(start)]
        tail = existing[existing.index(end) + len(end):]
        new = head + note + tail
    else:
        sep = "" if not existing else ("" if existing.endswith("\n\n") else
                                       "\n" if existing.endswith("\n") else "\n\n")
        new = existing + sep + note + "\n"
    with open(path, "w") as f:
        f.write(new)
    tracked = subprocess.run(["git", "-C", root, "ls-files", "--error-unmatch",
                              os.path.relpath(path, root)],
                             capture_output=True, text=True).returncode == 0
    return path, tracked


def cmd_deinit(a):
    """Undo everything init put in a project, optionally the graph itself."""
    root = os.path.realpath(os.path.expanduser(a.path)) if a.path else prj.find_root()
    reg = prj.load_registry()
    entry = reg.get(root, {})
    removed, kept = [], []

    for skill_dir in (PROJECT_SKILL, LEGACY_PROJECT_SKILL):
        skill = os.path.join(root, ".claude", "skills", skill_dir, "SKILL.md")
        if not os.path.exists(skill):
            continue
        with open(skill) as f:
            had_notes = NOTES_MARKER in f.read()
        if had_notes and not a.force:
            kept.append(f"{skill}  (has a Project notes section; pass --force to delete)")
        else:
            os.remove(skill)
            removed.append(skill)
            d = os.path.dirname(skill)
            for probe in (d, os.path.dirname(d)):
                try:
                    os.rmdir(probe)
                except OSError:
                    break

    for candidate in {os.path.join(root, "CLAUDE.md"),
                      os.path.join(root, prj.effective_config(root).get("claude_md_path") or "CLAUDE.md")}:
        if not os.path.exists(candidate):
            continue
        with open(candidate) as f:
            text = f.read()
        start, end = _markers_in(text)
        if start:
            head = text[:text.index(start)]
            tail = text[text.index(end) + len(end):]
            new = (head.rstrip("\n") + "\n") + tail.lstrip("\n")
            with open(candidate, "w") as f:
                f.write(new)
            removed.append(f"{candidate}  (agent note stripped)")

    if a.purge:
        db_file = entry.get("db") or prj.db_for(root)
        stem = db_file[:-3] if db_file.endswith(".db") else db_file
        targets = [db_file, entry.get("html") or stem + "-explorer.html", stem + "-history.db",
                   db_file + ".building", db_file + ".lock"]
        targets += [t + sfx for t in list(targets) for sfx in ("-wal", "-shm")]
        if os.path.isdir(os.path.dirname(db_file)):
            targets += [os.path.join(os.path.dirname(db_file), f) for f in os.listdir(os.path.dirname(db_file))
                        if f.startswith(os.path.basename(stem) + "-digest-") and f.endswith(".html")]
        for path in dict.fromkeys(targets):
            if os.path.isfile(path):
                os.remove(path)
                removed.append(path)
        for d in (db_file + ".building.shards", stem + "-vault"):
            if os.path.isdir(d):
                shutil.rmtree(d)
                removed.append(d + "/")
        external_vault = prj.effective_config(root).get("history_vault")
        if external_vault and os.path.isdir(os.path.expanduser(external_vault)):
            kept.append(f"{external_vault}  (your vault; clippings written there stay)")

    if root in reg:
        del reg[root]
        prj.save_registry(reg)
        removed.append(f"registry entry for {root}")

    print(f"project: {root}")
    if removed:
        print("removed")
        for r in removed:
            print("  " + r)
    if kept:
        print("kept")
        for k in kept:
            print("  " + k)
    if not a.purge and entry.get("db"):
        print(f"\nkept the graph, history and explorer beside {entry['db']} (pass --purge to delete them)")
    print("\nre-index any time with: idxg init")


def cmd_config(a):
    root = prj.find_root()
    scope_root = None if a.scope == "global" else root
    if a.unset:
        for key in a.unset:
            where = prj.unset_config(key, scope_root)
            print(f"unset {key}" + (f" ({where})" if where else " (was not set)"))
    for pair in a.assign or []:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        try:
            where, val = prj.set_config(key.strip(), value.strip(), scope_root)
        except ValueError as e:
            raise SystemExit(str(e))
        print(f"set {key.strip()} = {val}  ({where})")
    if a.assign or a.unset:
        print()
    eff = prj.effective_config(root, with_source=True)
    if a.json:
        print(json.dumps({k: {"value": v, "source": src} for k, (v, src) in eff.items()}, indent=2))
        return
    print(f"project: {root}")
    print(f"global:  {prj.CONFIG}")
    w = max(len(k) for k in eff)
    print(f"\n{'key'.ljust(w)}  {'value':<12} {'source':<8} what it does")
    for k, (v, src) in sorted(eff.items()):
        note = prj.CONFIG_HELP.get(k, "")
        if k in prj.GLOBAL_ONLY:
            note += " (global only)"
        print(f"{k.ljust(w)}  {str(v):<12} {src:<8} {note}")
    print("\nset for this project:  idxg config jobs=8 viz_limit=2500")
    print("set globally:          idxg config --global poll_minutes=20")
    print("clear an override:     idxg config --unset viz_limit")


def cmd_init(a):
    root = os.path.realpath(os.path.expanduser(a.path)) if a.path else prj.find_root()
    stores, _ = prj.detect_stores(root)
    print(f"project: {root}")
    if not stores:
        raise SystemExit(
            "no index store found for this project.\n"
            "  SwiftPM/Xcode: open it once in an editor with sourcekit-lsp background indexing,\n"
            "                 or build it so the compiler writes an index store\n"
            "  Bazel:         build with --features=swift.index_while_building, or set up\n"
            "                 sourcekit-bazel-bsp, then run idxg init again")
    for st in stores:
        print(f"  store: {st}")
    db = prj.db_for(root)
    if a.build:
        print("indexing (this can take a couple of minutes on a large repo)")
        build_now(root, jobs=a.jobs, quiet=False, viz=None if a.viz else False)
    elif not os.path.exists(db):
        raise SystemExit("no database yet; run without --no-build")
    entry = prj.load_registry().get(root, {})
    db = entry.get("db", db)
    done = [f"database: {db}"]
    if entry.get("html"):
        done.append(f"explorer: {entry['html']}")
    hist = _history_module()
    if os.path.exists(hist.history_db_for(db)):
        done.append(f"history:  {hist.history_db_for(db)}")
    if a.skill:
        done.append(f"skill:    {install_project_skill(root, db)}")
    if a.claude_md:
        target = a.claude_md_path or (prj.effective_config(root).get("claude_md_path") or None)
        if target and not os.path.isabs(target):
            target = os.path.join(root, target)
        path, tracked = install_claude_md(root, db, target)
        done.append(f"note:     {path}" + ("  (git-tracked, review before committing)" if tracked else ""))
    print("\ninstalled")
    for line in done:
        print("  " + line)
    print("\nnext")
    print("  idxg status            what the index covers")
    print("  idxg history timeline  the project's history in prose")
    print("  idxg docs search <q>   the repo's own docs")
    print("  idxg open              the visual explorer")
    print("  idxg autoindex --install   keep it fresh in the background")


def cmd_refresh(a):
    reg = prj.load_registry()
    roots = list(reg) if a.all else [prj.find_root()]
    for root in roots:
        entry = reg.get(root, {})
        db = entry.get("db") or prj.db_for(root)
        stale, reason, _ = (True, "forced", {})
        if not a.force and os.path.exists(db):
            d = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            d.row_factory = sqlite3.Row
            stale, reason, _ = prj.staleness(meta(d))
            d.close()
        if not stale:
            print(f"up to date: {root}")
            continue
        print(f"reindexing {root}  ({reason})")
        build_now(root, jobs=a.jobs, quiet=not a.verbose)
        print(f"  done: {prj.load_registry().get(root, {}).get('db')}")


def cmd_projects(a):
    reg = prj.load_registry()
    if not reg:
        print("no projects indexed yet; run idxg init inside one")
        return
    rows = []
    for root, e in sorted(reg.items()):
        db = e.get("db", "")
        stale = "?"
        if os.path.exists(db):
            d = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            d.row_factory = sqlite3.Row
            st, reason, _ = prj.staleness(meta(d))
            d.close()
            stale = reason if st else "fresh"
        else:
            stale = "missing db"
        rows.append((root, e.get("symbols", 0), e.get("indexed_at", ""), stale))
    if a.json:
        print(json.dumps([{"root": r, "symbols": s, "indexed_at": t, "state": st}
                          for r, s, t, st in rows], indent=2))
        return
    w = max(len(r[0]) for r in rows)
    print(f"{'project'.ljust(w)}  {'symbols':>9}  {'indexed':<19}  state")
    for r, syms, t, st in rows:
        print(f"{r.ljust(w)}  {syms:>9,}  {t:<19}  {st}")


def cmd_open(a):
    root = prj.find_root()
    entry = prj.load_registry().get(root, {})
    html = entry.get("html")
    if not html or not os.path.exists(html):
        db = entry.get("db") or db_path(a.db)
        html = os.path.join(os.path.dirname(db),
                            os.path.basename(db).replace(".db", "-explorer.html"))
        if not os.path.exists(html):
            raise SystemExit("no explorer yet; run idxg viz")
    print(html)
    subprocess.run(["open", html])


def store_settled(stores, quiet_seconds, max_wait):
    """True once the index store has stopped changing for quiet_seconds.

    A build writes units continuously, so reindexing on the first write would snapshot a
    half-compiled state and then be stale again immediately.
    """
    import time as _t
    waited = 0
    last = prj.store_signature(stores)
    while waited < max_wait:
        _t.sleep(quiet_seconds)
        waited += quiet_seconds
        now = prj.store_signature(stores)
        if now == last:
            return True
        last = now
    return False


def plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_LABEL}.plist")


def _remove_legacy_agent():
    """Unload the agent installed under the tool's previous name, so two agents never
    reindex the same projects."""
    old = os.path.expanduser(f"~/Library/LaunchAgents/{LEGACY_LAUNCH_LABEL}.plist")
    if os.path.exists(old):
        subprocess.run(["launchctl", "unload", old], capture_output=True)
        os.remove(old)
        print(f"removed the agent installed under the old name: {old}")


def cmd_autoindex(a):
    log = os.path.join(prj.CACHE_DIR, "autoindex.log")
    if a.run_once:
        reg = prj.load_registry()
        for root in list(reg):
            db = reg[root].get("db") or prj.db_for(root)
            if not os.path.exists(db) or not os.path.isdir(root):
                continue
            d = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            d.row_factory = sqlite3.Row
            stale, reason, _ = prj.staleness(meta(d))
            d.close()
            stamp = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
            if not stale:
                print(f"{stamp} fresh {root}", flush=True)
                continue
            settle = a.settle if a.settle is not None else 45
            if settle > 0 and not store_settled(reg[root].get("stores") or [], settle, a.settle_max):
                print(f"{stamp} {root} still being written after {a.settle_max}s, leaving it "
                      f"for the next trigger", flush=True)
                continue
            print(f"{stamp} reindexing {root} ({reason})", flush=True)
            try:
                build_now(root, jobs=a.jobs, quiet=True)
                print(f"{stamp} done {root}", flush=True)
            except SystemExit as e:
                print(f"{stamp} failed: {e}", flush=True)
        return
    if a.status:
        p = plist_path()
        print(f"plist: {p}  ({'installed' if os.path.exists(p) else 'not installed'})")
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        print("loaded:", "yes" if LAUNCH_LABEL in out else "no")
        if os.path.exists(log):
            print(f"log: {log}")
            with open(log) as f:
                for line in f.readlines()[-5:]:
                    print("  " + line.rstrip())
        return
    if a.uninstall:
        _remove_legacy_agent()
        p = plist_path()
        subprocess.run(["launchctl", "unload", p], capture_output=True)
        if os.path.exists(p):
            os.remove(p)
        print(f"removed {p}")
        return
    # install
    _remove_legacy_agent()
    minutes = a.every or prj.effective_config().get("poll_minutes", 15)
    if a.every:
        prj.set_config("poll_minutes", a.every, None)
    os.makedirs(prj.CACHE_DIR, exist_ok=True)
    p = plist_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idxg.py")
    watch_paths = []
    if a.watch:
        for entry in prj.load_registry().values():
            for st in entry.get("stores") or []:
                units = os.path.join(st, "v5", "units")
                watch_paths.append(units if os.path.isdir(units) else st)
    watch_block = ""
    if watch_paths:
        joined = "\n".join(f"        <string>{w}</string>" for w in dict.fromkeys(watch_paths))
        watch_block = (f"    <key>WatchPaths</key>\n    <array>\n{joined}\n    </array>\n"
                       f"    <key>ThrottleInterval</key><integer>120</integer>\n")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCH_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script}</string>
        <string>autoindex</string>
        <string>--run-once</string>
    </array>
{watch_block}    <key>StartInterval</key><integer>{int(minutes) * 60}</integer>
    <key>RunAtLoad</key><false/>
    <key>LowPriorityIO</key><true/>
    <key>Nice</key><integer>5</integer>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    with open(p, "w") as f:
        f.write(body)
    subprocess.run(["launchctl", "unload", p], capture_output=True)
    r = subprocess.run(["launchctl", "load", p], capture_output=True, text=True)
    print(f"installed {p}")
    if watch_paths:
        print(f"  watches {len(set(watch_paths))} index store dir(s); a build that writes records")
        print(f"  wakes it, it waits for writes to stop, then reindexes")
    print(f"  also checks every {minutes} min as a fallback")
    print(f"  log: {log}")
    if r.returncode != 0:
        print("  launchctl load said:", (r.stderr or r.stdout).strip())


def build_parser():
    ap = argparse.ArgumentParser(prog="idxg", description="codebase-brain: a code graph, its history and its docs")
    ap.add_argument("--version", action="version", version=f"codebase-brain {VERSION}")
    ap.add_argument("--db", help="graph db path (default ~/.cache/codebase-brain/<project>.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="index status + coverage summary")
    p.add_argument("--json", action="store_true")
    p.add_argument("--exact", action="store_true",
                   help="recount rows instead of reading the values cached at build time")
    p.set_defaults(fn=cmd_status)

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
    p.add_argument("--max-rows", type=int, default=120,
                   help="cap printed rows so a wide trace stays readable (default 120)")
    p.add_argument("--max-bytes", type=int, default=8000,
                   help="cap printed bytes, keeping one call affordable for an agent")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_trace)

    p = sub.add_parser("refs", help="every occurrence of a symbol with roles")
    p.add_argument("symbol"); p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_refs)

    p = sub.add_parser("snippet", help="print a symbol's definition from disk")
    p.add_argument("symbol"); p.add_argument("--max-lines", type=int, default=200)
    p.add_argument("--max-bytes", type=int, default=6000,
                   help="cap printed bytes for a large definition (default 6000)")
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
    p.add_argument("--per-node-cap", type=int, default=None,
                   help="max edges kept per symbol per direction (default: config viz_per_node_cap)")
    p.add_argument("--title"); p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_viz)

    p = sub.add_parser("dead", help="symbols nothing in the indexed build reaches")
    p.add_argument("--kind", help="comma list, default: types, methods and properties")
    p.add_argument("--module")
    p.add_argument("--include-tests", action="store_true")
    p.add_argument("--include-vendor", action="store_true",
                   help="include vendored trees (third-party, Vendor, Pods)")
    p.add_argument("--lang", default="Swift",
                   help="Swift (default), ObjC, C, or any. ObjC selectors are dispatched "
                        "dynamically, so ObjC candidates are mostly false positives")
    p.add_argument("--verify", action="store_true",
                   help="drop candidates whose name appears in any other file (ripgrep)")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_dead)

    p = sub.add_parser("schema", help="print the db schema and edge/role vocabulary")
    p.set_defaults(fn=cmd_schema)

    p = sub.add_parser("history", help="commit history of the main branch, joined to the graph")
    hs = p.add_subparsers(dest="hcmd", required=True)
    h = hs.add_parser("build", help="extract git log (incremental) and sync repo docs")
    h.add_argument("--full", action="store_true", help="start over instead of continuing from the last head")
    h.add_argument("--branch", help="history branch (default: config history_branch, then main, master)")
    h.add_argument("--since", help="only commits after YYYY-MM-DD (cheaper first build)")
    h.add_argument("--all-commits", action="store_true",
                   help="every commit reachable from the branch, not one per merge")
    h.add_argument("--no-docs", dest="docs", action="store_false", default=True)
    h.add_argument("--no-prs", dest="prs", action="store_false", default=True,
                   help="skip fetching pull request descriptions through gh")
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_build)
    h = hs.add_parser("log", help="commits touching paths, a symbol's file, a module, an author")
    h.add_argument("paths", nargs="*", help="repo-relative files, directories (trailing /) or globs")
    h.add_argument("--symbol", help="follow the file that defines this symbol")
    h.add_argument("--module"); h.add_argument("--component", help="module-depth directory")
    h.add_argument("--author"); h.add_argument("--since"); h.add_argument("--until")
    h.add_argument("--grep", help="substring of subject, body or ticket")
    h.add_argument("--files", action="store_true", help="list changed files per commit")
    h.add_argument("--narrate", action="store_true",
                   help="one plain paragraph per commit: who, what, why (from the PR), which files")
    h.add_argument("--limit", type=int, default=30)
    h.add_argument("--max-bytes", type=int, default=12000,
                   help="cap printed bytes, keeping one call affordable for an agent")
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_log)
    h = hs.add_parser("show", help="one commit in full, by sha or #PR")
    h.add_argument("sha"); h.add_argument("--max-files", type=int, default=80)
    h.add_argument("--max-body", type=int, default=4000, help="cap the printed PR description")
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_show)
    h = hs.add_parser("digest", help="weekly digest: every change narrated, grouped by area")
    h.add_argument("--week", help="ISO week, e.g. 2026-W36 (default: the week of the last commit)")
    h.add_argument("--since"); h.add_argument("--until")
    h.add_argument("--list", action="store_true", help="list weeks with commit counts")
    h.add_argument("--limit", type=int, default=30, help="weeks shown by --list")
    h.add_argument("--html", nargs="?", const="auto", metavar="PATH",
                   help="write the digest page (the vault's weekly-digest layout); default path beside the db")
    h.add_argument("--open", action="store_true")
    h.add_argument("--max-bytes", type=int, default=0, help="cap the text output (0 = no cap)")
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_digest)
    h = hs.add_parser("churn", help="where change concentrates over a window")
    h.add_argument("--since", help="YYYY-MM-DD (default: 365 days ago)")
    h.add_argument("--by", choices=["module", "component", "file", "author"], default="module")
    h.add_argument("--ext", help="one extension, e.g. swift")
    h.add_argument("--limit", type=int, default=30)
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_churn)
    h = hs.add_parser("timeline", help="narrative history, one paragraph per period")
    h.add_argument("--periods", type=int, default=6, help="most recent periods (0 = all)")
    h.add_argument("--granularity", choices=["year", "quarter", "month"])
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_timeline)
    h = hs.add_parser("vault", help="write history and docs as knowledge-vault clippings")
    h.add_argument("--out", help="vault directory (default: config history_vault, else beside the db)")
    h.add_argument("--no-docs", dest="docs", action="store_false", default=True)
    h.add_argument("--no-history", dest="history", action="store_false", default=True)
    h.add_argument("--min-commits", type=int, default=3, help="modules with fewer commits get no note")
    h.add_argument("--dry-run", action="store_true")
    h.set_defaults(hfn=cmd_history_vault)
    p.set_defaults(fn=cmd_history, no_stale_check=True)

    p = sub.add_parser("docs", help="the repository's own markdown docs, searchable")
    ds = p.add_subparsers(dest="dcmd", required=True)
    d = ds.add_parser("list", help="docs by kind, module or path")
    d.add_argument("--module"); d.add_argument("--kind", help="readme, agent-note, skill, guide, doc")
    d.add_argument("--path", help="glob on the repo path")
    d.add_argument("--limit", type=int, default=100)
    d.add_argument("--json", action="store_true")
    d.set_defaults(hfn=cmd_docs_list)
    d = ds.add_parser("search", help="full-text search over doc content")
    d.add_argument("query"); d.add_argument("--limit", type=int, default=10)
    d.add_argument("--json", action="store_true")
    d.set_defaults(hfn=cmd_docs_search)
    d = ds.add_parser("show", help="print one doc")
    d.add_argument("path", help="repo path or a unique suffix of it")
    d.add_argument("--max-bytes", type=int, default=12000)
    d.set_defaults(hfn=cmd_docs_show)
    p.set_defaults(fn=cmd_docs, no_stale_check=True)

    p = sub.add_parser("init", help="index a project and install its skill + CLAUDE.md note")
    p.add_argument("path", nargs="?", help="project root (default: detected from the cwd)")
    p.add_argument("--jobs", type=int)
    p.add_argument("--no-build", dest="build", action="store_false", default=True)
    p.add_argument("--no-viz", dest="viz", action="store_false", default=True)
    p.add_argument("--no-skill", dest="skill", action="store_false", default=True)
    p.add_argument("--no-claude-md", dest="claude_md", action="store_false", default=True)
    p.add_argument("--claude-md-path", dest="claude_md_path",
                   help="write the agent note here instead of <project>/CLAUDE.md")
    p.set_defaults(fn=cmd_init, no_stale_check=True)

    p = sub.add_parser("deinit", help="remove what init installed in a project")
    p.add_argument("path", nargs="?")
    p.add_argument("--purge", action="store_true",
                   help="also delete the graph, history db, explorer, digests and the default vault export")
    p.add_argument("--force", action="store_true",
                   help="delete the project skill even when it has a Project notes section")
    p.set_defaults(fn=cmd_deinit, no_stale_check=True)

    p = sub.add_parser("config", help="show or change settings for this project")
    p.add_argument("assign", nargs="*", metavar="key=value")
    p.add_argument("--global", dest="scope", action="store_const", const="global",
                   default="project", help="write to the global config instead of this project")
    p.add_argument("--unset", nargs="+", metavar="key")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_config, no_stale_check=True)

    p = sub.add_parser("refresh", help="reindex when the index store has moved on")
    p.add_argument("--all", action="store_true", help="every registered project")
    p.add_argument("--force", action="store_true")
    p.add_argument("--jobs", type=int)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_refresh, no_stale_check=True)

    p = sub.add_parser("projects", help="list indexed projects and whether they are fresh")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_projects, no_stale_check=True)

    p = sub.add_parser("open", help="open the HTML explorer for this project")
    p.set_defaults(fn=cmd_open, no_stale_check=True)

    p = sub.add_parser("autoindex", help="background refresh via a launchd agent")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--run-once", action="store_true", help="what the agent runs on each tick")
    p.add_argument("--every", type=int, metavar="MINUTES")
    p.add_argument("--jobs", type=int)
    p.add_argument("--watch", action="store_true",
                   help="also trigger on index-store writes, so a build refreshes the graph")
    p.add_argument("--settle", type=int, metavar="SECONDS",
                   help="quiet period the store must hold before reindexing (default 45)")
    p.add_argument("--settle-max", type=int, default=1800, metavar="SECONDS",
                   help="give up waiting for quiet after this long (default 1800)")
    p.set_defaults(fn=cmd_autoindex, no_stale_check=True)
    return ap


if __name__ == "__main__":
    args = build_parser().parse_args()
    try:
        check_stale(args)
        args.fn(args)
    except BrokenPipeError:
        pass
