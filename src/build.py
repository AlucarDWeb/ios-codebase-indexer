"""Build a queryable SQLite graph from a Swift/clang index store."""
import argparse, hashlib, json, os, sqlite3, subprocess, sys, time
from ctypes import c_uint, c_void_p, byref
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import idxstore as ix
import project as prj

SKIP_KINDS = {25, 1000}  # Parameter, CommentTag

R_DECL, R_DEF, R_REF, R_READ, R_WRITE, R_CALL = 1, 2, 4, 8, 16, 32
R_DYNAMIC, R_ADDROF, R_IMPLICIT = 64, 128, 256
R_CHILDOF, R_BASEOF, R_OVERRIDEOF, R_RECEIVEDBY = 1 << 9, 1 << 10, 1 << 11, 1 << 12
R_CALLEDBY, R_EXTENDEDBY, R_ACCESSOROF, R_CONTAINEDBY = 1 << 13, 1 << 14, 1 << 15, 1 << 16
R_IBTYPEOF, R_SPECIALIZATIONOF = 1 << 17, 1 << 18

# relation bit -> (edge kind, flip) ; flip=True means related symbol is the source
REL_EDGES = [
    (R_CALLEDBY, "CALLS", True),
    (R_CHILDOF, "CONTAINS", True),
    (R_BASEOF, "INHERITS", True),
    (R_OVERRIDEOF, "OVERRIDES", False),
    (R_EXTENDEDBY, "EXTENDS", True),
    (R_ACCESSOROF, "ACCESSOR_OF", False),
    (R_RECEIVEDBY, "RECEIVED_BY", True),
    (R_CONTAINEDBY, "REFERENCES", True),
    (R_IBTYPEOF, "IB_TYPE_OF", True),
    (R_SPECIALIZATIONOF, "SPECIALIZES", False),
]


def h64(s):
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big", signed=True)


SHARD_SCHEMA = """
PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
CREATE TABLE IF NOT EXISTS symbols(usr_hash INTEGER PRIMARY KEY, usr TEXT, name TEXT, kind INTEGER,
  subkind INTEGER, lang INTEGER, props INTEGER);
CREATE TABLE IF NOT EXISTS files(path_hash INTEGER PRIMARY KEY, path TEXT);
CREATE TABLE IF NOT EXISTS defs(usr_hash INTEGER, path_hash INTEGER, line INTEGER, col INTEGER, module TEXT);
CREATE TABLE IF NOT EXISTS occurrences(usr_hash INTEGER, path_hash INTEGER, line INTEGER, col INTEGER, roles INTEGER);
CREATE TABLE IF NOT EXISTS edges(src INTEGER, dst INTEGER, kind TEXT, path_hash INTEGER, line INTEGER, col INTEGER);
"""


def _sym_tuple(sym):
    return (
        h64(ix.lib.indexstore_symbol_get_usr(sym).str()),
        ix.lib.indexstore_symbol_get_usr(sym).str(),
        ix.lib.indexstore_symbol_get_name(sym).str(),
        ix.lib.indexstore_symbol_get_kind(sym),
        ix.lib.indexstore_symbol_get_subkind(sym),
        ix.lib.indexstore_symbol_get_language(sym),
        ix.lib.indexstore_symbol_get_properties(sym),
    )


def scan_units(store_path, names):
    """Read unit metadata + record dependencies for a batch of unit names."""
    st = ix.store_open(store_path)
    units, records = [], {}
    err = c_void_p()
    for un in names:
        ur = ix.lib.indexstore_unit_reader_create(st, un.encode(), byref(err))
        if not ur:
            continue
        module = ix.lib.indexstore_unit_reader_get_module_name(ur).str()
        main = ix.lib.indexstore_unit_reader_get_main_file(ur).str()
        target = ix.lib.indexstore_unit_reader_get_target(ur).str()
        is_sys = bool(ix.lib.indexstore_unit_reader_is_system_unit(ur))
        is_mod = bool(ix.lib.indexstore_unit_reader_is_module_unit(ur))
        is_dbg = bool(ix.lib.indexstore_unit_reader_is_debug_compilation(ur))
        local = []

        @ix.DEP_APPLIER
        def dcb(_c, d):
            if ix.lib.indexstore_unit_dependency_get_kind(d) == ix.DEP_RECORD:
                local.append((ix.lib.indexstore_unit_dependency_get_name(d).str(),
                              ix.lib.indexstore_unit_dependency_get_filepath(d).str()))
            return True

        ix.lib.indexstore_unit_reader_dependencies_apply_f(ur, None, dcb)
        ix.lib.indexstore_unit_reader_dispose(ur)
        units.append((un, module, main, target, is_sys, is_mod, is_dbg, len(local)))
        for rname, rpath in local:
            records.setdefault(rname, (rpath, module, is_sys))
    ix.lib.indexstore_store_dispose(st)
    return units, records


