# codebase-brain

A brain for a codebase that coding agents can query. It holds three things about a
project in one place and serves them as MCP tools, as a CLI (`idxg`), and as an HTML
explorer:

- the code graph the compiler already knows: every symbol, call, reference, override and
  conformance it resolved, each with the exact `file:line`;
- the commit history of the main branch, with the pull request description behind each
  merge;
- the repository's own documentation, searchable by content.

An agent working in the repository does not grep for "who calls this" or guess "why was
this changed". It calls `trace_path` and gets the resolved call sites; it calls
`get_history` with `narrate` and gets the pull requests that touched the file, in prose;
it pastes a crash report into `triage_crash` and gets, per frame, the symbol, its callers
and what changed there since the last release. Every answer is computed from the compiler,
git and GitHub, and every payload is capped so a call stays affordable.

Editors get "jump to definition" from the compiler's index store; codebase-brain turns the
same data into a SQLite graph. Because the edges come from the compiler, "who calls this"
returns the calls that were actually resolved, with no parsing and no guessing about
generics or dynamic dispatch. Today it reads Swift, Objective-C, C and C++ through that
store. The history and docs layers do not depend on the language, and another language
would need only a second extractor writing the same tables.

## How an agent uses it

`./install.sh` registers the MCP server with Claude Code, and `idxg init` in a project
writes two things into it: a project skill (`.claude/skills/codebase-brain/SKILL.md`) that
says when to reach for the graph instead of grep and which caveats to respect, and a block
in the project's CLAUDE.md that points at it. From then on the agent picks the tools by
itself. Nothing in the agent's prompt needs to change.

The tools, by the question they answer:

| Question | Tool | Same thing on the CLI |
|---|---|---|
| Is the graph fresh, what does it cover, is there a newer release | `index_status` | `idxg status` |
| Find a symbol by words, regex, kind, module, file | `search_graph` | `idxg search` |
| Who calls this, what does it call, override and conformance chains | `trace_path` | `idxg trace` |
| Every use of a symbol with read, write, call roles | `find_references` | `idxg refs` |
| The definition, read from disk | `get_code_snippet` | `idxg snippet` |
| Any SQL over the graph | `query_graph` | `idxg sql` |
| Was this file compiled at all (before claiming "unused") | `check_index_coverage` | `idxg coverage` |
| Layers, modules, cross-module hotspots | `get_architecture` | `idxg arch` |
| Candidates nothing reaches | `find_dead_code` | `idxg dead` |
| Who changed this, when, in which PR, and why | `get_history` (`narrate`) | `idxg history log --narrate` |
| One commit or PR in full: description, files, modules | `get_commit` | `idxg history show` |
| Where change concentrates | `get_churn` | `idxg history churn` |
| How the project evolved, period by period | `get_timeline` | `idxg history timeline` |
| What shipped this week, every change narrated by area | `get_digest` | `idxg history digest` |
| A crash report: per frame, symbol, callers, commits since a release | `triage_crash` | `idxg crash` |
| The repo's own docs: list, full-text search, read one | `list_docs`, `search_docs`, `get_doc` | `idxg docs` |
| Rebuild when the graph or history is behind | `refresh_index`, `refresh_history` | `idxg refresh`, `idxg history build` |
| Table and column reference | `get_schema` | `idxg schema` |

A typical crash investigation, as an agent runs it: `triage_crash` with the trace and
`since` set to the previous release tag; `get_commit` on the two or three PRs it lists, to
read what they meant to do; `trace_path` on the frame's symbol to see every caller;
`get_code_snippet` to read the definition. Four calls, all bounded, and every claim
traceable to a compiler edge or a commit.

Payloads are budgeted. `trace_path`, `get_history`, `triage_crash` and `get_doc` cap rows
and bytes and say when they truncated, so the agent narrows the question rather than the
tool flooding the context. `index_status` reads counts cached at build time and is cheap
to call first. The MCP server resolves the database from the session's working directory
and takes `db` to target another project.

Everything an agent gets, a person gets from the same functions on the CLI, and the
explorer shows the same data as pages.

## Triage a crash

Paste a crash report and get back, for every frame that lives in your repository, the
symbol behind it, who calls it, and what changed there since the release you name.

```bash
idxg crash crash.txt --since v1.328.0
```

The input can be an Apple crash report, an lldb backtrace, Sentry's frames, or any text
that carries `Type.method(labels:)` and `File.swift:line`. Mangled Swift names are
demangled. Given this backtrace:

```
frame #0: 0x0000000104a1b2c4 Wallapop`SearchReducer.reduce(_:_:) at SearchReducer.swift:80
frame #1: 0x0000000104a1c000 Wallapop`IconView.apply(viewModel:) at IconView.swift:80
```

the output is:

```
2 frames parsed, 0 in system images, 2 resolved to this repo; commits since 2026-08-10

