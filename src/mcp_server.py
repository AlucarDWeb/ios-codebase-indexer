#!/usr/bin/env python3
"""MCP stdio server over an index-store graph: codebase-memory tool shapes, sourcekit data."""
import argparse, io, json, os, sys, traceback
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import idxg

SERVER = {"name": "indexstore-graph", "version": "1.0.0"}

TOOLS = [
    {"name": "index_status",
     "description": "Index-store graph status: counts, edge kinds, build time, and how much of the "
                    "repo the compiled index actually covers. Call this first in a session.",
     "inputSchema": {"type": "object", "properties": {"db": {"type": "string"}}}},
    {"name": "search_graph",
     "description": "Find symbols by full-text query (BM25 over camel-split names), name regex, kind, "
                    "module, file glob, or degree. Results carry exact definition file:line and "
                    "in/out degrees from the compiler index, not a parser heuristic.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "natural-language / keyword full-text search"},
         "name_pattern": {"type": "string", "description": "regex on the symbol name"},
         "kind": {"type": "string", "description": "comma list: Struct,Class,Protocol,InstanceMethod,..."},
         "module": {"type": "string"}, "file_pattern": {"type": "string", "description": "glob on repo path"},
         "lang": {"type": "string", "enum": ["Swift", "ObjC", "C", "C++"]},
         "min_degree": {"type": "integer"}, "max_degree": {"type": "integer"},
         "limit": {"type": "integer", "default": 40}, "offset": {"type": "integer", "default": 0},
         "include_external": {"type": "boolean", "description": "include symbols defined outside the repo"},
         "db": {"type": "string"}}}},
    {"name": "trace_path",
     "description": "Walk resolved call/reference edges from a symbol. direction in=callers, out=callees, "
                    "both. Every edge carries the exact source location of the call site.",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string", "description": "name, Module.Name, or USR"},
         "direction": {"type": "string", "enum": ["in", "out", "both"], "default": "both"},
         "depth": {"type": "integer", "default": 2}, "fanout": {"type": "integer", "default": 25},
         "edge_kinds": {"type": "string", "default": "CALLS",
                        "description": "CALLS,REFERENCES,CONTAINS,INHERITS,OVERRIDES,EXTENDS,ACCESSOR_OF"},
         "first": {"type": "boolean", "default": True, "description": "take best match instead of listing"},
         "max_rows": {"type": "integer", "default": 120,
                      "description": "cap printed rows; a wide trace is truncated with a note"},
         "max_bytes": {"type": "integer", "default": 8000,
                       "description": "cap the payload size of one call"},
         "db": {"type": "string"}},
         "required": ["symbol"]}},
    {"name": "find_references",
     "description": "Every recorded occurrence of a symbol with its role (definition, reference, read, "
                    "write, call, dynamic), grouped by file.",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "limit": {"type": "integer", "default": 200},
         "db": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_code_snippet",
     "description": "Print a symbol's definition from disk, using the index's definition line and a "
                    "brace-balanced extent.",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "max_lines": {"type": "integer", "default": 200},
         "max_bytes": {"type": "integer", "default": 6000},
         "db": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "query_graph",
     "description": "Read-only SQL over the graph. Tables: symbols(usr_hash,usr,name,kind,lang,module,"
                    "def_path_hash,def_line,in_deg,out_deg,call_in,call_out,ref_count,in_repo), "
                    "edges(src,dst,kind,path_hash,line,col), occurrences(usr_hash,path_hash,line,col,roles), "
                    "defs, files(path_hash,path,rel,in_repo,module), units. Call get_schema for details.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer", "default": 200},
         "db": {"type": "string"}}, "required": ["query"]}},
    {"name": "check_index_coverage",
     "description": "For each path (file or directory), report whether the compiled index covers it. "
                    "A file with no records was never compiled in the indexed build: grep it instead. "
                    "Absence is never proof a symbol does not exist.",
     "inputSchema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"}}, "db": {"type": "string"}},
         "required": ["paths"]}},
    {"name": "get_architecture",
     "description": "Layers, modules by symbol count, cross-module call hotspots, and build targets.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "default": 20}, "db": {"type": "string"}}}},
    {"name": "find_dead_code",
     "description": "Symbols nothing in the indexed build reaches: no call, no reference, no "
                    "override, no occurrence beyond their own definition. Structural edges are "
                    "ignored, and synthesis-driven members, protocol witnesses, IB outlets, entry "
                    "points, vendored trees and ObjC are excluded by default. Pass verify to "
                    "cross-check each candidate with a text search. Bounded by index coverage: "
                    "report survivors as candidates to check, never as unused code.",
     "inputSchema": {"type": "object", "properties": {
         "module": {"type": "string"}, "kind": {"type": "string", "description": "comma list"},
         "verify": {"type": "boolean", "description": "drop candidates named in another file"},
         "include_tests": {"type": "boolean"}, "include_vendor": {"type": "boolean"},
         "lang": {"type": "string", "enum": ["Swift", "ObjC", "C", "any"]},
         "limit": {"type": "integer", "default": 100}, "offset": {"type": "integer", "default": 0},
         "db": {"type": "string"}}}},
    {"name": "refresh_index",
     "description": "Reindex the project when the compiler's index store has moved on, which it "
                    "does after any build. Takes minutes on a large repo, so call it when a trace "
                    "looks stale rather than routinely; index_status reports staleness for free.",
     "inputSchema": {"type": "object", "properties": {
         "force": {"type": "boolean", "description": "reindex even when nothing changed"},
         "db": {"type": "string"}}}},
    {"name": "list_projects",
     "description": "Every indexed project on this machine, with symbol counts and whether each "
                    "graph is fresh or behind its index store.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_schema",
     "description": "Full db schema plus the edge-kind and occurrence-role vocabulary.",
     "inputSchema": {"type": "object", "properties": {"db": {"type": "string"}}}},
    {"name": "build_visualizer",
     "description": "Generate the self-contained HTML graph explorer and return its path.",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string", "description": "module name or path glob"},
         "out": {"type": "string"}, "limit": {"type": "integer"},
         "db": {"type": "string"}}}},
]

