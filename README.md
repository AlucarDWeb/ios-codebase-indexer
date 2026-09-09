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

Six tabs: overview, a cross-module call graph where clicking a module isolates its
calls, a symbol browser with callers and callees, dead-code candidates, the project's
history told period by period, and the repository's own docs.

**6b. Ask about the past.**

```bash
idxg history timeline                     # the project's history in prose
idxg history log --symbol MyReducer       # who changed it, when, in which PR
idxg docs search "dependency injection"   # the repo's markdown docs, full text
```

`idxg init` and every `idxg-build` extract the main branch's `git log` and the tracked
markdown docs into a second database beside the graph. See
[History and docs](#history-and-docs) for what is in it and what it cannot tell you.

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
idxg dead --verify                        # symbols nothing in the indexed build reaches
idxg coverage Sources/Feature             # what the compiled index actually covers
idxg sql "SELECT kind, COUNT(*) n FROM symbols WHERE in_repo=1 GROUP BY kind"
idxg viz --scope MyModule --open          # HTML explorer
idxg schema                               # tables, edge kinds, role bits
idxg deinit --purge                       # un-index: skill, CLAUDE.md block, registry, graph, history, explorer

idxg history build                        # git log of main + repo docs -> <project>-history.db
idxg history log Sources/Feature/ --files # commits touching a path, with changed files
idxg history log --symbol MyType          # commits touching the file that defines a symbol
idxg history log --narrate --since 2026-09-01   # one plain paragraph per commit: who, what, why
idxg history show '#1234'                 # one commit or PR in full: description, files, modules
idxg history digest                       # this week, every change narrated and grouped by area
idxg history digest --week 2026-W36 --html --open   # the same as a standalone digest page
idxg history churn --by module            # where change concentrated in the last year
idxg history timeline --periods 4         # narrative history, one paragraph per period
idxg history vault --out ~/my-vault       # history and docs as knowledge-vault clippings
idxg docs list --module MyModule          # a module's README, CLAUDE.md, design docs
idxg docs search "path resolver"          # full-text search over the docs
idxg docs show Documentation/Testing.md   # print one doc
```

Every query command takes `--json`, and `--db <path>` to point at another project's
graph. Symbols resolve by bare name, `Module.Name`, or USR; an ambiguous name lists
candidates unless you pass `--first`.

## Removing a project

```bash
idxg deinit            # remove the project skill and the CLAUDE.md block, forget the project
idxg deinit --purge    # also delete the graph, the history db, the explorer, digests and the
                       # default vault export under ~/.cache/indexstore-graph
```

`deinit` keeps a project skill that has a `## Project notes` section unless you pass
`--force`, and never touches a vault you pointed `history_vault` at. `idxg autoindex
--uninstall` removes the launchd agent, which is per machine rather than per project.

## Where the index store comes from

`idxg-build` auto-detects, and merges everything it finds:

| Source | Path |
|---|---|
| Xcode and `xcodebuild` | `~/Library/Developer/Xcode/DerivedData/<Project>-<hash>/Index.noindex/DataStore` |
| sourcekit-lsp background indexing (SwiftPM, plain projects) | `.index-build/index`, `~/.sourcekit-lsp/index-build` |
| sourcekit-bazel-bsp | `<output_base>/sourcekit-bazel-bsp/execroot/_main/bazel-out/_global_index_store` |
| bazel with `--features=swift.index_while_building` | `<output_base>/execroot/_main/bazel-out/_global_index_store` |

Every store found is merged, and records dedupe by name, so a project built two ways
gets the union. Bazel paths come from `.sourcekit-lsp/config.json`'s
`index.indexPrefixMap`; DerivedData directories are matched to the project by the
`WorkspacePath` in their `info.plist`. Pass `--store <path>` (repeatable) to override.

**Xcode and xcodebuild need no setup.** Both index while building by default
(`COMPILER_INDEX_STORE_ENABLE`), and unlike the Bazel Swift rules they index
Objective-C too, so an Xcode-built project gets `.m` files and even IB outlet
relationships for free. `xcodebuild -scheme MyApp build` followed by `idxg init` is the
whole flow for a non-Bazel project.

On a large monorepo (~790k symbols, 6.9M edges from four stores) a full build takes
about 220 seconds and produces a ~2.2 GB database.

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

## History and docs

The code graph says what the code is. A second database, `<project>-history.db` beside
the graph, says how it got there and what the repository says about itself.

**Commit history.** `idxg history build` runs `git log --first-parent` on the history
branch (`main`, then `master`, unless `idxg config history_branch=...` says otherwise), so
each row is one merge to that branch with its full diff stats, message body, PR number
and ticket keys. Runs are incremental: after the first one only new commits are read.
Every changed file is attributed to a module by the directory prefix the graph knows for
that module, and to a component directory at module depth. From that come `log`, `show`,
`churn` and `timeline`.

**Pull request descriptions.** Squash merges usually drop the PR description from the
commit, and that description is where "why" lives. When `gh` is installed and logged in,
the build fetches title, body, labels and merge date for every PR-numbered commit through
the GitHub GraphQL API, forty per request, and only for commits it has not seen. Turn it
off with `idxg config history_prs=false` or `--no-prs`.

**Narration, commit by commit.** `idxg history log --narrate`, `idxg history show` and the
explorer's history tab write one plain paragraph per commit: who merged what and when,
the sentences of the PR description that say what changed and why (template sections
whose heading mentions what, why or context are preferred), which files and modules it
touched and how, renames, labels. Every clause maps to a field of the commit.

**Weekly digest.** `idxg history digest` narrates a week: headline and stats, the changes
that stood out (hotfixes and fixes first, then the largest), then every change grouped by
area. Areas are ordered the way the code depends on itself, using the graph's cross-module
call counts: tooling first, modules the rest of the code calls next, leaf features later,
tests and docs last; one-change modules fold into shared "Other" tiles with a subhead per
module. `--html` writes the digest as a page in the fixed layout the knowledge vault's
weekly digest uses (`src/templates/weekly-digest.html`, two placeholders, nothing else
changes), and the explorer embeds the most recent weeks (`viz_history_weeks`, default 26)
in that same layout.

**Narrative timeline.** `idxg history timeline` and the explorer's history tab tell the
project's history one paragraph per year, quarter or month depending on the span. Every
sentence is computed: commit and author counts, most touched modules, subject words that
are distinctive for the period, directories that first appear or were last touched, the
largest change. It is a factual digest, not an interpretation.

**Repository docs.** The same build collects every tracked markdown file (vendored trees
and changelogs excluded, or the include and exclude globs of a JSON manifest named by
`idxg config docs_manifest=...`), with its last-commit date, a `Last refreshed:` date when
the doc has generated sections, and a module attribution. `idxg docs search` is a BM25
full-text search over their content.

**Vault export.** `idxg history vault --out <dir>` writes the docs, the period
narratives and one note per module into `<dir>/Clippings/` in the shape an LLM-maintained
knowledge vault compiles from: frontmatter with `source`, `path`, `url`, `published`,
`hash`; a file is never rewritten, and a changed source becomes a new date-suffixed
clipping. Point it at an existing vault with `--out` or `idxg config history_vault=...`.

Limits, stated once here and again in the output:

- A commit is whatever the branch records. Squash merges make one row per PR; a rebased
  or force-pushed branch triggers a full re-extract.
- Module attribution follows the compiled index. A file the build never compiled belongs
  to no module, so per-module counts are lower bounds and `--module` filters miss it.
  Filter by path when that matters.
- `--symbol` follows the file that defines the symbol. Line-level history is not tracked,
  so unrelated edits to the same file appear too.
- A doc's `published` date is its last commit, not the date its content is true. Where a
  doc and the graph disagree, the graph reflects the compiled code.

## For coding agents

The MCP server exposes `index_status`, `search_graph`, `trace_path`, `find_references`,
`get_code_snippet`, `query_graph`, `check_index_coverage`, `get_architecture`,
`get_schema`, `build_visualizer`, and for history and docs `get_history`, `get_commit`,
`get_churn`, `get_timeline`, `get_digest`, `list_docs`, `search_docs`, `get_doc` and
`refresh_history`. `get_history` takes `narrate` for one paragraph per commit. It resolves the database from the session's working
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
- `src/history.py` extracts `git log` and the tracked markdown docs into
  `<project>-history.db`, attributes paths to modules through the graph, and writes the
  narrative and the vault export.
- `src/mcp_server.py` is a stdio MCP server wrapping the same commands.

MIT licensed.
