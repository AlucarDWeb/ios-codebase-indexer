"""Commit history and repository documentation for an indexed project.

Both live in `<graph>-history.db` beside the code graph and join to it through the
graph's repo-relative paths: a commit touching `Modules/Feature/X/Sources/A.swift` lands
on module X because the graph says X's files live under that prefix. Nothing here knows a
repository's layout; prefixes are derived from the graph at build time.
"""
import collections, datetime, hashlib, json, os, re, sqlite3, statistics, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project as prj

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS commits(sha TEXT PRIMARY KEY, short TEXT, parents INTEGER, author TEXT,
  email TEXT, authored TEXT, committed TEXT, day TEXT, month TEXT, subject TEXT, body TEXT,
  pr INTEGER, tickets TEXT, files INTEGER, ins INTEGER, del INTEGER, seq INTEGER);
CREATE TABLE IF NOT EXISTS commit_files(sha TEXT, path TEXT, old_path TEXT, ins INTEGER, del INTEGER,
  module TEXT, component TEXT, ext TEXT);
CREATE TABLE IF NOT EXISTS components(component TEXT PRIMARY KEY, first_sha TEXT, first_date TEXT,
  last_sha TEXT, last_date TEXT, commits INTEGER, alive INTEGER, module TEXT);
CREATE TABLE IF NOT EXISTS module_rank(module TEXT PRIMARY KEY, prefix TEXT, layer TEXT, calls_in INTEGER,
  calls_out INTEGER, symbols INTEGER);
CREATE TABLE IF NOT EXISTS docs(path TEXT PRIMARY KEY, title TEXT, kind TEXT, module TEXT,
  component TEXT, author TEXT, published TEXT, mechanical_refreshed TEXT, hash TEXT, bytes INTEGER,
  content TEXT, synced_at TEXT);
CREATE INDEX IF NOT EXISTS ix_cf_path ON commit_files(path);
CREATE INDEX IF NOT EXISTS ix_cf_module ON commit_files(module);
CREATE INDEX IF NOT EXISTS ix_cf_component ON commit_files(component);
CREATE INDEX IF NOT EXISTS ix_cf_sha ON commit_files(sha);
CREATE INDEX IF NOT EXISTS ix_commits_committed ON commits(committed);
CREATE INDEX IF NOT EXISTS ix_commits_month ON commits(month);
CREATE INDEX IF NOT EXISTS ix_commits_author ON commits(author);
CREATE INDEX IF NOT EXISTS ix_commits_pr ON commits(pr);
CREATE INDEX IF NOT EXISTS ix_docs_module ON docs(module);
"""

COMMIT_INSERT = """INSERT OR REPLACE INTO commits(sha, short, parents, author, email, authored, committed, day,
  month, subject, body, pr, tickets, files, ins, del, seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
# Columns added after the first release; _migrate adds them to an older history db.
COMMIT_EXTRA = (("pr_title", "TEXT"), ("pr_body", "TEXT"), ("pr_labels", "TEXT"), ("pr_merged", "TEXT"),
                ("pr_author", "TEXT"))
PR_BODY_CAP = 6000
PR_BATCH = 40
# Top-level directories that hold build, CI and developer tooling rather than product code.
TOOLING_DIRS = {"bazel", "tools", "scripts", "fastlane", ".github", ".mise", "ci", ".ci", "buildsystem",
                "build", "sourcerytemplates", ".maestro", "gradle", "configs", ".circleci", ".buildkite"}

# Markdown that documents nothing about the project's own code.
DOC_EXCLUDES = ("CHANGELOG*", "**/CHANGELOG*", "LICENSE*", "**/LICENSE*", "**/node_modules/**",
                "**/Pods/**", "**/third-party/**", "**/third_party/**", "**/Vendor/**",
                "**/vendor/**", "**/.build/**", "**/DerivedData/**", "**/*.docc/**",
                "**/Carthage/**", "**/fastlane/report.xml")
DOC_BODY_CAP = 400_000
COMMIT_BODY_CAP = 4000
PR_RE = re.compile(r"\(#(\d+)\)\s*$")
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)")
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b")
REFRESHED_RE = re.compile(r"Last refreshed:\s*(\d{4}-\d{2}-\d{2})")
STOPWORDS = set("""the and for with from into this that when where which while make made adds added
add fix fixed fixes fixing remove removed removes removing update updated updates updating use
using used new old some more less only also just after before between over under about
into onto than then them they their there these those been being have has had was were will
would should could can not but are its it's via per all any each one two first second
ios app feature module modules code changes change support improve improved improvement
refactor refactoring cleanup clean tests test testing unit snapshot pr follow part
enable enabled disable disabled show hide hidden move moved rename renamed replace replaced
implement implemented implementation minor small typo wip merge branch main master release
version bump revert reverts crash crashes error errors issue issues bug bugs""".split())


def history_db_for(graph_db):
    return graph_db[:-3] + "-history.db" if graph_db.endswith(".db") else graph_db + "-history.db"


def connect(path, write=False):
    if not write and not os.path.exists(path):
        raise SystemExit(f"no history yet at {path}\n  run: idxg history build")
    uri = f"file:{path}" + ("" if write else "?mode=ro")
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    return db


def meta(db):
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}


def git(root, *args, check=True, text=True):
    r = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=text)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def resolve_branch(root, wanted=None):
    """The branch whose history is the project's history: configured, else main/master,
    else whatever origin calls its default, else the checked-out HEAD."""
    candidates = [wanted] if wanted else []
    candidates += ["main", "master", "origin/main", "origin/master"]
    for c in candidates:
        if c and subprocess.run(["git", "-C", root, "rev-parse", "--verify", "--quiet", c + "^{commit}"],
                                capture_output=True).returncode == 0:
            return c
    head = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if head:
        return head
    return "HEAD"


def remote_web(root):
    """https URL of the origin remote when it is a GitHub-style host, else ''."""
    url = git(root, "remote", "get-url", "origin", check=False).strip()
    if not url:
        return ""
    m = re.match(r"^(?:git@|ssh://git@|https://)([^/:]+)[:/](.+?)(?:\.git)?/?$", url)
    if not m:
        return ""
    return f"https://{m.group(1)}/{m.group(2)}"


# ---------------------------------------------------------------- module prefixes

def module_prefixes(graph_db):
    """Map every module in the graph to the directory its compiled files share.

    The common prefix of a module's files is cut before a `Sources` or `Tests` segment,
    since the compiled subset of a module may all sit in one of those while the module's
    docs and manifests sit one level up. Modules whose files share no directory are
    dropped: a path cannot be attributed to them.
    """
    out = {}
    if not graph_db or not os.path.exists(graph_db):
        return out
    g = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
    try:
        rows = g.execute("""SELECT module, rel FROM files
                            WHERE in_repo = 1 AND rel IS NOT NULL AND module IS NOT NULL AND module != ''""")
        per = collections.defaultdict(list)
        for module, rel in rows:
            per[module].append(rel.split("/")[:-1])
    finally:
        g.close()
    for module, dirs in per.items():
        prefix = dirs[0]
        for d in dirs[1:]:
            n = 0
            while n < len(prefix) and n < len(d) and prefix[n] == d[n]:
                n += 1
            prefix = prefix[:n]
            if not prefix:
                break
        for cut in ("Sources", "Source", "Tests", "Test", "src", "Src"):
            if cut in prefix:
                prefix = prefix[:prefix.index(cut)]
        if prefix:
            out[module] = "/".join(prefix)
    # Two modules on one directory (a target and its tests when both drop the Tests
    # segment) keep the one with more files; the loser is unmatchable, not wrong.
    by_prefix = {}
    for module, prefix in out.items():
        if prefix not in by_prefix or len(per[module]) > len(per[by_prefix[prefix]]):
            by_prefix[prefix] = module
    return {m: p for m, p in out.items() if by_prefix[p] == m}


class PathMapper:
    def __init__(self, prefixes):
        self.by_prefix = {p: m for m, p in prefixes.items()}
        depths = [p.count("/") + 1 for p in prefixes.values()]
        self.depth = int(statistics.median(depths)) if depths else 2
        self.cache = {}

    def map(self, path):
        """(module or None, component or None) for a repo-relative path."""
        if path in self.cache:
            return self.cache[path]
        parts = path.split("/")
        module = None
        for n in range(len(parts) - 1, 0, -1):
            cand = "/".join(parts[:n])
            if cand in self.by_prefix:
                module = self.by_prefix[cand]
                component = cand
                break
        else:
            component = "/".join(parts[:self.depth]) if len(parts) > self.depth else None
        self.cache[path] = (module, component)
        return module, component


# ---------------------------------------------------------------- git log parsing

REC, FIELD, END = "\x01", "\x1f", "\x02"
FORMAT = REC + FIELD.join(["%H", "%P", "%an", "%ae", "%aI", "%cI", "%s", "%b"]) + END


def _rename(path):
    """Resolve numstat's rename spelling to (new_path, old_path)."""
    if len(path) > 1 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
    if " => " not in path:
        return path, None
    m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", path)
    if m:
        pre, old, new, post = m.groups()
        return (pre + new + post).replace("//", "/"), (pre + old + post).replace("//", "/")
    old, new = path.split(" => ", 1)
    return new, old