def scan_records(shard_path, batch):
    """Extract symbols/defs/occurrences/edges for a batch of (store, record, file, module)."""
    stores = {}
    db = sqlite3.connect(shard_path)
    db.executescript(SHARD_SCHEMA)
    syms, files, defs, occs, edges = {}, {}, [], [], []
    err = c_void_p()
    for store_path, rname, rpath, module in batch:
        st = stores.get(store_path) or stores.setdefault(store_path, ix.store_open(store_path))
        ph = h64(rpath)
        files[ph] = rpath
        rr = ix.lib.indexstore_record_reader_create(st, rname.encode(), byref(err))
        if not rr:
            continue

        @ix.OCC_APPLIER
        def ocb(_c, o):
            sym = ix.lib.indexstore_occurrence_get_symbol(o)
            kind = ix.lib.indexstore_symbol_get_kind(sym)
            if kind in SKIP_KINDS:
                return True
            t = _sym_tuple(sym)
            uh = t[0]
            syms[uh] = t
            line, col = c_uint(), c_uint()
            ix.lib.indexstore_occurrence_get_line_col(o, byref(line), byref(col))
            ln, cl = line.value, col.value
            roles = ix.lib.indexstore_occurrence_get_roles(o)
            if roles & (R_DEF | R_DECL):
                defs.append((uh, ph, ln, cl, module))
            occs.append((uh, ph, ln, cl, roles))

            @ix.REL_APPLIER
            def rcb(_c2, r):
                rroles = ix.lib.indexstore_symbol_relation_get_roles(r)
                rsym = ix.lib.indexstore_symbol_relation_get_symbol(r)
                rt = _sym_tuple(rsym)
                if rt[3] in SKIP_KINDS:
                    return True
                syms[rt[0]] = rt
                for bit, ekind, flip in REL_EDGES:
                    if rroles & bit:
                        src, dst = (rt[0], uh) if flip else (uh, rt[0])
                        edges.append((src, dst, ekind, ph, ln, cl))
                return True

            ix.lib.indexstore_occurrence_relations_apply_f(o, None, rcb)
            return True

        ix.lib.indexstore_record_reader_occurrences_apply_f(rr, None, ocb)
        ix.lib.indexstore_record_reader_dispose(rr)
        if len(occs) > 400_000:
            _flush(db, syms, files, defs, occs, edges)
    _flush(db, syms, files, defs, occs, edges)
    db.commit()
    db.close()
    for st in stores.values():
        ix.lib.indexstore_store_dispose(st)
    return shard_path


def _flush(db, syms, files, defs, occs, edges):
    db.executemany("INSERT OR IGNORE INTO symbols VALUES(?,?,?,?,?,?,?)", syms.values())
    db.executemany("INSERT OR IGNORE INTO files VALUES(?,?)", files.items())
    db.executemany("INSERT INTO defs VALUES(?,?,?,?,?)", defs)
    db.executemany("INSERT INTO occurrences VALUES(?,?,?,?,?)", occs)
    db.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?)", edges)
    syms.clear(); files.clear(); defs.clear(); occs.clear(); edges.clear()


MAIN_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(path_hash INTEGER PRIMARY KEY, path TEXT, rel TEXT, in_repo INTEGER,
  module TEXT, ext TEXT);
CREATE TABLE IF NOT EXISTS units(name TEXT PRIMARY KEY, module TEXT, main_file TEXT, target TEXT,
  is_system INTEGER, is_module_unit INTEGER, is_debug INTEGER, records INTEGER);
