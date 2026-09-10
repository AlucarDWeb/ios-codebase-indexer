"""Crash triage: map the frames of a stack trace onto the graph and the history.

A frame is resolved to a symbol by file and line when the trace carries them, else by
name. For each in-repo frame the output is the definition, its callers, and the commits
that touched its file since a date or a git ref. Nothing here knows why the crash
happened; it gathers what an engineer collects by hand before deciding.
"""
import os, re, subprocess

# Apple crash report: "3   Wallapop   0x000104a1b2c4 Type.method(a:) + 1234 (File.swift:210)"
APPLE_RE = re.compile(r"^\s*(\d+)\s+(\S+)\s+0x[0-9a-fA-F]+\s+(.*?)(?:\s\+\s\d+)?(?:\s\(([^()]+?\.\w+):(\d+)\))?\s*$")
# lldb / Xcode: "#3 0x000104a1b2c4 in Type.method(a:) at /path/File.swift:210"
LLDB_RE = re.compile(r"^\s*#(\d+)\s+0x[0-9a-fA-F]+\s+in\s+(.*?)(?:\s+at\s+(\S+?):(\d+))?\s*$")
# lldb thread backtrace: "  * frame #1: 0x000104a1c000 Wallapop`Type.method(a:) at File.swift:80"
FRAME_RE = re.compile(r"^\s*\*?\s*frame\s+#(\d+):\s+0x[0-9a-fA-F]+\s+(\S+?)`(.*?)(?:\s+at\s+(\S+?):(\d+))?\s*$")
# Sentry's text export: "    at Type.method(a:) (File.swift:80)" or "(<unknown>)"
SENTRY_RE = re.compile(r"^\s*at\s+(.+?)\s+\(([^()]*)\)\s*$")
FILE_LINE_RE = re.compile(r"([\w./+-]+\.(?:swift|m|mm|h|c|cpp|cc))\D{0,12}?(\d+)")
OBJC_RE = re.compile(r"[-+]\[(\w+)(?:\s*\((\w+)\))?\s+([\w:]+)\]")
SWIFT_RE = re.compile(r"((?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*(?:\([^()]*\))?|[A-Za-z_]\w*\([^()]*\))")
WRAPPERS = ("closure #", "specialized ", "partial apply for ", "@objc ", "thunk for ", "merged ",
            "protocol witness for ", "generic specialization", "reabstraction thunk", "implicit closure #",
            "key path getter for ", "key path setter for ", "variable initialization expression of ",
            "default argument ", "lazy protocol witness table accessor for ", "outlined ")
# Symbols that belong to the OS even when the trace names no image.
SYSTEM_SYMBOL_PREFIXES = ("start", "_dispatch", "__CF", "_CF", "CF", "_os_", "__os_", "os_", "objc_", "GSEvent",
                          "mach_", "__ulock", "_dlock", "voucher_", "firehose_", "nw_", "_pthread", "pthread_",
                          "UIApplicationMain", "__alm_", "_swift_", "swift_", "$s", "_sigtramp", "__psynch",
                          "-[UI", "+[UI", "-[NS", "+[NS", "-[_UI", "-[CA", "-[NW", "-[_NS", "___", "block_destroy",
                          "_Block", "NSData.", "write", "rename", "closure in writeToFileAux", "thunk for closure")
SYSTEM_IMAGES = ("libswift", "libsystem", "libdispatch", "libobjc", "dyld", "UIKitCore", "UIKit", "Foundation",
                 "CoreFoundation", "CoreGraphics", "GraphicsServices", "QuartzCore", "SwiftUI", "AttributeGraph",
                 "libc++", "CFNetwork", "Combine", "libxpc", "FrontBoardServices", "libclosured")
CALLABLE_KINDS = ("InstanceMethod", "ClassMethod", "StaticMethod", "Function", "Constructor", "Destructor",
                  "InstanceProperty", "StaticProperty", "ClassProperty")


def demangle(text):
    """Run `swift demangle` over the whole trace when it carries mangled Swift names."""
    if "$s" not in text and "_$s" not in text and "_T0" not in text:
        return text
    try:
        out = subprocess.run(["swift", "demangle", "--compact"], input=text, capture_output=True,
                             text=True, timeout=20)
        return out.stdout if out.returncode == 0 and out.stdout else text
    except (OSError, subprocess.TimeoutExpired):
        return text


def _strip_wrappers(sym):
    s = sym.strip()
    changed = True
    while changed:
        changed = False
        for w in WRAPPERS:
            if s.startswith(w):
                # "closure #1 in Type.method(...)" keeps what follows "in "
                rest = s.split(" in ", 1)[1] if " in " in s and w.startswith(("closure", "implicit closure", "variable", "default", "key path", "protocol witness")) else s[len(w):]
                if w == "protocol witness for " and " in conformance " in rest:
                    rest = rest.split(" in conformance ", 1)[1]
                s = rest.strip()
                changed = True
                break
        if s.startswith("<") and ">" in s:
            s = s[s.index(">") + 1:].strip()
            changed = True
    return s