def iter_log(root, revrange, first_parent=True, since=None):
    args = ["log", revrange, "--numstat", "--no-color", f"--format={FORMAT}"]
    if first_parent:
        args += ["--first-parent", "-m"]
    if since:
        args.append(f"--since={since}")
    proc = subprocess.Popen(["git", "-C", root, "-c", "core.quotepath=false"] + args, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace")
    buf = ""
    for chunk in iter(lambda: proc.stdout.read(1 << 16), ""):
        buf += chunk
        while True:
            start = buf.find(REC)
            if start < 0:
                break
            nxt = buf.find(REC, start + 1)
            if nxt < 0:
                break
            yield _parse_record(buf[start + 1:nxt])
            buf = buf[nxt:]
    if buf.startswith(REC):
        yield _parse_record(buf[1:])
    proc.wait()
    if proc.returncode != 0:
        raise SystemExit(f"git log failed: {proc.stderr.read()[:300]}")


def _parse_record(text):
    head, _, stat = text.partition(END)
    fields = head.split(FIELD)
    while len(fields) < 8:
        fields.append("")
    sha, parents, an, ae, aI, cI, subject, body = fields[:8]
    files = []
    for line in stat.strip("\n").split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        ins, dele, path = parts
        path, old = _rename(path.strip())
        files.append((path, old, int(ins) if ins.isdigit() else 0, int(dele) if dele.isdigit() else 0))
    return {"sha": sha, "parents": len(parents.split()) if parents else 0, "author": an, "email": ae,
            "authored": aI, "committed": cI, "subject": subject.strip(),
            "body": body.strip()[:COMMIT_BODY_CAP], "files": files}


def pr_number(subject):
    m = PR_RE.search(subject) or MERGE_PR_RE.match(subject)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- docs discovery

def doc_kind(path):
    base = os.path.basename(path).lower()
    if base == "claude.md" or base == "agents.md":
        return "agent-note"
    if base == "skill.md":
        return "skill"
    if base.startswith("readme"):
        return "readme"
    if base in ("context.md", "contributing.md", "architecture.md"):
        return "guide"
    return "doc"


def _glob_match(rel, pattern):
    import fnmatch
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch("/" + rel, "/" + pattern)


def discover_docs(root, manifest_path=None):
    """Every tracked markdown file, minus vendored trees and changelogs, or the include
    and exclude globs of a manifest when the project has one."""
    tracked = [f for f in git(root, "ls-files", "-z", "*.md", "*.MD", "*.markdown").split("\0") if f]
    includes, excludes = None, list(DOC_EXCLUDES)
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path) as f:
            man = json.load(f)
        inc = man.get("include") or []
        if inc and isinstance(inc[0], dict):
            inc = [g for group in inc for g in group.get("globs", [])]
        includes = inc or None
        excludes += man.get("exclude") or []
    out = []
    for rel in tracked:
        if any(_glob_match(rel, ex) for ex in excludes):
            continue
        if includes and not any(_glob_match(rel, g) for g in includes):
            continue
        out.append(rel)
    return sorted(out)


def doc_title(rel, content):
    for line in content.split("\n", 40)[:40]:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return os.path.splitext(os.path.basename(rel))[0]


def clipping_name(project, rel):
    """Flat note name from a repo path, in the shape the knowledge vaults use: segments
    joined with ' - ', hidden directories unhidden, illegal Obsidian characters dashed."""
    parts = rel.split("/")
    if parts[0] == ".claude" and len(parts) >= 3 and parts[1] == "skills":
        rest = [x for x in parts[3:] if x.lower() != "skill.md"]
        parts = ["Skill", parts[2]] + rest
    elif parts[0].startswith("."):
        parts[0] = parts[0][1:].capitalize()
    if parts[-1].lower().endswith(".md"):
        parts[-1] = parts[-1][:-3]
    if len(parts) == 1:
        parts = [project] + parts
    name = " - ".join(parts)
    name = "".join("-" if c in '/\\:#^[]|' else c for c in name if c not in '"<>?*')
    return " ".join(name.split())


def sync_docs(db, root, branch, mapper, manifest_path=None, log=print):
    now = datetime.date.today().isoformat()
    rels = discover_docs(root, manifest_path)
    have = dict(db.execute("SELECT path, hash FROM docs").fetchall())
    new = changed = 0
    for rel in rels:
        full = os.path.join(root, rel)
        try:
            with open(full, errors="replace") as f:
                content = f.read(DOC_BODY_CAP)
        except OSError:
            continue
        digest = hashlib.sha256(content.encode()).hexdigest()[:12]
        if have.get(rel) == digest:
            continue
        info = git(root, "log", "-1", "--format=%cs%x1f%an", branch, "--", rel, check=False).strip()
        published, _, author = info.partition(FIELD)
        m = REFRESHED_RE.search(content)
        module, component = mapper.map(rel)
        db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rel, doc_title(rel, content), doc_kind(rel), module, component, author, published,
                    m.group(1) if m else "", digest, len(content.encode()), content, now))
        if rel in have:
            changed += 1
        else:
            new += 1
    gone = set(have) - set(rels)
    if gone:
        db.executemany("DELETE FROM docs WHERE path = ?", [(g,) for g in gone])
    db.commit()
    log(f"docs: {len(rels)} matched, {new} new, {changed} changed, {len(gone)} removed")
    db.executescript("""
      DROP TABLE IF EXISTS docs_fts;
      CREATE VIRTUAL TABLE docs_fts USING fts5(path UNINDEXED, title, content);
      INSERT INTO docs_fts(path, title, content) SELECT path, title, content FROM docs;
    """)
    db.commit()
    return len(rels), new, changed, len(gone)


# ---------------------------------------------------------------- build

def build(root, graph_db, branch=None, first_parent=True, full=False, since=None,
          docs=True, prs=True, manifest_path=None, log=print):
    root = os.path.realpath(root)
    hdb_path = history_db_for(graph_db)
    os.makedirs(os.path.dirname(hdb_path), exist_ok=True)
    branch = resolve_branch(root, branch)
    head = git(root, "rev-parse", branch + "^{commit}").strip()
    t0 = time.time()
    db = sqlite3.connect(hdb_path)
    db.execute("PRAGMA journal_mode=DELETE")
    db.executescript(SCHEMA)
    _migrate(db)
    m = {r[0]: r[1] for r in db.execute("SELECT key, value FROM meta")}
    cursor = m.get("head_sha")
    same_shape = (m.get("branch") == branch and m.get("first_parent") == str(int(first_parent))
                  and m.get("since", "") == (since or ""))
    incremental = bool(cursor) and same_shape and not full
    if incremental and cursor != head:
        # A rewritten branch has no ancestor to continue from; start over rather than
        # keep commits that are no longer on it.
        anc = subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", cursor, head],
                             capture_output=True).returncode == 0
        incremental = anc
    if not incremental:
        db.executescript("DELETE FROM commits; DELETE FROM commit_files; DELETE FROM components;")
        revrange = branch
    else:
        revrange = f"{cursor}..{head}"
    mapper = PathMapper(module_prefixes(graph_db))
    log(f"history: {root}\n  branch: {branch} @ {head[:11]}  "
        f"({'incremental from ' + cursor[:11] if incremental else 'full history'})"
        + (f"  since {since}" if since else ""))
    log(f"  module prefixes: {len(mapper.by_prefix)} (component depth {mapper.depth})")

    seq = db.execute("SELECT COALESCE(MAX(seq), 0) FROM commits").fetchone()[0]
    n = 0
    crows, frows = [], []
    if cursor != head or not incremental:
        for rec in iter_log(root, revrange, first_parent=first_parent, since=since):
            n += 1
            seq += 1
            ins = sum(f[2] for f in rec["files"])
            dele = sum(f[3] for f in rec["files"])
            tickets = sorted(set(TICKET_RE.findall(rec["subject"])))
            crows.append((rec["sha"], rec["sha"][:11], rec["parents"], rec["author"], rec["email"],
                          rec["authored"], rec["committed"], rec["committed"][:10], rec["committed"][:7],
                          rec["subject"], rec["body"], pr_number(rec["subject"]), ",".join(tickets),
                          len(rec["files"]), ins, dele, seq))
            for path, old, i, d in rec["files"]:
                module, component = mapper.map(path)
                frows.append((rec["sha"], path, old, i, d, module, component,
                              os.path.splitext(path)[1].lstrip(".").lower()))
            if len(crows) >= 2000:
                db.executemany(COMMIT_INSERT, crows)
                db.executemany("INSERT INTO commit_files VALUES(?,?,?,?,?,?,?,?)", frows)
                db.commit()
                crows, frows = [], []
                log(f"  {n:,} commits ({time.time() - t0:.0f}s)")
        db.executemany(COMMIT_INSERT, crows)
        db.executemany("INSERT INTO commit_files VALUES(?,?,?,?,?,?,?,?)", frows)
        db.commit()
    log(f"  commits added: {n:,} ({time.time() - t0:.1f}s)")

    # Re-attribute every file on a full build or when the graph changed, since module
    # prefixes come from the graph and a rebuilt graph may know more modules.
    if not incremental or m.get("graph_prefix_count") != str(len(mapper.by_prefix)):
        rows = db.execute("SELECT DISTINCT path FROM commit_files").fetchall()
        db.executemany("UPDATE commit_files SET module = ?, component = ? WHERE path = ?",
                       [mapper.map(p[0]) + (p[0],) for p in rows])
        db.commit()
    _rebuild_components(db, root, branch, mapper)
    _rebuild_module_rank(db, graph_db, mapper)
    if docs:
        sync_docs(db, root, branch, mapper, manifest_path, log=log)
    if prs:
        enrich_prs(db, remote_web(root), log=log)

    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("commits", "commit_files", "components", "docs")}
    span = db.execute("SELECT MIN(day), MAX(day), COUNT(DISTINCT author) FROM commits").fetchone()
    for k, v in {"branch": branch, "head_sha": head, "repo_root": root,
                 "project": os.path.basename(root.rstrip("/")), "graph_db": graph_db,
                 "first_parent": str(int(first_parent)), "since": since or "",
                 "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "build_seconds": f"{time.time() - t0:.1f}",
                 "remote_web": remote_web(root), "graph_prefix_count": str(len(mapper.by_prefix)),
                 "component_depth": str(mapper.depth), "first_day": span[0] or "", "last_day": span[1] or "",
                 "authors": str(span[2] or 0), **{f"count_{k}": str(v) for k, v in counts.items()}}.items():
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    db.commit()
    if not incremental:
        db.execute("VACUUM")
    db.close()
    for suffix in ("-wal", "-shm"):
        if os.path.exists(hdb_path + suffix):
            os.remove(hdb_path + suffix)
    log(f"  wrote {hdb_path} ({os.path.getsize(hdb_path) / 1e6:.1f} MB, {time.time() - t0:.1f}s)")
    return hdb_path, counts


