# codebase-brain

A brain for a codebase, for people and for coding agents. It holds three things about a
project in one place:

- the code graph the compiler already knows: every symbol, call, reference, override and
  conformance it resolved, each with the exact `file:line`;
- the commit history of the main branch, with the pull request description behind each
  merge;
- the repository's own documentation, searchable by content.

Editors get "jump to definition" from the compiler's index store. codebase-brain turns the
same data into a SQLite graph with a CLI (`idxg`), an MCP server for agents, and a
self-contained HTML explorer. Because the edges come from the compiler, "who calls this"
returns the calls that were actually resolved, with no parsing and no guessing about
generics or dynamic dispatch.

Today it reads Swift, Objective-C, C and C++ through the compiler's index store. The
history and docs layers do not depend on the language, and another language would need
only a second extractor writing the same tables.

## Install

```bash
git clone https://github.com/AlucarDWeb/codebase-brain.git
cd codebase-brain
./install.sh
```

The script links `idxg`, `idxg-build` and `idxg-history` into `~/.local/bin`, links the
Claude Code skill into `~/.claude/skills/`, and registers the MCP server when the `claude`
CLI is present.

You need macOS with Xcode installed (for `libIndexStore.dylib`) and Python 3.9 or newer.
There are no third-party packages. Pull request descriptions need the GitHub CLI (`gh`),
logged in; everything else works without it.

## First run, step by step

1. Check the CLI is on your PATH.

   ```bash
   idxg --help
   ```

   If that fails, add `~/.local/bin` to your PATH, or re-run `./install.sh` and read its
   last line.

2. Make sure the project has an index store. This is the one prerequisite. The compiler
   writes the store during a build or during sourcekit-lsp's background indexing.

   - SwiftPM or Xcode project: open it in an editor with sourcekit-lsp background
     indexing on, or build it once.
   - Bazel: build with `--features=swift.index_while_building`, or set up
     [sourcekit-bazel-bsp](https://github.com/spotify/sourcekit-bazel-bsp).

3. Index the project.

   ```bash
   cd /path/to/your/project
   idxg init
   ```

   It prints the stores it found, builds the graph, extracts the history and the docs,
   renders the explorer, and installs a project skill and a CLAUDE.md note for agents.
   Expect seconds on a small package and a few minutes on a large monorepo (the graph
   takes two to four minutes, the first history extract about two more, and fetching
   pull request descriptions a few more on top). If it reports no index store, go back
   to step 2.

4. See what you got, and what it does not cover.

   ```bash
   idxg status
   ```

   The coverage line is the one to read. It counts tracked sources that have index
   records. A low number is not a bug. It means much of the project was never compiled
   in the build that produced the store. Build more targets and run `idxg refresh` to
   raise it. The history line tells you how many commits and docs were extracted.

5. Try a query.

   ```bash
   idxg search "<something you know exists>"
   idxg trace <ASymbolFromThatSearch> --direction in --first
   idxg history log --symbol <ThatSymbol> --narrate
   ```

   The second command answers "who calls this" with the exact call sites. The third
   answers "who changed it, when, and why".

6. Look at it.

   ```bash
   idxg open
   ```

   The explorer has six tabs: an overview, a cross-module call graph where clicking a
   module isolates its calls, a symbol browser with callers and callees, dead-code
   candidates, the project's history (a weekly digest, every recent commit narrated, and
   the story period by period), and the repository's docs.

7. Keep it fresh.

   ```bash
   idxg autoindex --install --every 20
   ```

   A launchd agent then reindexes any registered project whose store has changed. Without
   it, run `idxg refresh` after builds. Queries warn on stderr when the graph has fallen
   behind.

8. Restart your agent. The MCP server was registered at install time, but a Claude Code
   session that was already running will not see it until you restart. The project skill
   and the CLAUDE.md note are picked up per project with no restart.

### If something looks wrong

| Symptom | Cause and fix |
|---|---|
| `no index store found` | Nothing has compiled this project yet. See step 2. |
| `no graph for <path>` | Indexed elsewhere or not yet. Run `idxg init`, or `idxg projects` to see what is registered. |
| Coverage far below 100% | Only compiled targets have records. Build the missing targets, then `idxg refresh`. |
| A symbol resolves to several candidates | Pass `--first`, or give `Module.Name` or the USR. |
| Line numbers are off | The graph predates your edits. Run `idxg refresh`. |
| `.m` files missing | Objective-C needs a build-system change. See [docs/objc-index-store.md](docs/objc-index-store.md). |
| `no history yet` | Run `idxg history build`. |
| Narration says only which files moved | Pull request descriptions were not fetched. Run `gh auth login`, then `idxg history build`. |

## Everyday commands

The code graph:

```bash
idxg-build --jobs 8        # index store -> ~/.cache/codebase-brain/<project>.db
idxg status                # counts, edge kinds, coverage, history

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
```

The history:

