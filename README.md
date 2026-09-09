# ios-codebase-indexer

A queryable code graph for Swift and Objective-C projects, built from the index store
the compiler already writes.

Editors get "jump to definition" from that index store. This turns the same data into a
SQLite graph with a CLI, an MCP server for coding agents, and a self-contained HTML
explorer. Because the edges come from the compiler, calls, references, overrides and
protocol conformances are the ones that were actually resolved, each with the exact
`file:line` of the call site. No parsing, no guessing about generics or dynamic dispatch.

## Install

```bash
git clone https://github.com/AlucarDWeb/ios-codebase-indexer.git
cd ios-codebase-indexer
./install.sh
```

That links `idxg` and `idxg-build` into `~/.local/bin`, links the Claude Code skill into
`~/.claude/skills/`, and registers the MCP server if the `claude` CLI is present.
Requirements: macOS with Xcode installed (for `libIndexStore.dylib`) and Python 3.9+.
No third-party packages.

## First run, step by step

**1. Check the CLI is on your PATH.**

```bash
idxg --help
```

If that fails, add `~/.local/bin` to your PATH, or re-run `./install.sh` and read its
last line.

**2. Make sure the project has an index store.** This is the one prerequisite, and
nothing works without it. The compiler writes the store during a build or during
sourcekit-lsp's background indexing:

- SwiftPM or Xcode project: open it in an editor with sourcekit-lsp background indexing
  on, or just build it once.