def _rebuild_components(db, root, branch, mapper):
    alive = set()
    out = git(root, "ls-tree", "-d", "-r", "--name-only", "-z", branch, check=False)
    for d in out.split("\0"):
        if d:
            alive.add(d)
    db.execute("DELETE FROM components")
    rows = db.execute("""SELECT cf.component, MIN(c.committed), MAX(c.committed), COUNT(DISTINCT cf.sha)
                         FROM commit_files cf JOIN commits c ON c.sha = cf.sha
                         WHERE cf.component IS NOT NULL GROUP BY cf.component""").fetchall()
    # SQLite fills a bare column from the row that produced MIN or MAX, which is what
    # picks the first and last commit here.
    firsts = {r[0]: r[2] for r in db.execute(
        """SELECT cf.component, MIN(c.committed), c.sha FROM commit_files cf JOIN commits c ON c.sha = cf.sha
           WHERE cf.component IS NOT NULL GROUP BY cf.component""")}
    lasts = {r[0]: r[2] for r in db.execute(
        """SELECT cf.component, MAX(c.committed), c.sha FROM commit_files cf JOIN commits c ON c.sha = cf.sha
           WHERE cf.component IS NOT NULL GROUP BY cf.component""")}
    db.executemany("INSERT INTO components VALUES(?,?,?,?,?,?,?,?)",
                   [(comp, firsts.get(comp), first[:10], lasts.get(comp), last[:10], n,
                     1 if comp in alive else 0, mapper.by_prefix.get(comp))
                    for comp, first, last, n in rows])
    db.commit()


# ---------------------------------------------------------------- queries

def period_of(day, granularity):
    if granularity == "year":
        return day[:4]
    if granularity == "quarter":
        return f"{day[:4]}-Q{(int(day[5:7]) - 1) // 3 + 1}"
    return day[:7]


def pick_granularity(first_day, last_day):
    if not first_day or not last_day:
        return "month"
    days = (datetime.date.fromisoformat(last_day) - datetime.date.fromisoformat(first_day)).days
    if days > 365 * 6:
        return "year"
    if days > 540:
        return "quarter"
    return "month"


def commits_for(db, paths=None, module=None, component=None, author=None, since=None,
                until=None, query=None, limit=50):
    where, args = [], []
    join = ""
    if paths:
        join = "JOIN commit_files cf ON cf.sha = c.sha"
        ors = []
        for p in paths:
            if p.endswith("/") or "*" in p:
                ors.append("cf.path GLOB ?")
                args.append(p + "*" if p.endswith("/") else p)
            else:
                ors.append("(cf.path = ? OR cf.old_path = ?)")
                args += [p, p]
        where.append("(" + " OR ".join(ors) + ")")
    if module or component:
        join = "JOIN commit_files cf ON cf.sha = c.sha"
        if module:
            where.append("cf.module = ?")
            args.append(module)
        if component:
            where.append("cf.component = ?")
            args.append(component)
    if author:
        where.append("(c.author LIKE ? OR c.email LIKE ?)")
        args += [f"%{author}%", f"%{author}%"]
    if since:
        where.append("c.day >= ?")
        args.append(since)
    if until:
        where.append("c.day <= ?")
        args.append(until)
    if query:
        where.append("(c.subject LIKE ? OR c.body LIKE ? OR c.tickets LIKE ?)")
        args += [f"%{query}%"] * 3
    sql = f"""SELECT DISTINCT c.sha, c.short, c.author, c.day, c.subject, c.pr, c.tickets, c.files, c.ins, c.del
              FROM commits c {join} {'WHERE ' + ' AND '.join(where) if where else ''}
              ORDER BY c.committed DESC LIMIT ?"""
    return db.execute(sql, args + [limit]).fetchall()


def files_of(db, sha, limit=200):
    return db.execute("""SELECT path, old_path, ins, del, module, component FROM commit_files WHERE sha = ?
                         ORDER BY ins + del DESC LIMIT ?""", (sha, limit)).fetchall()


def churn(db, since=None, by="module", limit=30, ext=None):
    col = {"module": "cf.module", "component": "cf.component", "file": "cf.path", "author": "c.author"}[by]
    where, args = [f"{col} IS NOT NULL"], []
    if since:
        where.append("c.day >= ?")
        args.append(since)
    if ext:
        where.append("cf.ext = ?")
        args.append(ext.lower())
    return db.execute(f"""SELECT {col} AS key, COUNT(DISTINCT c.sha) commits, SUM(cf.ins) ins, SUM(cf.del) del,
                                 COUNT(DISTINCT c.author) authors, MAX(c.day) last
                          FROM commit_files cf JOIN commits c ON c.sha = cf.sha
                          WHERE {' AND '.join(where)} GROUP BY 1 ORDER BY commits DESC LIMIT ?""",
                      args + [limit]).fetchall()


def monthly(db):
    return [(r[0], r[1], r[2]) for r in db.execute(
        "SELECT month, COUNT(*), COUNT(DISTINCT author) FROM commits GROUP BY month ORDER BY month")]


def _terms(subject):
    s = TICKET_RE.sub(" ", subject)
    s = PR_RE.sub(" ", s)
    s = re.sub(r"[\[\](){}<>:;,\.!?\"'`/\\|=+*&%$@~^]", " ", s)
    out = []
    for w in s.split():
        if len(w) < 4 or w.lower() in STOPWORDS or w.isdigit():
            continue
        out.append(w if any(c.isupper() for c in w[1:]) else w.lower())
    return out


def eras(db, granularity=None, limit=None):
    """One summary per period, oldest first, with the facts the narrative is written from."""
    m = meta(db)
    granularity = granularity or pick_granularity(m.get("first_day"), m.get("last_day"))
    per = collections.OrderedDict()
    global_terms, era_terms = collections.Counter(), {}
    for r in db.execute("SELECT day, author, subject, files, ins, del, pr, tickets, sha FROM commits ORDER BY committed"):
        key = period_of(r["day"], granularity)
        e = per.setdefault(key, {"period": key, "commits": 0, "prs": 0, "authors": collections.Counter(),
                                 "ins": 0, "del": 0, "first": r["day"], "last": r["day"],
                                 "tickets": collections.Counter(), "biggest": []})
        e["commits"] += 1
        e["prs"] += 1 if r["pr"] else 0
        e["authors"][r["author"]] += 1
        e["ins"] += r["ins"] or 0
        e["del"] += r["del"] or 0
        e["last"] = r["day"]
        for t in (r["tickets"] or "").split(","):
            if t:
                e["tickets"][t.split("-")[0]] += 1
        e["biggest"].append((-(r["ins"] or 0) - (r["del"] or 0), r["sha"], r["subject"], r["files"] or 0))
        if len(e["biggest"]) > 400:
            e["biggest"].sort()
            del e["biggest"][6:]
        terms = _terms(r["subject"])
        era_terms.setdefault(key, collections.Counter()).update(terms)
        global_terms.update(terms)
    total_terms = sum(global_terms.values()) or 1
    mod_rows = db.execute("""SELECT c.day, cf.module, COUNT(DISTINCT c.sha) FROM commit_files cf
                             JOIN commits c ON c.sha = cf.sha WHERE cf.module IS NOT NULL
                             GROUP BY substr(c.day, 1, 7), cf.module""").fetchall()
    per_mod = collections.defaultdict(collections.Counter)
    for day, module, n in mod_rows:
        per_mod[period_of(day, granularity)][module] += n
    comps = db.execute("SELECT component, first_date, last_date, commits, alive, module FROM components").fetchall()
    born, died = collections.defaultdict(list), collections.defaultdict(list)
    for c in comps:
        if c["commits"] < 3 or _fixture_dir(c["component"]):
            continue
        born[period_of(c["first_date"], granularity)].append((c["commits"], c["first_date"], c["component"], c["module"]))
        if not c["alive"]:
            died[period_of(c["last_date"], granularity)].append((c["commits"], c["last_date"], c["component"], c["module"]))
    out = []
    for key, e in per.items():
        e["biggest"].sort()
        et = era_terms.get(key, collections.Counter())
        n_et = sum(et.values()) or 1
        distinctive = sorted(((cnt / n_et) / (global_terms[w] / total_terms), w, cnt)
                             for w, cnt in et.items() if cnt >= 3)
        distinctive = [w for _, w, _ in sorted(distinctive, reverse=True)[:6]]
        out.append({"period": key, "commits": e["commits"], "prs": e["prs"], "first": e["first"], "last": e["last"],
                    "authors": len(e["authors"]), "top_authors": e["authors"].most_common(4),
                    "ins": e["ins"], "del": e["del"], "tickets": e["tickets"].most_common(4),
                    "modules": per_mod.get(key, collections.Counter()).most_common(6),
                    "born": [b[1:] for b in sorted(born.get(key, []), reverse=True)[:8]],
                    "died": [d[1:] for d in sorted(died.get(key, []), reverse=True)[:8]],
                    "terms": distinctive,
                    "biggest": [{"sha": s[:11], "subject": subj, "files": f, "lines": -neg}
                                for neg, s, subj, f in e["biggest"][:3]]})
    if limit:
        out = out[-limit:]
    return granularity, out


