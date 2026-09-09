#!/usr/bin/env python3
"""codebase-brain MCP stdio server: the code graph, its history and its docs as tools."""
import argparse, io, json, os, sys, traceback
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import idxg

SERVER = {"name": "codebase-brain", "version": idxg.VERSION}

TOOLS = [
    {"name": "index_status",
     "description": "Index-store graph status: counts, edge kinds, build time, how much of the repo "
                    "the compiled index actually covers, the history db, and whether a newer "
                    "codebase-brain release exists. Call this first in a session.",
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
    {"name": "get_history",
     "description": "Commits on the project's main branch that touched a path, a symbol's file, a "
                    "module, or matched an author or subject text. Each row: date, short sha, author, "
                    "subject, PR number, tickets, files and line counts. Attribution to modules follows "
                    "the compiled index, so uncompiled files have none. Pass with_files to list the "
                    "paths each commit changed.",
     "inputSchema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"},
                   "description": "repo-relative files, directories (trailing /) or globs"},
         "symbol": {"type": "string", "description": "history of the file defining this symbol"},
         "module": {"type": "string"}, "component": {"type": "string", "description": "module-depth directory"},
         "author": {"type": "string"}, "since": {"type": "string", "description": "YYYY-MM-DD"},
         "until": {"type": "string"}, "query": {"type": "string", "description": "substring of subject, body or ticket"},
         "with_files": {"type": "boolean"},
         "narrate": {"type": "boolean", "description": "one plain paragraph per commit: who, what, why "
                                                        "(from the PR description), which files and modules"},
         "limit": {"type": "integer", "default": 30},
         "max_bytes": {"type": "integer", "default": 12000, "description": "cap the payload of one call"},
         "db": {"type": "string"}}}},
    {"name": "get_commit",
     "description": "One commit in full: message body, PR, tickets, and every file it changed with "
                    "line counts and module attribution.",
     "inputSchema": {"type": "object", "properties": {
         "sha": {"type": "string", "description": "full or short sha, or a PR number as #123"},
         "max_files": {"type": "integer", "default": 80},
         "max_body": {"type": "integer", "default": 4000, "description": "cap the PR description"},
         "db": {"type": "string"}},
         "required": ["sha"]}},
    {"name": "get_digest",
     "description": "Weekly digest of the main branch: headline and stats for the window, the changes "
                    "that stood out, then every change narrated in plain language and grouped by area "
                    "(tooling first, then modules ordered by how much the rest of the code depends on "
                    "them, features, tests last). Defaults to the week of the last commit. Pass a "
                    "since/until pair for any window.",
     "inputSchema": {"type": "object", "properties": {
         "week": {"type": "string", "description": "ISO week, e.g. 2026-W36"},
         "since": {"type": "string"}, "until": {"type": "string"},
         "max_bytes": {"type": "integer", "default": 16000}, "db": {"type": "string"}}}},
    {"name": "get_churn",
     "description": "Where change concentrates: commits, lines and authors per module, component "
                    "directory, file or author over a window (default the last 365 days).",
     "inputSchema": {"type": "object", "properties": {
         "since": {"type": "string", "description": "YYYY-MM-DD"},
         "by": {"type": "string", "enum": ["module", "component", "file", "author"], "default": "module"},
         "ext": {"type": "string", "description": "restrict to one extension, e.g. swift"},
         "limit": {"type": "integer", "default": 25}, "db": {"type": "string"}}}},
    {"name": "get_timeline",
     "description": "Narrative history of the project: an overview paragraph, then one paragraph per "
                    "period (year, quarter or month by span) with commit and author counts, most "
                    "touched modules, distinctive subject words, directories that appeared or "
                    "disappeared, and the largest change. Every sentence is computed from git log.",
     "inputSchema": {"type": "object", "properties": {
         "periods": {"type": "integer", "default": 6, "description": "most recent periods to narrate; 0 = all"},
         "granularity": {"type": "string", "enum": ["year", "quarter", "month"]},
         "db": {"type": "string"}}}},
    {"name": "list_docs",
     "description": "Markdown documentation tracked in the repository (READMEs, CLAUDE.md notes, "
                    "skills, design docs), each with kind, module attribution and last-commit date. "
                    "Filter by module to find a module's own docs without knowing their paths.",
     "inputSchema": {"type": "object", "properties": {
         "module": {"type": "string"}, "kind": {"type": "string",
                    "description": "readme, agent-note, skill, guide or doc"},
         "path_glob": {"type": "string"}, "limit": {"type": "integer", "default": 60},
         "db": {"type": "string"}}}},
    {"name": "search_docs",
     "description": "Full-text search (BM25) over the repository's markdown docs, with a snippet "
                    "per hit. Use it before reading a doc file, and for 'how does this project do X' "
                    "questions the code graph cannot answer.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer", "default": 10},
         "db": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_doc",
     "description": "The content of one repository doc by path (or a unique path suffix), with its "
                    "last-commit date. Truncated at max_bytes with a note.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "max_bytes": {"type": "integer", "default": 12000},
         "db": {"type": "string"}}, "required": ["path"]}},
    {"name": "refresh_history",
     "description": "Pull new commits from the history branch and re-sync repo docs. Incremental and "
                    "cheap after the first run; index_status shows when it last ran.",
     "inputSchema": {"type": "object", "properties": {
         "full": {"type": "boolean", "description": "rebuild from the first commit"},
         "db": {"type": "string"}}}},
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
    if name == "get_history":
        return run(idxg.cmd_history_log, ns(db=db, paths=a.get("paths") or [], symbol=a.get("symbol"),
                                            module=a.get("module"), component=a.get("component"),
                                            author=a.get("author"), since=a.get("since"),
                                            until=a.get("until"), grep=a.get("query"),
                                            files=bool(a.get("with_files")), narrate=bool(a.get("narrate")),
                                            limit=a.get("limit", 30), max_bytes=a.get("max_bytes", 12000)))
    if name == "get_digest":
        return run(idxg.cmd_history_digest, ns(db=db, week=a.get("week"), since=a.get("since"),
                                               until=a.get("until"), list=False, limit=30, html=None,
                                               open=False, max_bytes=a.get("max_bytes", 16000)))
    if name == "get_commit":
        return run(idxg.cmd_history_show, ns(db=db, sha=a["sha"], max_files=a.get("max_files", 80),
                                             max_body=a.get("max_body", 4000)))
    if name == "get_churn":
        return run(idxg.cmd_history_churn, ns(db=db, since=a.get("since"), by=a.get("by", "module"),
                                              ext=a.get("ext"), limit=a.get("limit", 25)))
    if name == "get_timeline":
        return run(idxg.cmd_history_timeline, ns(db=db, periods=a.get("periods", 6),
                                                 granularity=a.get("granularity")))
    if name == "list_docs":
        return run(idxg.cmd_docs_list, ns(db=db, module=a.get("module"), kind=a.get("kind"),
                                          path=a.get("path_glob"), limit=a.get("limit", 60)))
    if name == "search_docs":
        return run(idxg.cmd_docs_search, ns(db=db, query=a["query"], limit=a.get("limit", 10)))
    if name == "get_doc":
        return run(idxg.cmd_docs_show, ns(db=db, path=a["path"], max_bytes=a.get("max_bytes", 12000)))
    if name == "refresh_history":
        return run(idxg.cmd_history_build, ns(db=db, full=bool(a.get("full")), branch=None, since=None,
                                              docs=True, prs=True, all_commits=False))
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