CREATE TABLE IF NOT EXISTS symbols(usr_hash INTEGER PRIMARY KEY, usr TEXT, name TEXT, kind TEXT,
  lang TEXT, props INTEGER, def_path_hash INTEGER, def_line INTEGER, def_col INTEGER, module TEXT,
  in_deg INTEGER DEFAULT 0, out_deg INTEGER DEFAULT 0, call_in INTEGER DEFAULT 0, call_out INTEGER DEFAULT 0,
  ref_count INTEGER DEFAULT 0, in_repo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS occurrences(usr_hash INTEGER, path_hash INTEGER, line INTEGER, col INTEGER, roles INTEGER);
CREATE TABLE IF NOT EXISTS edges(src INTEGER, dst INTEGER, kind TEXT, path_hash INTEGER, line INTEGER,
  col INTEGER, n INTEGER DEFAULT 1);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_sym_usr ON symbols(usr);
CREATE INDEX IF NOT EXISTS ix_sym_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS ix_sym_module ON symbols(module);
CREATE INDEX IF NOT EXISTS ix_sym_def ON symbols(def_path_hash);
CREATE INDEX IF NOT EXISTS ix_edge_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON edges(dst, kind);
CREATE INDEX IF NOT EXISTS ix_edge_file ON edges(path_hash);
CREATE INDEX IF NOT EXISTS ix_occ_sym ON occurrences(usr_hash);
CREATE INDEX IF NOT EXISTS ix_occ_file ON occurrences(path_hash, line);
CREATE INDEX IF NOT EXISTS ix_files_rel ON files(rel);
"""


def camel_split(name):
    out, cur = [], ""
    for ch in name:
        if ch.isupper() and cur and not cur[-1].isupper():
            out.append(cur); cur = ch
        elif ch in "_.:$":
            if cur: out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur: out.append(cur)
    return " ".join(dict.fromkeys([name] + [w.lower() for w in out if len(w) > 1]))


def normalize(path, root, prefix_map):
    """Map an index-store path to (abs path, repo-relative path or None, in_repo flag)."""
    p = path
    for src, dst in prefix_map.items():
        if p.startswith(src):
            p = dst + p[len(src):]
            break
    if p.startswith("./"):
        p = p[2:]
    if not p.startswith("/"):
        cand = os.path.join(root, p) if root else p
        return cand, p, 1
    p = os.path.normpath(p)
    root_s = root.rstrip("/") if root else ""
    if root_s and p.startswith(root_s + "/"):
        return p, p[len(root_s) + 1:], 1
    marker = "/execroot/_main/"
    if marker in p:
        tail = p.split(marker, 1)[1]
        if not tail.startswith(("bazel-out/", "external/", "bazel-bin", "bazel-out")):
            if root_s and os.path.exists(os.path.join(root_s, tail)):
                return os.path.join(root_s, tail), tail, 1
    return p, None, 0


def _manifest(root, cfg):
    m = cfg.get("docs_manifest") or ""
    if not m:
        return None
    return m if os.path.isabs(os.path.expanduser(m)) else os.path.join(root, m)


def chunks(seq, n):
    k = max(1, (len(seq) + n - 1) // n)
    return [seq[i:i + k] for i in range(0, len(seq), k)]


def main():
    ap = argparse.ArgumentParser(prog="idxg-build")
    ap.add_argument("--root", help="project root (default: detected from the cwd)")
    ap.add_argument("--store", action="append", help="repeatable; defaults to every detected store")
    ap.add_argument("--db")
    ap.add_argument("--jobs", type=int, help="parallel workers (default: config jobs)")
    ap.add_argument("--limit-records", type=int, default=0)
    ap.add_argument("--include-system", action="store_true")
    ap.add_argument("--viz", dest="viz", action="store_true", default=None,
                    help="regenerate the HTML explorer after building (default: config viz_on_build)")
    ap.add_argument("--no-viz", dest="viz", action="store_false")
    ap.add_argument("--no-history", action="store_true",
                    help="skip the commit-history and docs refresh that follows the build")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = os.path.realpath(args.root) if args.root else prj.find_root()
    cfg = prj.effective_config(root)
    detected, prefix_map = prj.detect_stores(root)
    stores = args.store or detected
    if not stores:
        raise SystemExit(f"no index store found for {root}; pass --store, or build the project "
                         f"once so the compiler writes one")
    final_db = args.db or prj.db_for(root)
    os.makedirs(os.path.dirname(final_db), exist_ok=True)
    # Build into a scratch file and swap it in, so queries keep hitting the old graph
    # for the whole build instead of an empty one.
    db_path = final_db + ".building"
    jobs = args.jobs or cfg.get("jobs", 4)
    lock = final_db + ".lock"
    if os.path.exists(lock):
        age = time.time() - os.path.getmtime(lock)
        if age < 7200:
            with open(lock) as f:
                who = f.read().strip()
            raise SystemExit(f"another index build is running for this project "
                             f"({who}, {int(age)}s ago). Wait for it, or remove {lock}")
        os.remove(lock)
    with open(lock, "w") as f:
        f.write(f"pid {os.getpid()}")

    shard_dir = db_path + ".shards"
    os.makedirs(shard_dir, exist_ok=True)
    for f in os.listdir(shard_dir):
        os.remove(os.path.join(shard_dir, f))
    t0 = time.time()

    all_units, records = [], {}
    for store in stores:
        st = ix.store_open(store)
        names = ix.unit_names(st)
        ix.lib.indexstore_store_dispose(st)
        print(f"store: {store}\n  units: {len(names)}", flush=True)
        with ProcessPoolExecutor(args.jobs) as ex:
            for units, recs in ex.map(scan_units, [store] * args.jobs, chunks(names, args.jobs)):
                all_units.extend(units)
                for k, v in recs.items():
                    if k not in records:
                        records[k] = (store,) + v
    print(f"records: {len(records)} from {len(stores)} store(s)  ({time.time()-t0:.1f}s)", flush=True)

    batch = [(v[0], r, v[1], v[2]) for r, v in records.items() if args.include_system or not v[3]]
    if args.limit_records:
        batch = batch[:args.limit_records]
    print(f"extracting {len(batch)} records with {jobs} workers", flush=True)
    shard_paths = [os.path.join(shard_dir, f"s{i}.db") for i in range(jobs)]
    with ProcessPoolExecutor(jobs) as ex:
        list(ex.map(scan_records, shard_paths, chunks(batch, jobs)))
    print(f"extract done ({time.time()-t0:.1f}s)", flush=True)

    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    db = sqlite3.connect(db_path)
    db.executescript(MAIN_SCHEMA)
    db.executescript("""
      CREATE TABLE _sym(usr_hash INTEGER PRIMARY KEY, usr TEXT, name TEXT, kind INTEGER, subkind INTEGER,
        lang INTEGER, props INTEGER);
      CREATE TABLE _files(path_hash INTEGER PRIMARY KEY, path TEXT);
      CREATE TABLE defs(usr_hash INTEGER, path_hash INTEGER, line INTEGER, col INTEGER, module TEXT);
      CREATE UNIQUE INDEX ix_edge_uq ON edges(src, dst, kind, path_hash, line, col);
    """)
    db.executemany("INSERT OR REPLACE INTO units VALUES(?,?,?,?,?,?,?,?)", all_units)
    for i, sp in enumerate(shard_paths):
        if not os.path.exists(sp):
            continue
        db.execute("ATTACH ? AS sh", (sp,))
        db.execute("INSERT OR IGNORE INTO _sym SELECT * FROM sh.symbols")
        db.execute("INSERT OR IGNORE INTO _files SELECT * FROM sh.files")
        db.execute("INSERT INTO defs SELECT * FROM sh.defs")
        db.execute("INSERT INTO occurrences SELECT * FROM sh.occurrences")
        db.execute("INSERT OR IGNORE INTO edges SELECT src,dst,kind,path_hash,line,col,1 FROM sh.edges")
        db.commit()
        db.execute("DETACH sh")
        print(f"  merged shard {i+1}/{len(shard_paths)} ({time.time()-t0:.1f}s)", flush=True)

    db.execute("CREATE INDEX ix_defs_sym ON defs(usr_hash)")
    db.commit()
    kmap = dict(ix.SYMBOL_KIND)
    lmap = dict(ix.LANGUAGE)
    db.create_function("kindname", 1, lambda k: kmap.get(k, str(k)))
    db.create_function("langname", 1, lambda l: lmap.get(l, str(l)))
    rows = db.execute("SELECT path_hash, path FROM _files").fetchall()
    db.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",
                   [(ph,) + normalize(p, root, prefix_map) + (None, os.path.splitext(p)[1].lstrip("."))
                    for ph, p in rows])
    db.execute("""UPDATE files SET module = u.module FROM units u
                  WHERE u.main_file = (SELECT path FROM _files WHERE _files.path_hash = files.path_hash)""")
    db.execute("""INSERT INTO symbols(usr_hash, usr, name, kind, lang, props)
                  SELECT usr_hash, usr, name, kindname(kind), langname(lang), props FROM _sym""")
    db.commit()
    print(f"  symbols staged ({time.time()-t0:.1f}s)", flush=True)
    db.executescript("""
      CREATE TEMP TABLE best_def AS
      SELECT usr_hash, path_hash, line, col, module FROM (
        SELECT d.usr_hash, d.path_hash, d.line, d.col, d.module,
               ROW_NUMBER() OVER (PARTITION BY d.usr_hash
                 ORDER BY COALESCE(f.in_repo,0) DESC, d.line) AS rn
        FROM defs d LEFT JOIN files f ON f.path_hash = d.path_hash)
      WHERE rn = 1;
      CREATE UNIQUE INDEX ix_best_def ON best_def(usr_hash);
    """)
    db.execute("""UPDATE symbols SET def_path_hash = b.path_hash, def_line = b.line, def_col = b.col,
                  module = b.module FROM best_def b WHERE b.usr_hash = symbols.usr_hash""")
    db.execute("""UPDATE symbols SET in_repo = COALESCE(f.in_repo, 0) FROM files f
                  WHERE f.path_hash = symbols.def_path_hash""")
    db.commit()
    print(f"  def sites resolved ({time.time()-t0:.1f}s)", flush=True)
    db.executescript(INDEXES)
    db.commit()
    db.executescript("""
      CREATE TEMP TABLE deg_in AS SELECT dst AS uh, COUNT(*) AS n,
        SUM(CASE WHEN kind='CALLS' THEN 1 ELSE 0 END) AS c FROM edges GROUP BY dst;
      CREATE UNIQUE INDEX ix_deg_in ON deg_in(uh);
      CREATE TEMP TABLE deg_out AS SELECT src AS uh, COUNT(*) AS n,
        SUM(CASE WHEN kind='CALLS' THEN 1 ELSE 0 END) AS c FROM edges GROUP BY src;
      CREATE UNIQUE INDEX ix_deg_out ON deg_out(uh);
      CREATE TEMP TABLE occ_n AS SELECT usr_hash AS uh, COUNT(*) AS n FROM occurrences GROUP BY usr_hash;
      CREATE UNIQUE INDEX ix_occ_n ON occ_n(uh);
    """)
    db.execute("UPDATE symbols SET in_deg = d.n, call_in = d.c FROM deg_in d WHERE d.uh = symbols.usr_hash")
    db.execute("UPDATE symbols SET out_deg = d.n, call_out = d.c FROM deg_out d WHERE d.uh = symbols.usr_hash")
    db.execute("UPDATE symbols SET ref_count = o.n FROM occ_n o WHERE o.uh = symbols.usr_hash")
    db.commit()
    print(f"  degrees computed ({time.time()-t0:.1f}s)", flush=True)
    db.executescript("""
      CREATE VIRTUAL TABLE symbols_fts USING fts5(terms, content='');
      CREATE TABLE fts_map(rowid INTEGER PRIMARY KEY, usr_hash INTEGER);
    """)
    cur = db.execute("SELECT usr_hash, name FROM symbols WHERE name != ''")
    n = 0
    buf_f, buf_m = [], []
    for uh, name in cur.fetchall():
        n += 1
        buf_f.append((n, camel_split(name)))
        buf_m.append((n, uh))
        if len(buf_f) >= 50000:
            db.executemany("INSERT INTO symbols_fts(rowid, terms) VALUES(?,?)", buf_f)
            db.executemany("INSERT INTO fts_map VALUES(?,?)", buf_m)
            buf_f, buf_m = [], []
    db.executemany("INSERT INTO symbols_fts(rowid, terms) VALUES(?,?)", buf_f)
    db.executemany("INSERT INTO fts_map VALUES(?,?)", buf_m)
    db.execute("CREATE INDEX ix_fts_map ON fts_map(usr_hash)")
    db.executescript("DROP TABLE _sym; DROP TABLE _files;")
    for k, v in {
        "store_path": " ; ".join(stores), "repo_root": root, "project": os.path.basename(root.rstrip("/")),
        "format_version": str(ix.lib.indexstore_format_version()),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "prefix_map": json.dumps(prefix_map), "build_seconds": f"{time.time()-t0:.1f}",
        "units": str(len(all_units)), "records": str(len(batch)),
        "stores": json.dumps(stores),
        "store_signature": json.dumps(prj.store_signature(stores)),
    }.items():
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    db.commit()
    db.execute("ANALYZE")
    db.commit()
    stats = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("symbols", "edges", "occurrences", "defs", "files", "units")}
    # Counting 6M edge rows takes seconds, and coverage needs a git listing, so both are
    # measured once here and read back from meta instead of on every status call.
    for name, value in stats.items():
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"count_{name}", str(value)))
    tracked = covered = 0
    try:
        listing = subprocess.run(["git", "-C", root, "ls-files", "-z", "*.swift", "*.m",
                                  "*.h", "*.mm", "*.c", "*.cpp"],
                                 capture_output=True, text=True).stdout.split("\0")
        files = [f for f in listing if f]
        have = {r[0] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1 AND rel IS NOT NULL")}
        tracked = len(files)
        covered = sum(1 for f in files if f in have)
    except OSError:
        pass
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("coverage_tracked", str(tracked)))
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("coverage_covered", str(covered)))
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)",
               ("edge_kinds", json.dumps(dict(db.execute(
                   "SELECT kind, COUNT(*) FROM edges GROUP BY kind ORDER BY 2 DESC").fetchall()))))
    db.commit()
    db.close()
    # Both sides of the swap: the scratch file's journals, and the destination's, which a
    # reader may have left behind in WAL mode. Applying a stale WAL to a fresh database
    # reads back as "database disk image is malformed".
    for target in (db_path, final_db):
        for suffix in ("-wal", "-shm"):
            leftover = target + suffix
            if os.path.exists(leftover):
                os.remove(leftover)
    os.replace(db_path, final_db)
    db_path = final_db
    prj.register(root, db_path, stores, {"symbols": stats["symbols"], "edges": stats["edges"]})
    html = None
    if cfg.get("history_on_build", True) and not args.no_history:
        import history
        try:
            history.build(root, db_path, branch=cfg.get("history_branch") or None,
                          first_parent=cfg.get("history_first_parent", True),
                          prs=cfg.get("history_prs", True), manifest_path=_manifest(root, cfg))
        except SystemExit as e:
            print(f"history skipped: {e}", file=sys.stderr)
    if args.viz if args.viz is not None else cfg.get("viz_on_build", True):
        import viz
        import sqlite3 as _sq
        vdb = _sq.connect(f"file:{db_path}?mode=ro", uri=True)
        vdb.row_factory = _sq.Row
        data = viz.slice_data(vdb, scope=cfg.get("viz_scope") or None,
                              limit=cfg.get("viz_limit", 1500), edge_cap=30000,
                              per_node_cap=cfg.get("viz_per_node_cap", 25))
        viz.attach_history(data, db_path, weeks=cfg.get("viz_history_weeks", 26))
        html = os.path.join(os.path.dirname(db_path),
                            os.path.basename(db_path).replace(".db", "-explorer.html"))
        viz.render(data, html)
        vdb.close()
        prj.register(root, db_path, stores, {"html": html})
    print(json.dumps({"project": root, "db": db_path, "html": html,
                      "seconds": round(time.time() - t0, 1), **stats}, indent=2))
    for f in os.listdir(shard_dir):
        os.remove(os.path.join(shard_dir, f))
    os.rmdir(shard_dir)
    if os.path.exists(lock):
        os.remove(lock)


if __name__ == "__main__":
    main()