FIXTURE_SEGMENTS = {"snapshots", "__snapshots__", "references", "tests", "test", "fixtures", "mocks"}


def _fixture_dir(component):
    return any(seg.lower() in FIXTURE_SEGMENTS for seg in component.split("/"))


def _plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _period_label(key):
    if re.match(r"^\d{4}-Q\d$", key):
        return key.replace("-", " ")
    if re.match(r"^\d{4}-\d{2}$", key):
        return datetime.date(int(key[:4]), int(key[5:]), 1).strftime("%B %Y")
    return key


def narrate(m, granularity, era_list, all_eras=None):
    """Plain prose from the era facts. Every sentence is backed by a number in the db."""
    ranked = sorted(all_eras or era_list, key=lambda e: -e["commits"])
    busiest = ranked[0]["period"] if ranked else None
    paragraphs = []
    for e in era_list:
        s = []
        label = _period_label(e["period"])
        span = "" if e["first"] == e["last"] else f" between {e['first']} and {e['last']}"
        s.append(f"{label}: {_plural(e['commits'], 'commit')} from {_plural(e['authors'], 'author')} "
                 f"landed on {m.get('branch', 'main')}{span}"
                 + (", the busiest period in the history" if e["period"] == busiest else "") + ".")
        if e["prs"]:
            s.append(f"{e['prs']:,} of them carry a pull request number.")
        if e["ins"] or e["del"]:
            s.append(f"They added {e['ins']:,} lines and removed {e['del']:,}.")
        if e["modules"]:
            mods = ", ".join(f"{name} ({n})" for name, n in e["modules"][:5])
            s.append(f"Most touched modules, by commits: {mods}.")
        if e["terms"]:
            s.append("Words that stand out in this period's subjects compared with the rest of the history: "
                     + ", ".join(e["terms"]) + ".")
        if e["tickets"]:
            s.append("Ticket prefixes: " + ", ".join(f"{k} ({n})" for k, n in e["tickets"]) + ".")
        if e["born"]:
            s.append("Directories that first appear: " + "; ".join(
                f"{c} ({d})" + (f", now module {mod}" if mod else "") for d, c, mod in e["born"][:5]) + ".")
        if e["died"]:
            s.append("Directories last touched here and gone from the branch today: " + "; ".join(
                f"{c} ({d})" for d, c, _ in e["died"][:5]) + ".")
        if e["biggest"]:
            b = e["biggest"][0]
            s.append(f"Largest change: {b['subject'][:110]} ({b['sha']}, {b['files']:,} files, {b['lines']:,} lines).")
        if e["top_authors"]:
            s.append("Most active: " + ", ".join(f"{a} ({n})" for a, n in e["top_authors"][:3]) + ".")
        paragraphs.append({"period": e["period"], "label": label, "text": " ".join(s)})
    return paragraphs


def overview_text(db):
    m = meta(db)
    n = int(m.get("count_commits") or 0)
    authors = int(m.get("authors") or 0)
    first, last = m.get("first_day", ""), m.get("last_day", "")
    year_ago = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
    recent = db.execute("SELECT COUNT(*) FROM commits WHERE day >= ?", (year_ago,)).fetchone()[0]
    prs = db.execute("SELECT COUNT(*) FROM commits WHERE pr IS NOT NULL").fetchone()[0]
    alive = db.execute("SELECT COUNT(*) FROM components WHERE alive = 1 AND commits >= 3").fetchone()[0]
    dead = db.execute("SELECT COUNT(*) FROM components WHERE alive = 0 AND commits >= 3").fetchone()[0]
    docs = int(m.get("count_docs") or 0)
    kind = "first-parent commits" if m.get("first_parent") == "1" else "commits"
    parts = [f"The {m.get('branch', 'main')} branch of {m.get('project', 'this project')} holds "
             f"{n:,} {kind} between {first} and {last}, by {authors:,} authors."]
    if n:
        parts.append(f"{recent:,} of them ({100 * recent / n:.0f}%) landed in the last twelve months, "
                     f"and {prs:,} ({100 * prs / n:.0f}%) name a pull request.")
    parts.append(f"{alive:,} source directories at module depth are still present; {dead:,} that once "
                 f"had three or more commits are gone.")
    if docs:
        parts.append(f"The repository documents itself in {docs:,} markdown files, indexed here with "
                     f"their last-commit dates.")
    parts.append("Every count comes from `git log` on that branch, so rewritten history or squash "
                 "merges shape what a commit means here, and a file the build never compiled has no "
                 "module attribution.")
    return " ".join(parts)


def docs_list(db, module=None, kind=None, path_glob=None, limit=100):
    where, args = [], []
    if module:
        where.append("module = ?"); args.append(module)
    if kind:
        where.append("kind = ?"); args.append(kind)
    if path_glob:
        where.append("path GLOB ?"); args.append(path_glob)
    return db.execute(f"""SELECT path, title, kind, module, published, mechanical_refreshed, bytes FROM docs
                          {'WHERE ' + ' AND '.join(where) if where else ''}
                          ORDER BY kind, module, path LIMIT ?""", args + [limit]).fetchall()


def docs_search(db, query, limit=20):
    try:
        return db.execute("""SELECT d.path, d.title, d.kind, d.module, d.published,
                                    snippet(docs_fts, 2, '[', ']', ' ... ', 18) AS snip, bm25(docs_fts) AS rank
                             FROM docs_fts JOIN docs d ON d.path = docs_fts.path
                             WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?""", (query, limit)).fetchall()
    except sqlite3.OperationalError:
        safe = " ".join('"' + w.replace('"', '') + '"' for w in query.split())
        return db.execute("""SELECT d.path, d.title, d.kind, d.module, d.published,
                                    snippet(docs_fts, 2, '[', ']', ' ... ', 18) AS snip, bm25(docs_fts) AS rank
                             FROM docs_fts JOIN docs d ON d.path = docs_fts.path
                             WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?""", (safe, limit)).fetchall()


def doc_get(db, path):
    r = db.execute("SELECT * FROM docs WHERE path = ?", (path,)).fetchone()
    if r:
        return r
    rows = db.execute("SELECT * FROM docs WHERE path GLOB ? OR title = ? ORDER BY length(path) LIMIT 5",
                      (f"*{path}*", path)).fetchall()
    return rows[0] if len(rows) == 1 else rows



def _migrate(db):
    have = {r[1] for r in db.execute("PRAGMA table_info(commits)")}
    for col, typ in COMMIT_EXTRA:
        if col not in have:
            db.execute(f"ALTER TABLE commits ADD COLUMN {col} {typ}")
    db.commit()


def _rebuild_module_rank(db, graph_db, mapper):
    """Cross-module call totals per module, so digests can order areas the way the code
    depends on itself: modules many others call come first, leaf features later."""
    db.execute("DELETE FROM module_rank")
    if not graph_db or not os.path.exists(graph_db):
        db.commit()
        return
    g = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
    try:
        syms = dict(g.execute("""SELECT module, COUNT(*) FROM symbols WHERE in_repo = 1 AND module IS NOT NULL
                                 GROUP BY module""").fetchall())
        calls = collections.defaultdict(lambda: [0, 0])
        rows = g.execute("""SELECT a.module, b.module, COUNT(*) FROM edges e
                            JOIN symbols a ON a.usr_hash = e.src JOIN symbols b ON b.usr_hash = e.dst
                            WHERE e.kind = 'CALLS' AND a.in_repo = 1 AND b.in_repo = 1
                              AND a.module IS NOT NULL AND b.module IS NOT NULL AND a.module <> b.module
                            GROUP BY 1, 2""")
        for src, dst, n in rows:
            calls[src][1] += n
            calls[dst][0] += n
    finally:
        g.close()
    prefixes = {m: p for p, m in mapper.by_prefix.items()}
    db.executemany("INSERT OR REPLACE INTO module_rank VALUES(?,?,?,?,?,?)",
                   [(m, prefixes.get(m), _layer_of(prefixes.get(m)), calls[m][0], calls[m][1], n)
                    for m, n in syms.items()])
    db.commit()