```bash
idxg history build                        # git log of main + PR descriptions + repo docs
idxg history log Sources/Feature/ --files # commits touching a path, with changed files
idxg history log --symbol MyType          # commits touching the file that defines a symbol
idxg history log --narrate --since 2026-09-01   # one plain paragraph per commit
idxg history show '#1234'                 # one commit or PR in full: description, files, modules
idxg history digest                       # this week: every change narrated, grouped by area
idxg history digest --week 2026-W36 --html --open   # the same as a standalone page
idxg history churn --by module            # where change concentrated in the last year
idxg history timeline --periods 4         # the story, one paragraph per period
idxg history vault --out ~/my-vault       # history and docs as knowledge-vault clippings
```

The docs:

```bash
idxg docs list --module MyModule          # a module's README, CLAUDE.md, design docs
idxg docs search "path resolver"          # full-text search over their content
idxg docs show Documentation/Testing.md   # print one doc
```

Every query command takes `--json`, and `--db <path>` to point at another project. Symbols
resolve by bare name, `Module.Name`, or USR. An ambiguous name lists candidates unless you
pass `--first`.

## History and docs, explained

The code graph says what the code is. A second database, `<project>-history.db` beside
the graph, says how it got there and what the repository says about itself. `idxg init`
and every `idxg-build` refresh it; `idxg history build` refreshes it alone.

### Commits

The build runs `git log --first-parent` on the history branch. That is `main`, then
`master`, unless `idxg config history_branch=...` names another. Each row is one merge to
that branch with its diff stats, message body, pull request number and ticket keys. Runs
are incremental: after the first one, only new commits are read.

Every changed file is attributed to a module by the directory prefix the graph knows for
that module, and to a component directory at module depth. `log`, `show`, `churn` and
`timeline` are built on that attribution.

### Pull request descriptions

Squash merges usually drop the pull request description from the commit, and that
description is where "why" lives. When `gh` is installed and logged in, the build fetches
the title, body, labels and merge date of every PR-numbered commit through the GitHub
GraphQL API, forty per request, and only for commits it has not seen before. Turn it off
with `idxg config history_prs=false` or `--no-prs`.

### One paragraph per commit

`idxg history log --narrate`, `idxg history show` and the explorer's history tab write one
plain paragraph per commit: who merged what and when; the sentences of the PR description
that say what changed and why (a template section whose heading mentions what, why or
context is preferred); which files and modules it touched and how; renames; labels. Every
clause maps to a field of the commit.

### The weekly digest

`idxg history digest` narrates a week. It opens with a headline and a few numbers, then
the changes that stood out (hotfixes and fixes first, then the largest), then every change
grouped by area. Areas are ordered the way the code depends on itself, using the graph's
cross-module call counts: tooling first, modules the rest of the code calls next, leaf
features later, tests and docs last. Modules with a single change fold into shared "Other"
tiles with a subhead per module.

`--html` writes the digest as a page in the fixed layout an LLM-maintained knowledge vault
uses for its weekly digest (`src/templates/weekly-digest.html`, two placeholders, nothing
else changes). The explorer embeds the most recent weeks in that same layout; the number
of weeks is the `viz_history_weeks` setting, 26 by default.

### The timeline

`idxg history timeline` and the explorer tell the project's history one paragraph per
year, quarter or month, depending on how long the history is. Every sentence is computed:
commit and author counts, the most touched modules, subject words that are distinctive for
the period, directories that first appear or were last touched, the largest change. It is
a factual digest, not an interpretation.

### Repository docs

The same build collects every tracked markdown file, minus vendored trees and changelogs,
or exactly the include and exclude globs of a JSON manifest named by
`idxg config docs_manifest=...`. Each doc carries its last-commit date, a `Last refreshed:`
date when it has generated sections, and a module attribution. `idxg docs search` is a
BM25 full-text search over their content.

### Vault export

`idxg history vault --out <dir>` writes the docs, the period narratives and one note per
module into `<dir>/Clippings/`, in the shape an LLM-maintained knowledge vault compiles
from: frontmatter with `source`, `path`, `url`, `published` and `hash`. A file is never
rewritten. A changed source becomes a new, date-suffixed clipping. Point the export at an
existing vault with `--out` or `idxg config history_vault=...`.

## Where the index store comes from

`idxg-build` detects every store below and merges them:

| Source | Path |
|---|---|
| Xcode and `xcodebuild` | `~/Library/Developer/Xcode/DerivedData/<Project>-<hash>/Index.noindex/DataStore` |
| sourcekit-lsp background indexing (SwiftPM, plain projects) | `.index-build/index`, `~/.sourcekit-lsp/index-build` |
| sourcekit-bazel-bsp | `<output_base>/sourcekit-bazel-bsp/execroot/_main/bazel-out/_global_index_store` |
| bazel with `--features=swift.index_while_building` | `<output_base>/execroot/_main/bazel-out/_global_index_store` |

Records dedupe by name, so a project built two ways gets the union. Bazel paths come from
the `index.indexPrefixMap` in `.sourcekit-lsp/config.json`. DerivedData directories are
matched to the project by the `WorkspacePath` in their `info.plist`. Pass `--store <path>`
(repeatable) to override the detection.

