"""Project registry, store detection and staleness for ios-codebase-indexer."""
import hashlib, json, os, plistlib, subprocess, time

CONFIG_DIR = os.path.expanduser("~/.config/ios-codebase-indexer")
CACHE_DIR = os.path.expanduser("~/.cache/indexstore-graph")
REGISTRY = os.path.join(CONFIG_DIR, "projects.json")
CONFIG = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "auto_refresh_on_query": False,   # rebuild inline when a query hits a stale graph
    "viz_on_build": True,             # regenerate the HTML explorer after every build
    "viz_limit": 1500,                # symbols in the repo-wide explorer slice
    "viz_per_node_cap": 25,           # edges kept per symbol per direction
    "viz_scope": "",                  # module or path glob the explorer defaults to
    "poll_minutes": 15,               # autoindex agent interval (global only)
    "jobs": max(2, (os.cpu_count() or 4) - 2),
    "claude_md_path": "",             # where init writes the agent note (blank = CLAUDE.md)
    "history_on_build": True,         # refresh commit history and docs after every graph build
    "history_branch": "",             # branch whose log is the project's history (blank = main/master)
    "history_first_parent": True,     # one commit per merge on that branch, not every branch commit
    "docs_manifest": "",              # JSON with include/exclude globs for repo docs (blank = all tracked .md)
    "history_vault": "",              # default directory for idxg history vault (blank = beside the db)
    "history_prs": True,              # fetch pull request descriptions through gh when it is logged in
    "viz_history_weeks": 26,          # weekly digests embedded in the explorer
}

GLOBAL_ONLY = ("poll_minutes",)

CONFIG_HELP = {
    "auto_refresh_on_query": "reindex inline when a query finds the graph stale",
    "viz_on_build": "regenerate the HTML explorer after every build",
    "viz_limit": "symbols in the repo-wide explorer slice (0 = all)",
    "viz_per_node_cap": "edges kept per symbol per direction in the explorer",
    "viz_scope": "module name or path glob the explorer defaults to",
    "poll_minutes": "autoindex agent interval, global only",
    "jobs": "parallel extractor workers",
    "claude_md_path": "where init writes the agent note (blank = <project>/CLAUDE.md)",
    "history_on_build": "refresh commit history and repo docs after every graph build",
    "history_branch": "branch whose log is the project's history (blank = main, then master)",
    "history_first_parent": "count one commit per merge on the history branch",
    "docs_manifest": "JSON file with include/exclude globs for repo docs (blank = every tracked .md)",
    "history_vault": "where idxg history vault writes by default (blank = <db dir>/<project>-vault)",
    "history_prs": "fetch pull request descriptions through gh (needs gh auth login)",
    "viz_history_weeks": "how many weekly digests the explorer embeds",
}


def coerce(key, raw):
    """Parse a command-line value against the type of the default."""
    default = DEFAULT_CONFIG.get(key)
    if isinstance(default, bool):
        low = str(raw).strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        raise ValueError(f"{key} expects a boolean, got {raw!r}")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{key} expects an integer, got {raw!r}")
    return str(raw)


def effective_config(root=None, with_source=False):
    """Defaults, overlaid with the global file, overlaid with this project's overrides."""
    merged = {k: (v, "default") for k, v in DEFAULT_CONFIG.items()}
    for k, v in _read_json(CONFIG).items():
        merged[k] = (v, "global")
    if root:
        over = (load_registry().get(os.path.realpath(root), {}).get("config") or {})
        for k, v in over.items():
            if k in GLOBAL_ONLY:
                continue
            merged[k] = (v, "project")
    if with_source:
        return merged
    return {k: v for k, (v, _) in merged.items()}


def set_config(key, value, root=None):
    """Write one key to the project override (default) or the global file."""
    if key not in DEFAULT_CONFIG:
        raise ValueError(f"unknown key {key!r}; known keys: {', '.join(sorted(DEFAULT_CONFIG))}")
    val = coerce(key, value)
    if root and key not in GLOBAL_ONLY:
        reg = load_registry()
        rr = os.path.realpath(root)
        entry = reg.setdefault(rr, {})
        entry.setdefault("config", {})[key] = val
        save_registry(reg)
        return "project", val
    cfg = _read_json(CONFIG)
    cfg[key] = val
    save_config(cfg)
    return "global", val


def unset_config(key, root=None):
    if root:
        reg = load_registry()
        rr = os.path.realpath(root)
        over = reg.get(rr, {}).get("config") or {}
        if key in over:
            del over[key]
            save_registry(reg)
            return "project"
    cfg = _read_json(CONFIG)
    if key in cfg:
        del cfg[key]
        save_config(cfg)
        return "global"
    return None


def _read_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}

MARKERS = (".sourcekit-lsp", "buildServer.json", "Package.swift", ".git", "MODULE.bazel", "WORKSPACE")


def load_config(root=None):
    return effective_config(root)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


def load_registry():
    if os.path.exists(REGISTRY):
        try:
            with open(REGISTRY) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_registry(reg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=2, sort_keys=True)
    os.replace(tmp, REGISTRY)