def _layer_of(prefix):
    if not prefix:
        return None
    parts = prefix.split("/")
    return parts[1] if len(parts) >= 2 else parts[0]


# ---------------------------------------------------------------- PR descriptions

def gh_available():
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def enrich_prs(db, web, log=print, limit=None):
    """Fetch title, body, labels and merge date for every PR-numbered commit that has none
    yet, in batches through the GitHub GraphQL API via `gh`. Squash merges usually drop
    the PR description from the commit, and that description is where 'why' lives."""
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+)$", web or "")
    if not m:
        log("prs: origin is not a GitHub repository, skipping PR descriptions")
        return 0
    if not gh_available():
        log("prs: `gh` is missing or not logged in, skipping PR descriptions (gh auth login)")
        return 0
    owner, name = m.groups()
    todo = [r[0] for r in db.execute("""SELECT DISTINCT pr FROM commits WHERE pr IS NOT NULL AND pr_body IS NULL
                                        ORDER BY committed DESC""")]
    if limit:
        todo = todo[:limit]
    if not todo:
        log("prs: descriptions up to date")
        return 0
    log(f"prs: fetching {len(todo):,} descriptions from {owner}/{name}")
    done, t0 = 0, time.time()
    for i in range(0, len(todo), PR_BATCH):
        batch = todo[i:i + PR_BATCH]
        fields = " ".join(f'p{n}: pullRequest(number: {n}) {{ title body mergedAt author {{ login }} '
                          f'labels(first: 12) {{ nodes {{ name }} }} }}' for n in batch)
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
        try:
            out = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                                 capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"prs: stopped after {done:,}: {e}")
            break
        try:
            data = json.loads(out.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        repo = (data.get("data") or {}).get("repository") or {}
        if not repo and out.returncode != 0 and "NOT_FOUND" not in (out.stdout + out.stderr):
            log(f"prs: stopped after {done:,}: {out.stderr.strip()[:200] or out.stdout[:200]}")
            break
        rows = []
        for n in batch:
            pr = repo.get(f"p{n}")
            if not pr:
                rows.append(("", "", "", "", "", n))
                continue
            labels = ",".join(l["name"] for l in (pr.get("labels") or {}).get("nodes") or [])
            rows.append((pr.get("title") or "", (pr.get("body") or "")[:PR_BODY_CAP], labels,
                         (pr.get("mergedAt") or "")[:10], (pr.get("author") or {}).get("login") or "", n))
        db.executemany("""UPDATE commits SET pr_title = ?, pr_body = ?, pr_labels = ?, pr_merged = ?,
                          pr_author = ? WHERE pr = ?""", rows)
        db.commit()
        done += len(batch)
        if done % (PR_BATCH * 10) == 0 or done == len(todo):
            log(f"  {done:,}/{len(todo):,} ({time.time() - t0:.0f}s)")
    return done


PLACEHOLDER_RE = re.compile(r"^\s*[-*]?\s*_.*_\s*$")
MEDIA_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img[^>]*>|<video[^>]*>.*?</video>|https?://\S+\.(?:png|gif|jpe?g|mov|mp4)\S*",
                      re.DOTALL | re.IGNORECASE)


# Lines a PR template leaves behind when nobody filled them in.
PLACEHOLDER_MARKS = ("_here", "here_", "yourtaskid", "add here a list", "describe what your", "explain what did you",
                     "all pr sections are mandatory", "put \"nothing else\"", "etc...", "link to previous pr part")


def _clean_md(text):
    # Long underscore-italic spans, across lines too, are template guidance; real emphasis is short.
    text = re.sub(r"_[^_\n][^_]{24,}?_", "", text or "", flags=re.DOTALL)
    lines = []
    for ln in text.split("\n"):
        st = ln.strip()
        low = st.lower()
        if re.match(r"^[-*]?\s*(\*\*)?_[^_].*_(\*\*)?$", st) or any(k in low for k in PLACEHOLDER_MARKS):
            continue
        lines.append(ln)
    text = "\n".join(lines)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<details>.*?</details>", "", text, flags=re.DOTALL)
    text = MEDIA_RE.sub("", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)
    text = re.sub(r"[*_]{1,3}([^*_\n]+)[*_]{1,3}", r"\1", text)
    return text


def body_digest(body, cap=360):
    """The sentences of a PR or commit body that say what changed and why: a section whose
    heading mentions what/why/context when the body follows a template, else the first
    paragraphs. Template placeholders, media, checklists and trailers are dropped."""
    text = _clean_md(body)
    if not text.strip():
        return ""
    sections = re.split(r"\n\s*#{1,6}\s+", "\n" + text)
    chosen = []
    if len(sections) > 1:
        ranked = []
        for sec in sections[1:]:
            heading, _, rest = sec.partition("\n")
            h = heading.lower()
            score = 3 if any(k in h for k in ("what", "why", "context", "summary", "description", "change")) else \
                    0 if any(k in h for k in ("test", "screenshot", "video", "checklist", "next", "todo", "reviewer")) else 1
            ranked.append((-score, len(ranked), rest))
        ranked.sort()
        chosen = [r[2] for r in ranked if -r[0] >= 1]
    else:
        chosen = [sections[0]]
    out = []
    for block in chosen:
        for para in re.split(r"\n\s*\n", block):
            lines = []
            for ln in para.split("\n"):
                st = ln.strip()
                if not st or PLACEHOLDER_RE.match(st) or st.startswith(("- [ ]", "- [x]", "[ ]", "[x]")):
                    continue
                if re.match(r"^(co-authored-by|signed-off-by|reviewed-by|fixes|closes|resolves|jira|ticket):", st, re.I):
                    continue
                if st.startswith(("|", "```", "---")) or not re.search(r"[A-Za-z]{3}", st):
                    continue
                lines.append(re.sub(r"^[-*+]\s+|^\d+[.)]\s+", "", st))
            if lines:
                out.append(" ".join(lines))
        if sum(len(x) for x in out) >= cap:
            break
    text = " ".join(" ".join(out).split())
    if len(text) > cap:
        text = text[:cap].rsplit(" ", 1)[0].rstrip(",;:") + " ..."
    return text


# ---------------------------------------------------------------- per-commit narration

TEST_SEGMENTS = {"tests", "test", "snapshots", "snapshottests", "__snapshots__", "uitests", "specs", "fixtures", "mocks"}
DOC_EXTS = {"md", "markdown", "txt", "rst"}
BUILD_EXTS = {"bazel", "bzl", "podspec", "yml", "yaml", "toml", "json", "xcconfig", "rb", "sh", "lock", "pbxproj", "swift.build"}
SOURCE_EXTS = {"swift", "m", "mm", "h", "c", "cpp", "kt", "java", "py", "js", "ts"}


KIND_WORDS = {"sources": ("source files", "source file"), "tests": ("test files", "test file"),
              "docs": ("docs", "doc"), "build": ("build files", "build file"), "other": ("other files", "other file")}


def clean_subject(subject):
    s = PR_RE.sub("", subject)
    s = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", s)
    s = re.sub(r"^\s*(feat|fix|chore|refactor|test|docs|build|ci|perf|style)(\([^)]*\))?!?:\s*", "", s, flags=re.I)
    return s.strip() or subject


def file_profile(files):
    """Counts by role for a commit's files: sources, tests, docs, build, other, plus renames."""
    prof = collections.Counter()
    modules = collections.Counter()
    for f in files:
        path = f["path"] if not isinstance(f, tuple) else f[0]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        segs = {s.lower() for s in path.split("/")[:-1]}
        top = path.split("/")[0].lower()
        if segs & TEST_SEGMENTS or re.search(r"(Spec|Tests?|Snapshot)\.\w+$", path):
            prof["tests"] += 1
        elif ext in DOC_EXTS:
            prof["docs"] += 1
        elif top in TOOLING_DIRS or ext in BUILD_EXTS or path.split("/")[-1] in ("BUILD", "BUILD.bazel", "Package.swift", "Podfile", "Makefile"):
            prof["build"] += 1
        elif ext in SOURCE_EXTS:
            prof["sources"] += 1
        else:
            prof["other"] += 1
        mod = f["module"] if not isinstance(f, tuple) else None
        if mod:
            modules[mod] += 1
        if (f["old_path"] if not isinstance(f, tuple) else None):
            prof["renamed"] += 1
    return prof, modules