DEFAULTS = {"json": False, "db": None, "exact": False, "no_stale_check": True}


def ns(**kw):
    d = dict(DEFAULTS)
    d.update(kw)
    return argparse.Namespace(**d)


def run(fn, args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(args)
    return buf.getvalue() or "(no output)"


def call(name, a):
    db = a.get("db")
    if name == "index_status":
        return run(idxg.cmd_status, ns(db=db))
    if name == "search_graph":
        return run(idxg.cmd_search, ns(db=db, query=a.get("query"), name=a.get("name_pattern"),
                                       kind=a.get("kind"), module=a.get("module"),
                                       file=a.get("file_pattern"), lang=a.get("lang"),
                                       min_degree=a.get("min_degree") or 0, max_degree=a.get("max_degree"),
                                       limit=a.get("limit", 40), offset=a.get("offset", 0),
                                       all=bool(a.get("include_external")), usr=True, detail="default"))
    if name == "trace_path":
        return run(idxg.cmd_trace, ns(db=db, symbol=a["symbol"], direction=a.get("direction", "both"),
                                      depth=a.get("depth", 2), fanout=a.get("fanout", 25),
                                      kind=a.get("edge_kinds", "CALLS"), first=a.get("first", True),
                                      max_rows=a.get("max_rows", 120),
                                      max_bytes=a.get("max_bytes", 8000)))
    if name == "find_references":
        return run(idxg.cmd_refs, ns(db=db, symbol=a["symbol"], limit=a.get("limit", 200)))
    if name == "get_code_snippet":
        return run(idxg.cmd_snippet, ns(db=db, symbol=a["symbol"], max_lines=a.get("max_lines", 200),
                                        max_bytes=a.get("max_bytes", 6000)))
    if name == "query_graph":
        return run(idxg.cmd_sql, ns(db=db, query=a["query"], limit=a.get("limit", 200)))
    if name == "check_index_coverage":
        return run(idxg.cmd_coverage, ns(db=db, paths=a["paths"]))
    if name == "get_architecture":
        return run(idxg.cmd_arch, ns(db=db, limit=a.get("limit", 20)))
    if name == "find_dead_code":
        return run(idxg.cmd_dead, ns(db=db, module=a.get("module"), kind=a.get("kind"),
                                     verify=bool(a.get("verify")),
                                     include_tests=bool(a.get("include_tests")),
                                     include_vendor=bool(a.get("include_vendor")),
                                     lang=a.get("lang", "Swift"), limit=a.get("limit", 100),
                                     offset=a.get("offset", 0)))
    if name == "refresh_index":
        return run(idxg.cmd_refresh, ns(db=db, all=False, force=bool(a.get("force")),
                                        jobs=None, verbose=False))
    if name == "list_projects":
        return run(idxg.cmd_projects, ns(db=None))
    if name == "get_schema":
        return run(idxg.cmd_schema, ns(db=db))
    if name == "build_visualizer":
        return run(idxg.cmd_viz, ns(db=db, scope=a.get("scope"), out=a.get("out"), limit=a.get("limit"),
                                    edge_cap=120000, per_node_cap=14, title=None, open=False))
    raise ValueError(f"unknown tool {name}")


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        try:
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}}, "serverInfo": SERVER}})
            elif method in ("notifications/initialized", "initialized"):
                continue
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                text = call(params["name"], params.get("arguments") or {})
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"content": [{"type": "text", "text": text}]}})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif mid is not None:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": f"method not found: {method}"}})
        except SystemExit as e:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
        except Exception:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": traceback.format_exc(limit=3)}], "isError": True}})


if __name__ == "__main__":
    main()