def slug(root):
    """Stable per-checkout name: directory name plus a short hash of the full path."""
    base = os.path.basename(root.rstrip("/")) or "project"
    h = hashlib.blake2b(root.encode(), digest_size=3).hexdigest()
    return f"{base}-{h}"


def db_for(root):
    """Where this project's graph lives, preferring a legacy <basename>.db if present."""
    legacy = os.path.join(CACHE_DIR, os.path.basename(root.rstrip("/")) + ".db")
    reg = load_registry().get(os.path.realpath(root))
    if reg and reg.get("db"):
        return reg["db"]
    if os.path.exists(legacy):
        return legacy
    return os.path.join(CACHE_DIR, slug(root) + ".db")


def find_root(start=None):
    """Walk up from start looking for a registered project, then for a project marker."""
    cur = os.path.realpath(start or os.getcwd())
    reg = load_registry()
    probe = cur
    while True:
        if probe in reg:
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    probe = cur
    while True:
        if any(os.path.exists(os.path.join(probe, m)) for m in MARKERS):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return cur
        probe = parent


def detect_stores(root):
    """Every index store describing this checkout, plus the bazel index prefix map."""
    found, pm = [], {}
    cfg = os.path.join(root, ".sourcekit-lsp", "config.json")
    if os.path.exists(cfg):
        try:
            with open(cfg) as f:
                data = json.load(f)
            pm = (data.get("index") or {}).get("indexPrefixMap") or {}
        except (OSError, ValueError):
            pm = {}
        bo = pm.get("./bazel-out")
        if bo:
            bsp = os.path.join(bo, "_global_index_store")
            if os.path.isdir(bsp):
                found.append(bsp)
            plain = os.path.join(bo.replace("/sourcekit-bazel-bsp/execroot/", "/execroot/"),
                                 "_global_index_store")
            if plain != bsp and os.path.isdir(plain):
                found.append(plain)
    for cand in (os.path.join(root, ".index-build", "index"),
                 os.path.join(root, ".build", "index-store"),
                 os.path.expanduser("~/.sourcekit-lsp/index-build")):
        if os.path.isdir(cand) and cand not in found:
            found.append(cand)
    if not found:
        bazel_out = os.path.join(root, "bazel-out", "_global_index_store")
        if os.path.isdir(bazel_out):
            found.append(bazel_out)
    for store in derived_data_stores(root):
        if store not in found:
            found.append(store)
    return found, pm


def derived_data_stores(root, derived_data=None):
    """Index stores Xcode wrote for this project.

    xcodebuild and Xcode index while building by default, into
    <DerivedData>/<Project>-<hash>/Index.noindex/DataStore. Each build directory records
    the workspace it belongs to in info.plist, which is what ties a store to this root.
    """
    base = derived_data or os.path.expanduser("~/Library/Developer/Xcode/DerivedData")
    if not os.path.isdir(base):
        return []
    root = os.path.realpath(root).rstrip("/")
    out = []
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        store = os.path.join(d, "Index.noindex", "DataStore")
        if not os.path.isdir(store):
            store = os.path.join(d, "Index", "DataStore")   # Xcode 12 and earlier
            if not os.path.isdir(store):
                continue
        workspace = None
        info = os.path.join(d, "info.plist")
        if os.path.exists(info):
            try:
                with open(info, "rb") as f:
                    workspace = (plistlib.load(f) or {}).get("WorkspacePath")
            except (OSError, ValueError, plistlib.InvalidFileException):
                workspace = None
        if not workspace:
            continue
        ws = os.path.realpath(workspace)
        if ws == root or ws.startswith(root + "/"):
            out.append(store)
    return out


def store_signature(stores):
    """Cheap fingerprint of every store: unit count and newest unit directory mtime."""
    sig = {}
    for s in stores:
        units = os.path.join(s, "v5", "units")
        if not os.path.isdir(units):
            units = s
        try:
            n = sum(1 for _ in os.scandir(units))
            mtime = os.stat(units).st_mtime
        except OSError:
            n, mtime = 0, 0
        sig[s] = {"units": n, "mtime": round(mtime, 3)}
    return sig


def staleness(db_meta, stores=None):
    """Compare the signature recorded at build time with the stores as they are now.

    Returns (stale, reason, detail).
    """
    stores = stores or json.loads(db_meta.get("stores") or "[]")
    if not stores:
        return False, "no stores detected", {}
    recorded = json.loads(db_meta.get("store_signature") or "{}")
    current = store_signature(stores)
    added = {}
    for s, cur in current.items():
        old = recorded.get(s)
        if not old:
            added[s] = {"units": cur["units"], "was": None}
        elif cur["units"] != old["units"] or cur["mtime"] > old["mtime"] + 1:
            added[s] = {"units": cur["units"], "was": old["units"]}
    if added:
        delta = sum((v["units"] or 0) - (v["was"] or 0) for v in added.values())
        return True, f"{len(added)} store(s) changed, {delta:+d} units", added
    return False, "up to date", {}


def register(root, db, stores, extra=None):
    reg = load_registry()
    entry = reg.get(root, {})
    entry.update({"db": db, "stores": stores, "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if extra:
        entry.update(extra)
    reg[root] = entry
    save_registry(reg)
    return entry


def git_root(path):
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None