def commit_tag(row, prof):
    """At most one digest tag: prod for hotfixes, build for tooling-only, test for test-only,
    train for one slice of a numbered series."""
    subj = (row["subject"] or "").lower()
    labels = (row["pr_labels"] or "").lower() if "pr_labels" in row.keys() else ""
    if "hotfix" in subj or "hotfix" in labels:
        return "prod"
    code = prof["sources"] + prof["tests"] + prof["docs"] + prof["other"]
    if prof["build"] and code == 0:
        return "build"
    if prof["tests"] and prof["sources"] == 0 and prof["build"] == 0 and prof["docs"] == 0:
        return "test"
    if re.search(r"\b(part|step|slice)\s*\d+|\(\d+/\d+\)|\btrain\b", subj):
        return "train"
    return None


def narrate_commit(row, files, web=""):
    """One plain paragraph about one commit, every clause backed by a field of the row."""
    prof, modules = file_profile(files)
    keys = row.keys()
    who = row["author"]
    pr = row["pr"]
    what = clean_subject(row["subject"])
    if pr and "pr_title" in keys and row["pr_title"] and clean_subject(row["pr_title"]) != what:
        what = clean_subject(row["pr_title"])
    lead = f"On {row['day']} {who} merged " + (f"pull request #{pr}" if pr else f"commit {row['short']}")
    if row["tickets"]:
        lead += f" for {row['tickets'].replace(',', ', ')}"
    lead += f": {what.rstrip('.')}."
    parts = [lead]
    body = ""
    if "pr_body" in keys and row["pr_body"]:
        body = body_digest(row["pr_body"])
    if not body and row["body"]:
        body = body_digest(row["body"])
    if body:
        parts.append(body if body.endswith((".", "!", "?", "...")) else body + ".")
    n = row["files"] or 0
    if n:
        kinds = [f"{v} {KIND_WORDS[k][1 if v == 1 else 0]}" for k, v in prof.most_common() if k != "renamed" and v]
        if len(files) < n:
            kinds.append(f"profile of the {len(files)} largest")
        touch = f"It touched {n:,} file{'s' if n != 1 else ''}"
        if modules:
            mods = modules.most_common(3)
            names = ", ".join(m for m, _ in mods)
            rest = len(modules) - len(mods)
            touch += f" in {names}" + (f" and {rest} more module{'s' if rest != 1 else ''}" if rest > 0 else "")
        if kinds:
            touch += f" ({', '.join(kinds)})"
        touch += f", adding {row['ins'] or 0:,} lines and removing {row['del'] or 0:,}."
        if prof["renamed"]:
            touch += f" {prof['renamed']} file{'s were' if prof['renamed'] != 1 else ' was'} moved or renamed."
        parts.append(touch)
    if "pr_labels" in keys and row["pr_labels"]:
        parts.append("Labels: " + row["pr_labels"].replace(",", ", ") + ".")
    return " ".join(parts)


def narrated_commits(db, rows, web="", files_cap=200):
    out = []
    for r in rows:
        full = db.execute("SELECT * FROM commits WHERE sha = ?", (r["sha"],)).fetchone()
        files = files_of(db, r["sha"], files_cap)
        out.append({"sha": full["sha"], "short": full["short"], "day": full["day"], "author": full["author"],
                    "pr": full["pr"], "tickets": full["tickets"], "subject": full["subject"],
                    "url": f"{web}/pull/{full['pr']}" if web and full["pr"] else "",
                    "text": narrate_commit(full, files, web)})
    return out


# ---------------------------------------------------------------- weekly digest

def iso_week(day):
    d = datetime.date.fromisoformat(day)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(label):
    """Monday and Sunday of an ISO week label like 2026-W36."""
    y, w = label.split("-W")
    monday = datetime.date.fromisocalendar(int(y), int(w), 1)
    return monday.isoformat(), (monday + datetime.timedelta(days=6)).isoformat()


def weeks(db, limit=None):
    rows = db.execute("SELECT day, COUNT(*) FROM commits GROUP BY day ORDER BY day").fetchall()
    per = collections.OrderedDict()
    for day, n in rows:
        per[iso_week(day)] = per.get(iso_week(day), 0) + n
    out = [(w, n) for w, n in per.items()]
    return out[-limit:] if limit else out


def _fmt_range(start, end):
    a, b = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    if a.year != b.year:
        return f"{a.day} {a.strftime('%b')} {a.year} to {b.day} {b.strftime('%b')} {b.year}"
    if a.month != b.month:
        return f"{a.day} {a.strftime('%b')} to {b.day} {b.strftime('%b')} {a.year}"
    return f"{a.day} to {b.day} {a.strftime('%b')} {a.year}"


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "area"