Xcode and xcodebuild need no setup. Both index while building by default
(`COMPILER_INDEX_STORE_ENABLE`), and unlike the Bazel Swift rules they index Objective-C
too, so an Xcode-built project gets `.m` files and even IB outlet relationships for free.
`xcodebuild -scheme MyApp build` followed by `idxg init` is the whole flow for a
non-Bazel project.

On a large monorepo (about 790k symbols and 6.9M edges from four stores) a full graph
build takes about 220 seconds and produces a 2.2 GB database. The history of that same
repository (18k first-parent commits, 14.7k pull requests, 369 docs) takes about two
minutes to extract plus five to fetch the descriptions, and about 290 MB.

## What is in the databases

The graph: `symbols`, `edges`, `occurrences`, `defs`, `files`, `units`, plus an FTS index
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

The history: `commits` (one row per merge, with the PR fields), `commit_files` (each
changed file with its module and component), `components` (module-depth directories with
first and last commit and whether the branch still has them), `docs` with a full-text
index, and `module_rank` (cross-module call counts used to order digest areas). The two
databases join on the repo-relative path. `idxg schema` prints both.

## For coding agents

The MCP server exposes the graph as `index_status`, `search_graph`, `trace_path`,
`find_references`, `get_code_snippet`, `query_graph`, `check_index_coverage`,
`get_architecture`, `get_schema` and `build_visualizer`; the history and docs as
`get_history` (with `narrate` for one paragraph per commit), `get_commit`, `get_churn`,
`get_timeline`, `get_digest`, `list_docs`, `search_docs`, `get_doc` and
`refresh_history`. Every payload is capped and says when it truncated.

The server resolves the database from the session's working directory. The bundled skill
tells an agent when to reach for the graph or the history instead of grep, and which
caveats to respect.

## Limits worth knowing

- The graph is a snapshot of the last compile. Line numbers drift and new symbols are
  absent until you rebuild. Rebuilding is cheap; trusting a stale trace is not.
- Coverage equals what was compiled. Targets nobody built have no records, so run
  `idxg coverage <path>` before concluding that nothing calls a symbol. A miss means "not
  compiled", not "not used".
- Objective-C under Bazel needs a build-system change. rules_swift indexes Swift while
  building; clang has no equivalent Bazel feature. See
  [docs/objc-index-store.md](docs/objc-index-store.md).
- One store per checkout. Bazel output bases differ per worktree, so each checkout gets
  its own database.
- A commit is whatever the branch records. Squash merges make one row per PR; a rebased or
  force-pushed branch triggers a full re-extract.
- Module attribution follows the compiled index. A file the build never compiled belongs
  to no module, so per-module counts are lower bounds and `--module` filters miss it.
  Filter by path when that matters.
- `--symbol` follows the file that defines the symbol, since line-level history is not
  tracked. Unrelated edits to the same file appear too.
- A doc's `published` date is its last commit, not the date its content is true. Where a
  doc and the graph disagree, the graph reflects the compiled code.
- The narration and the digest are computed, so they keep identifiers and read like a
  factual log. Tags come from subjects and labels: a fix whose title lacks the word is
  untagged.

## Removing a project

```bash
idxg deinit            # remove the project skill and the CLAUDE.md block, forget the project
idxg deinit --purge    # also delete the graph, the history db, the explorer, digests and
                       # the default vault export under ~/.cache/codebase-brain
```

`deinit` keeps a project skill that has a `## Project notes` section unless you pass
`--force`, and never touches a vault you pointed `history_vault` at. The launchd agent is
per machine rather than per project; `idxg autoindex --uninstall` removes it.

## Upgrading from ios-codebase-indexer

This tool was called `ios-codebase-indexer` until September 2026. The CLI is still `idxg`.
The first run after upgrading moves `~/.config/ios-codebase-indexer` and
`~/.cache/indexstore-graph` to `~/.config/codebase-brain` and `~/.cache/codebase-brain`.
Running `idxg init` in an already indexed project replaces the old CLAUDE.md block and
project skill with the new ones, keeping anything you wrote under `## Project notes`.
`install.sh` removes the old skill link and MCP registration.

## How it works

- `src/idxstore.py` binds `libIndexStore.dylib` through ctypes: units, records,
  occurrences, symbol relations.
- `src/build.py` walks every unit in parallel, writes per-worker SQLite shards, merges
  them, resolves definition sites, computes degrees and builds the FTS index, then
  refreshes the history and renders the explorer.
- `src/history.py` extracts `git log`, fetches PR descriptions through `gh`, collects the
  tracked markdown docs, attributes paths to modules through the graph, and writes the
  narration, the digest, the timeline and the vault export.
- `src/idxg.py` is the CLI and the query layer.
- `src/viz.py` renders the HTML explorer.
- `src/mcp_server.py` is a stdio MCP server wrapping the same commands.

MIT licensed.