#0  reduce(_:_:)  InstanceMethod  SearchFeature
    Modules/Feature/SearchFeature/Sources/UI/SearchReducer.swift:62  (trace line 80)  resolved by file:line
    callers (3 shown):
      sections(afterFavoriteToggle:)  SearchFeature_Tests  SearchRootViewContentInvalidationSpec.swift:180  (x2)
      ...
    commits touching SearchReducer.swift since 2026-08-10:
      2026-09-04  6149e25a545  [WPA-116102] Hide the location chip when the response carries no location quick filter  #21766  (Anderson da Silva)
      2026-09-03  8f11f82ef3c  [WPA-116013] Let the backend be the only gate on the location chip  #21754  (Anderson da Silva)

#1  apply(viewModel:)  InstanceMethod  Conchita
    Conchita/Sources/Foundations/Icons/IconView.swift:75  (trace line 80)  resolved by file:line
    callers (3 shown):
      prepareForReuse()  Conchita  ListItem.swift:238  (x4)
      ...
    commits touching IconView.swift since 2026-08-10: none

most recently changed frame: reduce(_:_:) in SearchReducer.swift, 2026-09-04 6149e25a545 ...
this is what changed near the crash, not why it crashed; read the callers and the PR bodies (idxg history show) before deciding.
```

Frames in system images (UIKit, libswiftCore, libdispatch and the like) are skipped. A
frame is resolved by file and line when the trace has them, otherwise by name, and a type
the graph does not know is reported as unresolved rather than matched to another type's
method of the same name. Callers list product code before tests. `--since` takes a date or
any git ref, so the previous release tag is the natural value: the commits it lists are
the ones that could have introduced the crash.

The same thing is one MCP call for an agent: `triage_crash` with the trace text and
`since`. The agent then reads the pull requests it names with `get_commit`, follows the
callers with `trace_path`, and reads the definition with `get_code_snippet`. Four bounded
calls, and every claim in its answer points at a compiler edge or a commit.

What this cannot do, and no static tool can: know why it crashed. The graph has no runtime
data and no expression-level detail, so a force unwrap, a race or a nil is invisible to
it. It gives you the places and the pull requests to read first, with the exact lines.

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
idxg crash crash.txt --since v1.328.0     # per frame: symbol, callers, commits since the tag
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

### Crash triage

See [Triage a crash](#triage-a-crash) above. `idxg crash` reads a file or `-` for stdin;
`--frames`, `--callers` and `--commits` set how much of each it prints, `--json` gives the
data, and `--max-bytes` caps the text the way every other command does.

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

See [How an agent uses it](#how-an-agent-uses-it) above for the tool table and the flow.
Two things worth adding. The bundled skill in `skill/codebase-brain/SKILL.md` is what an
agent reads through `~/.claude/skills`; it carries the gotchas (accessors are separate
symbols, `REFERENCES` is broad, coverage bounds every negative claim). And the project
skill `idxg init` writes has a `## Project notes` section at the end that is yours:
project-specific quirks you add there survive every re-init.

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

## Updating

`idxg status` (and the MCP `index_status` tool) asks GitHub at most once a day whether a
newer release exists and prints a line when one does. `idxg update --check` asks now.
`idxg update` pulls the release into the checkout the CLI runs from and re-runs
`install.sh`; graphs and history databases need no rebuild afterwards, but a running
Claude Code session needs a restart to pick up the new MCP server code. Turn the daily
check off with `idxg config --global update_check=false`.

## Removing a project

```bash
idxg deinit            # remove the project skill and the CLAUDE.md block, forget the project
idxg deinit --purge    # also delete the graph, the history db, the explorer, digests and
                       # the default vault export under ~/.cache/codebase-brain
```

`deinit` keeps a project skill that has a `## Project notes` section unless you pass
`--force`, and never touches a vault you pointed `history_vault` at. The launchd agent is
per machine rather than per project; `idxg autoindex --uninstall` removes it.

## How it works

- `src/idxstore.py` binds `libIndexStore.dylib` through ctypes: units, records,
  occurrences, symbol relations.
- `src/build.py` walks every unit in parallel, writes per-worker SQLite shards, merges
  them, resolves definition sites, computes degrees and builds the FTS index, then
  refreshes the history and renders the explorer.
- `src/history.py` extracts `git log`, fetches PR descriptions through `gh`, collects the
  tracked markdown docs, attributes paths to modules through the graph, and writes the
  narration, the digest, the timeline and the vault export.
- `src/crash.py` parses stack traces and resolves frames to symbols for `idxg crash`.
- `src/idxg.py` is the CLI and the query layer.
- `src/viz.py` renders the HTML explorer.
- `src/mcp_server.py` is a stdio MCP server wrapping the same commands.

MIT licensed.