def _html(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;")


def week_digest(db, start, end, web=""):
    """The digest data object in the shape the weekly-digest template renders. Every field
    is computed from the commits in the window; nothing is interpreted."""
    m = meta(db)
    project = m.get("project", "project")
    rows = db.execute("""SELECT * FROM commits WHERE day >= ? AND day <= ? ORDER BY committed""",
                      (start, end)).fetchall()
    ranks = {r["module"]: r for r in db.execute("SELECT * FROM module_rank")}
    prefixes = {r["prefix"]: r["module"] for r in ranks.values() if r["prefix"]}
    areas = collections.OrderedDict()
    findings_pool = []
    no_body = 0
    unattributed_files = 0
    total_files = 0
    authors = collections.Counter()
    per_day = collections.Counter()

    def area_for(row, files, prof, modules):
        """Area key and ordering class: tooling first, then modules by how much the rest of
        the code depends on them, product features later, test and upkeep last."""
        top_segments = collections.Counter(f["path"].split("/")[0].lower() for f in files)
        code = prof["sources"] + prof["tests"] + prof["docs"] + prof["other"]
        if prof["build"] and code == 0 or (files and all(s in TOOLING_DIRS for s in top_segments)):
            return ("Build and tooling", "Build & tooling", 0, 0)
        if prof["tests"] and not prof["sources"] and not prof["build"] and not prof["docs"]:
            return ("Tests and upkeep", "Tidying up", 9, 0)
        if prof["docs"] and not prof["sources"] and not prof["build"] and not prof["tests"]:
            return ("Documentation", "Tidying up", 9, 1)
        if modules:
            mod = modules.most_common(1)[0][0]
            rank = ranks.get(mod)
            ci = rank["calls_in"] if rank else 0
            co = rank["calls_out"] if rank else 0
            share = ci / (ci + co + 1)
            layer = (rank["layer"] if rank and rank["layer"] else "Modules")
            # High inbound share means the rest of the code depends on it (shared, service);
            # low share means a leaf that people use (feature).
            klass = 2 if share >= 0.5 else 5
            return (mod, layer, klass, -share)
        comps = collections.Counter(f["component"] for f in files if f["component"])
        if comps:
            comp = comps.most_common(1)[0][0]
            return (comp, comp.split("/")[0], 6, 0)
        return ("Other changes", "Elsewhere", 8, 0)

    for row in rows:
        files = files_of(db, row["sha"], 400)
        prof, modules = file_profile(files)
        total_files += len(files)
        unattributed_files += sum(1 for f in files if not f["module"])
        authors[row["author"]] += 1
        per_day[row["day"]] += 1
        key, layer, klass, order = area_for(row, files, prof, modules)
        a = areas.setdefault(key, {"name": key, "layer": layer, "klass": klass, "order": order, "rows": [],
                                   "ins": 0, "del": 0, "authors": collections.Counter(), "files": 0})
        tag = commit_tag(row, prof)
        why = narrate_commit(row, files, web)
        has_body = bool((row["pr_body"] if "pr_body" in row.keys() else "") or row["body"])
        if not has_body:
            no_body += 1
        n = f"#{row['pr']}" if row["pr"] else row["short"][:7]
        title = clean_subject(row["pr_title"] if ("pr_title" in row.keys() and row["pr_title"]) else row["subject"])
        entry = {"n": n, "t": title, "w": _html(why), "tag": tag, "sha": row["sha"], "day": row["day"],
                 "lines": (row["ins"] or 0) + (row["del"] or 0), "url": f"{web}/pull/{row['pr']}" if web and row["pr"] else ""}
        if not tag:
            entry.pop("tag")
        a["rows"].append(entry)
        a["ins"] += row["ins"] or 0
        a["del"] += row["del"] or 0
        a["authors"][row["author"]] += 1
        a["files"] += len(files)
        fixish = bool(re.search(r"\b(fix|hotfix|crash|regression|broken|revert)\b", row["subject"], re.I))
        findings_pool.append((0 if tag == "prod" else 1 if fixish else 2, -entry["lines"], key, entry, tag))

    ordered = sorted(areas.values(), key=lambda a: (a["klass"], a["order"], -len(a["rows"])))
    ordered = _fold_small_areas(ordered)
    out_areas = []
    for a in ordered:
        top_authors = ", ".join(n for n, _ in a["authors"].most_common(3))
        tagged = collections.Counter(r.get("tag") for r in a["rows"] if r.get("tag"))
        note = (f"{len(a['rows'])} change{'s' if len(a['rows']) != 1 else ''} by "
                f"{len(a['authors'])} author{'s' if len(a['authors']) != 1 else ''} ({top_authors}), "
                f"touching {a['files']:,} files and adding {a['ins']:,} lines while removing {a['del']:,}.")
        if tagged:
            note += " Of these, " + ", ".join(f"{v} {'are' if v != 1 else 'is'} {CHIP_WORDS[k]}" for k, v in tagged.most_common()) + "."
        rank = ranks.get(a["name"])
        if rank and (rank["calls_in"] or rank["calls_out"]):
            note += (f" In the code graph {a['name']} receives {rank['calls_in']:,} cross-module calls and makes "
                     f"{rank['calls_out']:,}, which is why it sits {'early' if a['klass'] <= 2 else 'late'} in this list.")
        area = {"id": _slug(a["name"]), "name": a["name"], "layer": a["layer"], "note": _html(note),
                "prs": [{k: v for k, v in r.items() if k in ("n", "t", "w", "tag")} for r in a["rows"]],
                "commits": [{k: v for k, v in r.items() if k in ("sha", "day", "url", "n")} for r in a["rows"]]}
        if a.get("groups"):
            area["groups"] = a["groups"]
        out_areas.append(area)

    findings_pool.sort(key=lambda f: (f[0], f[1]))
    findings = []
    seen = set()
    for prio, _, area, entry, tag in findings_pool:
        if prio == 2 and len(findings) >= 5:
            break
        if entry["sha"] in seen:
            continue
        seen.add(entry["sha"])
        kind = "build" if (tag == "build" or area == "Build and tooling") else "prod"
        findings.append({"n": entry["n"].lstrip("#"), "area": area, "kind": kind, "title": entry["t"],
                         "body": entry["w"]})
        if len(findings) >= 8:
            break
    if len(findings) < 3:
        findings = findings[:len(findings)]

    n = len(rows)
    fixes = sum(1 for f in findings_pool if f[0] <= 1)
    days = sorted(per_day)
    prs = [r["pr"] for r in rows if r["pr"]]
    stats = [{"label": "Changes shipped", "value": str(n),
              "sub": f"numbered {min(prs)}–{max(prs)}" if prs else f"commits on {m.get('branch', 'main')}"},
             {"label": "Days with changes", "value": str(len(days)),
              "sub": f"{datetime.date.fromisoformat(days[0]).strftime('%a %d')} to {datetime.date.fromisoformat(days[-1]).strftime('%a %d')}" if days else "none"},
             {"label": "Areas touched", "value": str(len(out_areas)),
              "sub": (f"from {out_areas[0]['name']} to {out_areas[-1]['name']}" if len(out_areas) > 1 else "")},
             {"label": "Fixes", "value": str(fixes), "sub": "by subject or hotfix label"},
             {"label": "Authors", "value": str(len(authors)), "sub": ", ".join(a for a, _ in authors.most_common(2))}]
    top_area = max(out_areas, key=lambda a: len(a["prs"])) if out_areas else None
    headline = (f"{n} change{'s' if n != 1 else ''} across {len(out_areas)} area{'s' if len(out_areas) != 1 else ''}"
                + (f", {fixes} of them fixes." if fixes else "."))
    lede_parts = []
    if top_area:
        lede_parts.append(f"Most of the week went into <em>{_html(top_area['name'])}</em> with {len(top_area['prs'])} changes")
        second = sorted(out_areas, key=lambda a: -len(a["prs"]))[1:3]
        if second:
            lede_parts[-1] += ", followed by " + " and ".join(f"{_html(a['name'])} ({len(a['prs'])})" for a in second)
        lede_parts[-1] += "."
    lede_parts.append(f"{len(authors)} people merged to {m.get('branch', 'main')}; the busiest day was "
                      f"{datetime.date.fromisoformat(per_day.most_common(1)[0][0]).strftime('%A %d %B')} with "
                      f"{per_day.most_common(1)[0][1]} changes." if per_day else "No changes landed in this window.")
    lede_parts.append("Every sentence below is computed from the commit log and the pull request descriptions; "
                      "nothing is interpreted.")
    gaps = []
    if no_body:
        gaps.append(f"{no_body} of {n} changes have no description beyond their title, so their entry says only "
                    f"what files moved. " + ("Run <em>idxg history build</em> with <em>gh</em> logged in to pull pull-request descriptions."
                                             if not any(r["pr_body"] for r in rows if "pr_body" in r.keys()) else ""))
    if unattributed_files:
        gaps.append(f"{unattributed_files:,} of {total_files:,} changed files belong to no module the compiled index knows, "
                    f"so they count under a directory or under Other changes rather than a module.")
    gaps.append(f"Areas are ordered by how much the rest of the code calls them, from the compiler's index, "
                f"not by product importance. Tags come from subjects and labels: a fix without the word in its title is untagged.")
    gaps.append(f"Anything merged after {m.get('last_day', '')} (the last extracted commit) is not included.")
    label = f"week of {datetime.date.fromisoformat(start).strftime('%-d %B %Y')}"
    return {"eyebrow": [f"{project}", _fmt_range(start, end), "weekly digest"],
            "headline": headline, "lede": " ".join(lede_parts), "stats": stats, "findings": findings,
            "areas": out_areas, "gaps": gaps,
            "footer": f"Compiled from {n} commits on {m.get('branch', 'main')} of {_html(project)}, with pull request "
                      f"descriptions where GitHub had them.<br />Window {start} → {end} · head "
                      f"{m.get('head_sha', '')[:11]} · every claim traceable to a commit · generated by idxg history digest",
            "window": {"start": start, "end": end, "label": label, "commits": n}}


MAX_AREAS = 12


def _fold_small_areas(ordered):
    """Keep the busiest areas as tiles and fold the one-change modules into one tile per
    layer, with a subhead per module, so a week reads as 2 to 12 areas like the vault's."""
    if len(ordered) <= MAX_AREAS:
        return ordered
    keep, rest = [], []
    for a in sorted(ordered, key=lambda a: -len(a["rows"])):
        (keep if len(keep) < MAX_AREAS - 3 and len(a["rows"]) >= 2 else rest).append(a)
    buckets = {0: ("Other build and tooling changes", "Build & tooling", 1),
               2: ("Other shared and service changes", "Shared components", 3),
               5: ("Other feature changes", "What people use", 6), 6: ("Other feature changes", "What people use", 6),
               8: ("Other changes", "Elsewhere", 8), 9: ("Other tests, docs and upkeep", "Tidying up", 9)}
    folded = collections.OrderedDict()
    for a in sorted(rest, key=lambda a: (a["klass"], a["order"], a["name"])):
        name, layer, klass = buckets.get(a["klass"], ("Other changes", "Elsewhere", 8))
        f = folded.setdefault(name, {"name": name, "layer": layer, "klass": klass,
                                     "order": 0, "rows": [], "ins": 0, "del": 0,
                                     "authors": collections.Counter(), "files": 0, "groups": []})
        f["groups"].append({"at": len(f["rows"]), "label": a["name"]})
        f["rows"].extend(a["rows"])
        f["ins"] += a["ins"]; f["del"] += a["del"]; f["files"] += a["files"]
        f["authors"].update(a["authors"])
    return sorted(keep + list(folded.values()), key=lambda a: (a["klass"], a["order"], -len(a["rows"])))


CHIP_WORDS = {"prod": "live-bug fixes", "build": "build or tooling changes", "train": "slices of a multi-part series",
              "test": "test-only changes"}

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "weekly-digest.html")


def digest_template():
    with open(TEMPLATE_PATH) as f:
        return f.read()


def render_digest(data, title=None):
    """The template with its two placeholders filled, exactly as the vault's digest command
    does it: title and data, nothing else changes."""
    view = {k: v for k, v in data.items() if k not in ("window",)}
    for a in view["areas"]:
        a = a  # commits stay: the renderer ignores unknown keys
    title = title or f"{data['eyebrow'][0]} digest, {data['window']['label']}"
    return (digest_template().replace("{{TITLE}}", _html(title))
            .replace("{{DIGEST_JSON}}", json.dumps(view, ensure_ascii=False).replace("</", "<\\/")))


def digest_text(data):
    lines = [" / ".join(data["eyebrow"]), data["headline"], "", re.sub(r"<[^>]+>", "", data["lede"]), ""]
    lines += [f"  {s['label']}: {s['value']}" + (f" ({s['sub']})" if s.get("sub") else "") for s in data["stats"]]
    if data["findings"]:
        lines += ["", "what stood out"]
        for f in data["findings"]:
            lines += [f"  {f['n']}  {f['area']}: {f['title']}", "      " + re.sub(r"<[^>]+>", "", f["body"])]
    lines += ["", f"the week by area ({sum(len(a['prs']) for a in data['areas'])} changes, {len(data['areas'])} areas)"]
    for a in data["areas"]:
        lines += ["", f"  {a['name']}  [{a['layer']}]  {len(a['prs'])}", "    " + re.sub(r"<[^>]+>", "", a["note"])]
        for p in a["prs"]:
            tag = f" [{p['tag']}]" if p.get("tag") else ""
            lines += [f"    {p['n']}  {p['t']}{tag}", "        " + re.sub(r"<[^>]+>", "", p["w"])]
    if data["gaps"]:
        lines += ["", "what this cannot tell you"] + ["  - " + re.sub(r"<[^>]+>", "", g) for g in data["gaps"]]
    return "\n".join(lines)