- Bazel: build with `--features=swift.index_while_building`, or set up
  [sourcekit-bazel-bsp](https://github.com/spotify/sourcekit-bazel-bsp).

**3. Index the project.**

```bash
cd /path/to/your/project
idxg init
```

It prints the stores it found, builds the graph, renders the explorer, and installs the
project skill and CLAUDE.md note. Expect seconds on a small package, two to three minutes
on a large monorepo. If it reports no index store, go back to step 2.

**4. See what you got, and what it does not cover.**

```bash
idxg status
```

The coverage line is the one to read. It counts tracked sources that have index records.
A low number is not a bug: it means much of the project was never compiled in the build
that produced the store. Build more targets and re-run `idxg refresh` to raise it.

**5. Try a query.**

```bash
idxg search "<something you know exists>"
idxg trace <ASymbolFromThatSearch> --direction in --first
```

The second one answers "who calls this", with the exact call sites.

**6. Look at it.**

```bash
idxg open
```

Three tabs: overview, a cross-module call graph where clicking a module isolates its
calls, and a symbol browser with callers and callees.

**7. Keep it fresh.**

```bash
idxg autoindex --install --every 20
```

A launchd agent then reindexes any registered project whose store has changed. Without
it, run `idxg refresh` after builds; queries warn on stderr when the graph has fallen
behind.

**8. Restart your agent.** The MCP server was registered at install time, but an already
running Claude Code session will not see it until you restart. The project skill and the
CLAUDE.md note are picked up per project with no restart needed.

### If something looks wrong

| Symptom | Cause and fix |
|---|---|
| `no index store found` | Nothing has compiled this project yet. See step 2. |
| `no graph for <path>` | Indexed elsewhere or not yet. Run `idxg init`, or `idxg projects` to see what is registered. |
| Coverage far below 100% | Only compiled targets have records. Build the missing targets, then `idxg refresh`. |
| A symbol resolves to several candidates | Pass `--first`, or give `Module.Name` or the USR. |
| Line numbers are off | The graph predates your edits. `idxg refresh`. |
| `.m` files missing | ObjC needs a build-system change. See [docs/objc-index-store.md](docs/objc-index-store.md). |

## Command reference

```bash
cd /path/to/your/project
idxg-build --jobs 8        # index store -> ~/.cache/indexstore-graph/<project>.db
idxg status                # counts, edge kinds, coverage

idxg search "location chip mapper"        # full text over camel-split names
idxg search --name '^SearchRoot' --kind Struct,Class
idxg trace MyReducer --direction in --depth 2 --first     # who calls it, with call sites
idxg trace MyView --kind CALLS,REFERENCES --direction out --first
idxg refs MyType                          # every occurrence, with roles
idxg snippet MyType                       # definition, read from disk
idxg arch                                 # layers, modules, hotspots, build targets
idxg coverage Sources/Feature             # what the compiled index actually covers
idxg sql "SELECT kind, COUNT(*) n FROM symbols WHERE in_repo=1 GROUP BY kind"
idxg viz --scope MyModule --open          # HTML explorer
idxg schema                               # tables, edge kinds, role bits
```

Every query command takes `--json`, and `--db <path>` to point at another project's
graph. Symbols resolve by bare name, `Module.Name`, or USR; an ambiguous name lists
candidates unless you pass `--first`.

## Where the index store comes from

`idxg-build` auto-detects, and merges everything it finds:

| Source | Path |
|---|---|
| sourcekit-lsp background indexing (SwiftPM, plain projects) | `.index-build/index`, `~/.sourcekit-lsp/index-build` |
| sourcekit-bazel-bsp | `<output_base>/sourcekit-bazel-bsp/execroot/_main/bazel-out/_global_index_store` |
| bazel with `--features=swift.index_while_building` | `<output_base>/execroot/_main/bazel-out/_global_index_store` |

Bazel paths are read from `.sourcekit-lsp/config.json`'s `index.indexPrefixMap`. Pass
`--store <path>` (repeatable) to override. On a large monorepo (~570k symbols, 4.9M
edges) a full build takes about 160 seconds and produces a ~1.5 GB database.

## What you get

Tables: `symbols`, `edges`, `occurrences`, `defs`, `files`, `units`, plus an FTS index
over camel-split names. Edges are normalized to `src -> dst`:

| Kind | Meaning |
|---|---|
| `CALLS` | caller to callee |
| `REFERENCES` | enclosing symbol to everything mentioned inside it |
| `CONTAINS` | type to member |
| `INHERITS` | subclass or conformer to base class or protocol |
| `OVERRIDES` | override to the requirement it satisfies |
| `EXTENDS` | extension to extended type |
| `ACCESSOR_OF` | getter or setter to its property |

Every edge row carries the source location of the relation, so "who calls this" comes
back with call sites, not just names.

## For coding agents

The MCP server exposes `index_status`, `search_graph`, `trace_path`, `find_references`,
`get_code_snippet`, `query_graph`, `check_index_coverage`, `get_architecture`,
`get_schema` and `build_visualizer`. It resolves the database from the session's working
directory. The bundled skill tells an agent when to reach for the graph instead of grep,
and which gotchas to respect.

## Limits worth knowing

- **It is a snapshot of the last compile.** Line numbers drift and new symbols are absent
  until you re-run `idxg-build`. Rebuilding is cheap; trusting a stale trace is not.
- **Coverage equals what was compiled.** Targets nobody built have no records, so
  `idxg coverage <path>` before concluding that nothing calls a symbol. A miss means "not
  compiled", not "not used".
- **Objective-C needs a build-system change.** rules_swift indexes Swift while building;
  clang has no equivalent Bazel feature. See [docs/objc-index-store.md](docs/objc-index-store.md).
- **One store per checkout.** Bazel output bases differ per worktree, so each checkout
  gets its own database.

## How it works

- `src/idxstore.py` binds `libIndexStore.dylib` through ctypes: units, records,
  occurrences, symbol relations.
- `src/build.py` walks every unit in parallel, writes per-worker SQLite shards, merges
  them, resolves definition sites, computes degrees, builds the FTS index.
- `src/idxg.py` is the query layer.
- `src/viz.py` renders the HTML explorer (overview, cross-module call graph with
  click-to-isolate, symbol explorer with callers and callees).
- `src/mcp_server.py` is a stdio MCP server wrapping the same commands.

MIT licensed.