def parse(text):
    """Frames as dicts: index, image, symbol, file, line, raw. Unparseable lines are skipped."""
    frames = []
    for raw in demangle(text).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = APPLE_RE.match(line) or None
        image, sym, file, ln, idx = None, None, None, None, None
        structured = True
        if m:
            idx, image, sym, file, ln = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        else:
            m = LLDB_RE.match(line) or FRAME_RE.match(line) or SENTRY_RE.match(line)
            if m and m.re is SENTRY_RE:
                sym, loc = m.group(1), m.group(2)
                fl = FILE_LINE_RE.search(loc)
                if fl:
                    file, ln = fl.group(1), fl.group(2)
            elif m and m.re is FRAME_RE:
                idx, image, sym, file, ln = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            elif m:
                idx, sym, file, ln = m.group(1), m.group(2), m.group(3), m.group(4)
            else:
                # A bare line counts as a frame only when it names a file and line or a call;
                # thread headers and queue names would otherwise resolve to random symbols.
                structured = False
                fl = FILE_LINE_RE.search(line)
                if fl:
                    file, ln = fl.group(1), fl.group(2)
                elif not OBJC_RE.search(line) and "(" not in line:
                    continue
                sym = line
        if not file:
            fl = FILE_LINE_RE.search(sym or "")
            if fl:
                file, ln = fl.group(1), fl.group(2)
        sym = _strip_wrappers(re.sub(r"\((?:[^()]*\.\w+):\d+\)", "", sym or "")).strip()
        objc = OBJC_RE.search(sym)
        candidates = []
        if objc:
            candidates.append({"type": objc.group(1), "name": objc.group(3)})
        else:
            for tok in sorted(SWIFT_RE.findall(sym), key=len, reverse=True):
                base = tok.split("(")[0]
                parts = base.split(".")
                name = parts[-1] + (tok[tok.index("("):] if "(" in tok else "")
                candidates.append({"type": parts[-2] if len(parts) >= 2 else None, "name": name,
                                   "module": parts[0] if len(parts) >= 3 else None})
                break
        if not candidates and not file and not structured:
            continue
        frames.append({"index": int(idx) if idx else len(frames), "image": image, "symbol": sym,
                       "file": os.path.basename(file) if file else None, "path_hint": file,
                       "line": int(ln) if ln else None, "candidates": candidates, "raw": raw.strip()})
    return frames


def _by_file_line(db, basename, line):
    rows = db.execute("""SELECT s.usr_hash, s.name, s.kind, s.module, s.def_line, f.rel FROM symbols s
                         JOIN files f ON f.path_hash = s.def_path_hash
                         WHERE f.in_repo = 1 AND (f.rel = ? OR f.rel GLOB ?) AND s.name != ''
                         ORDER BY s.def_line""", (basename, "*/" + basename)).fetchall()
    if not rows:
        return None, []
    below = [r for r in rows if r["def_line"] and r["def_line"] <= line]
    callables = [r for r in below if r["kind"] in CALLABLE_KINDS and not r["name"].startswith(("getter:", "setter:", "init:"))]
    pick = (callables or below or rows)[-1] if (callables or below) else rows[0]
    return pick, rows


def _parents(db, uh):
    return [r["name"] for r in db.execute("""SELECT p.name FROM edges e JOIN symbols p ON p.usr_hash = e.src
                                             WHERE e.dst = ? AND e.kind = 'CONTAINS'""", (uh,))]


def _by_name(db, cand):
    name = cand["name"]
    q = """SELECT s.usr_hash, s.name, s.kind, s.module, s.def_line, f.rel FROM symbols s
           LEFT JOIN files f ON f.path_hash = s.def_path_hash WHERE s.name = ? AND s.in_repo = 1
           ORDER BY (s.in_deg + s.out_deg) DESC LIMIT 40"""
    rows = db.execute(q, (name,)).fetchall()
    if not rows and "(" in name:
        rows = db.execute(q.replace("s.name = ?", "s.name GLOB ?"), (name.split("(")[0] + "(*",)).fetchall()
    if not rows:
        return None
    if cand.get("type"):
        typed = [r for r in rows if cand["type"] in _parents(db, r["usr_hash"])]
        if typed:
            return typed[0]
        # The trace names a type the graph does not have: guessing another type's method
        # of the same name would point the reader at the wrong code.
        if not db.execute("SELECT 1 FROM symbols WHERE name = ? AND in_repo = 1 LIMIT 1", (cand["type"],)).fetchone():
            return None
    if cand.get("module"):
        mod = [r for r in rows if r["module"] == cand["module"]]
        if mod:
            return mod[0]
    return rows[0]


def resolve_frame(db, frame):
    if frame["file"] and frame["line"]:
        sym, _ = _by_file_line(db, frame["file"], frame["line"])
        if sym:
            return sym, "file:line"
    for cand in frame["candidates"]:
        sym = _by_name(db, cand)
        if sym:
            return sym, "name"
    return None, None


def callers(db, uh, limit=5):
    """Callers with one call site each, product code before tests: in a crash the test
    callers are noise, and they are usually the most numerous."""
    rows = db.execute("""SELECT c.name, c.kind, c.module, COUNT(*) n, MIN(f.rel) rel, MIN(e.line) line
                         FROM edges e JOIN symbols c ON c.usr_hash = e.src
                         LEFT JOIN files f ON f.path_hash = e.path_hash
                         WHERE e.dst = ? AND e.kind = 'CALLS' GROUP BY e.src ORDER BY n DESC LIMIT 200""",
                      (uh,)).fetchall()
    def is_test(r):
        hay = f"{r['module'] or ''} {r['rel'] or ''}".lower()
        return any(k in hay for k in ("test", "spec", "snapshot", "mock", "fixture"))
    return sorted(rows, key=lambda r: (is_test(r), -r["n"]))[:limit]


def since_date(root, since):
    """A YYYY-MM-DD as given, or the commit date of a git ref (a release tag, say)."""
    if not since:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", since):
        return since
    out = subprocess.run(["git", "-C", root, "log", "-1", "--format=%cs", since + "^{commit}"],
                         capture_output=True, text=True)
    return out.stdout.strip() or None


def is_system(frame):
    img = frame.get("image") or ""
    if any(img.startswith(s) for s in SYSTEM_IMAGES):
        return True
    if img or frame.get("file"):
        return False
    sym = frame.get("symbol") or ""
    return any(sym.startswith(s) for s in SYSTEM_SYMBOL_PREFIXES)
