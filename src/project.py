"""Project registry, store detection and staleness for ios-codebase-indexer."""
import hashlib, json, os, subprocess, time

CONFIG_DIR = os.path.expanduser("~/.config/ios-codebase-indexer")
CACHE_DIR = os.path.expanduser("~/.cache/indexstore-graph")
REGISTRY = os.path.join(CONFIG_DIR, "projects.json")
CONFIG = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "auto_refresh_on_query": False,   # rebuild inline when a query hits a stale graph
    "viz_on_build": True,             # regenerate the HTML explorer after every build
    "poll_minutes": 15,               # autoindex daemon interval
    "jobs": max(2, (os.cpu_count() or 4) - 2),
}

MARKERS = (".sourcekit-lsp", "buildServer.json", "Package.swift", ".git", "MODULE.bazel", "WORKSPACE")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG) as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    return cfg


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
    return found, pm


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