# ---------------------------------------------------------------- explorer slice

def slice_for_viz(hdb_path, recent=60, churn_limit=40, weeks_limit=26):
    if not hdb_path or not os.path.exists(hdb_path):
        return None
    db = connect(hdb_path)
    try:
        m = meta(db)
        granularity, era_list = eras(db)
        year_ago = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
        return {
            "meta": m, "overview": overview_text(db), "granularity": granularity,
            "eras": [{k: v for k, v in e.items()} for e in era_list],
            "narrative": narrate(m, granularity, era_list),
            "monthly": monthly(db),
            "churn": [list(r) for r in churn(db, since=year_ago, by="module", limit=churn_limit)],
            "churn_since": year_ago,
            "recent": [list(r) for r in commits_for(db, limit=recent)],
            "recent_narrated": narrated_commits(db, commits_for(db, limit=recent), m.get("remote_web", "")),
            "weeks": [{"week": w, "commits": n, **{"digest": week_digest(db, *week_bounds(w), m.get("remote_web", ""))}}
                      for w, n in weeks(db, limit=weeks_limit)],
            "template": digest_template(),
            "components": [list(r) for r in db.execute(
                """SELECT component, first_date, last_date, commits, alive, module FROM components
                   WHERE commits >= 3 ORDER BY first_date DESC LIMIT 400""")],
            "docs": [list(r) for r in db.execute(
                """SELECT path, title, kind, module, published, bytes FROM docs ORDER BY kind, path LIMIT 1500""")],
        }
    finally:
        db.close()


# ---------------------------------------------------------------- vault export

def _yaml(s):
    return json.dumps(s if s is not None else "")


def _write_clipping(clippings, state, key, base, published, content, today, dry=False):
    """Immutable clippings: a changed source becomes a new, date-suffixed file, and an
    existing file is never rewritten, which is the contract the knowledge vaults rely on."""
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    prev = state.get(key)
    if prev and prev.get("hash") == digest:
        return None
    fname = f"{base}.md" if not prev else f"{base} ({published or today}).md"
    target = os.path.join(clippings, fname)
    n = 2
    while os.path.exists(target):
        target = os.path.join(clippings, f"{base} ({published or today}) {n}.md")
        n += 1
    if not dry:
        with open(target, "w") as f:
            f.write(content)
    state[key] = {"clipping": os.path.basename(target), "hash": digest, "published": published,
                  "synced_at": today}
    return os.path.basename(target), prev.get("clipping") if prev else None


def export_vault(hdb_path, out_dir, docs=True, history=True, modules_min_commits=3, dry=False, log=print):
    db = connect(hdb_path)
    m = meta(db)
    project = m.get("project", "project")
    repo = m.get("remote_web", "")
    branch = m.get("branch", "main")
    today = datetime.date.today().isoformat()
    out_dir = os.path.expanduser(out_dir)
    clippings = os.path.join(out_dir, "Clippings")
    sync_dir = os.path.join(out_dir, ".claude", "sync")
    state_path = os.path.join(sync_dir, "idxg-history-state.json")
    state = {}
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    if not dry:
        os.makedirs(clippings, exist_ok=True)
        os.makedirs(sync_dir, exist_ok=True)
    written, superseded = [], []

    def note(res):
        if res:
            written.append(res[0])
            if res[1]:
                superseded.append((res[0], res[1]))

    if docs:
        for d in db.execute("SELECT * FROM docs ORDER BY path"):
            title = clipping_name(project, d["path"])
            url = f"{repo}/blob/{branch}/{d['path']}" if repo else ""
            fm = ["---", f"title: {_yaml(title)}", "source: repo-doc", f"repo: {_yaml(repo or project)}",
                  f"path: {d['path']}", f"url: {url}", f"author: {_yaml(d['author'])}",
                  f"published: {d['published']}"]
            if d["mechanical_refreshed"]:
                fm.append(f"mechanical_refreshed: {d['mechanical_refreshed']}")
            if d["module"]:
                fm.append(f"module: {d['module']}")
            fm += [f"clipped: {today}", f"hash: {d['hash']}", "---", ""]
            note(_write_clipping(clippings, state, "doc:" + d["path"], title, d["published"],
                                 "\n".join(fm) + d["content"], today, dry))
    if history:
        granularity, era_list = eras(db)
        paras = narrate(m, granularity, era_list)
        head = m.get("head_sha", "")[:11]
        overview = overview_text(db)
        body = [f"# {project} history overview", "", overview, "",
                f"Granularity of the period notes: one per {granularity}. Regenerate with "
                f"`idxg history vault`.", "", "## Periods", ""]
        for p in paras:
            body.append(f"- [[History - {project} - {p['period']}]] ({p['label']})")
        fm = ["---", f"title: {_yaml(f'History - {project} - Overview')}", "source: git-history",
              f"repo: {_yaml(repo or project)}", f"branch: {branch}", f"head: {head}",
              f"published: {m.get('last_day', '')}", f"clipped: {today}", "---", ""]
        note(_write_clipping(clippings, state, "history:overview", f"History - {project} - Overview",
                             m.get("last_day", ""), "\n".join(fm + body) + "\n", today, dry))
        by_period = {e["period"]: e for e in era_list}
        for p in paras:
            e = by_period[p["period"]]
            lines = [f"# {project} history: {p['label']}", "", p["text"], ""]
            if e["biggest"]:
                lines += ["## Largest changes", ""]
                lines += [f"- {b['subject']} ({b['sha']}, {b['files']} files, {b['lines']:,} lines)"
                          for b in e["biggest"]]
                lines.append("")
            rows = commits_for(db, since=e["first"], until=e["last"], limit=60)
            lines += [f"## Commits ({e['commits']:,}; the {min(60, e['commits'])} most recent listed)", ""]
            for r in rows:
                pr = f" [#{r['pr']}]({repo}/pull/{r['pr']})" if r["pr"] and repo else (f" #{r['pr']}" if r["pr"] else "")
                lines.append(f"- {r['day']} {r['short']} {r['subject']}{pr}")
            fm = ["---", f"title: {_yaml(f'History - {project} - {p['period']}')}", "source: git-history",
                  f"repo: {_yaml(repo or project)}", f"branch: {branch}", f"period: {p['period']}",
                  f"range: {e['first']}..{e['last']}", f"published: {e['last']}", f"clipped: {today}",
                  "---", ""]
            # A closed period is stable, so its clipping is written once; only the period
            # that still receives commits changes between runs.
            note(_write_clipping(clippings, state, "history:" + p["period"], f"History - {project} - {p['period']}",
                                 e["last"], "\n".join(fm + lines) + "\n", today, dry))
        mods = db.execute("""SELECT cf.module, COUNT(DISTINCT c.sha) n, MIN(c.day), MAX(c.day),
                                    COUNT(DISTINCT c.author), SUM(cf.ins), SUM(cf.del)
                             FROM commit_files cf JOIN commits c ON c.sha = cf.sha
                             WHERE cf.module IS NOT NULL GROUP BY cf.module HAVING n >= ?
                             ORDER BY n DESC""", (modules_min_commits,)).fetchall()
        for mod, n, first, last, authors, ins, dele in mods:
            prefix = db.execute("SELECT component FROM components WHERE module = ? LIMIT 1", (mod,)).fetchone()
            rows = commits_for(db, module=mod, limit=40)
            lines = [f"# History of module {mod}", "",
                     f"{n:,} commits on {branch} touched {mod} between {first} and {last}, by {authors} "
                     f"authors, adding {ins or 0:,} lines and removing {dele or 0:,}."
                     + (f" Its files live under `{prefix[0]}`." if prefix else ""), "",
                     "Attribution follows the compiled index: a file the build never compiled is not "
                     "attributed to any module, so these counts are a lower bound.", "",
                     f"## Recent commits (latest {len(rows)})", ""]
            for r in rows:
                pr = f" [#{r['pr']}]({repo}/pull/{r['pr']})" if r["pr"] and repo else (f" #{r['pr']}" if r["pr"] else "")
                lines.append(f"- {r['day']} {r['short']} {r['subject']}{pr}")
            docs_rows = db.execute("SELECT path FROM docs WHERE module = ? ORDER BY path", (mod,)).fetchall()
            if docs_rows:
                lines += ["", "## Documentation in the repository", ""]
                lines += [f"- [[{clipping_name(project, d[0])}]]" for d in docs_rows]
            fm = ["---", f"title: {_yaml(f'History - Module - {mod}')}", "source: git-history",
                  f"repo: {_yaml(repo or project)}", f"branch: {branch}", f"module: {mod}",
                  f"published: {last}", f"clipped: {today}", "---", ""]
            note(_write_clipping(clippings, state, "module:" + mod, f"History - Module - {mod}", last,
                                 "\n".join(fm + lines) + "\n", today, dry))
    if not dry:
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    db.close()
    log(f"vault: {out_dir}{'  (dry run, nothing written)' if dry else ''}")
    log(f"  clippings written: {len(written)}  superseded: {len(superseded)}  tracked: {len(state)}")
    return written, superseded
