---
name: ios-codebase-indexer
description: Query a Swift/ObjC code graph built from the compiler's own index store, with exact call and reference edges, plus the project's commit history and its own markdown docs. Use for "who calls X", "what does X call", "where is X referenced", callers of a protocol requirement, override chains, cross-module call weights, or any structural question where a tree-sitter graph or grep would guess; and for "who changed X and why", "when did module Y appear", "what changed in Z this quarter", "what does the repo's documentation say about W". Also covers building and refreshing the graph, the history db, the HTML explorer and the knowledge-vault export.
---

# ios-codebase-indexer (idxg)

A code graph whose edges come from `libIndexStore`, so calls, references, overrides and
conformances are the ones the compiler resolved, each with the exact `file:line` of the
relation. Prefer it over grep for structure, and over a parser-derived graph always.

## Pick the right tool

| Question | Use |
|---|---|
| who calls / what calls this, override or conformance chains | `idxg trace` (MCP `trace_path`) |
| every use of a symbol, with read/write/call roles | `idxg refs` |
| find a symbol, filter by kind/module/file/degree | `idxg search` |
| module-level coupling, layer counts, hotspots | `idxg arch` |
| anything expressible as SQL over symbols/edges | `idxg sql` |
| unused code, dead-code candidates | `idxg dead --verify` |
| literal text, comments, strings, uncompiled files | ripgrep |
| files the compiled build never touched | `idxg coverage` first, then ripgrep |
| who changed this, when, in which PR, and why | `idxg history log --symbol X --narrate`, then `idxg history show` |
| what happened this week, in plain language, by area | `idxg history digest [--week 2026-W36]` |
| what changed in a module or path since a date | `idxg history log --module M --since` |
| where change concentrates, hotspots by churn | `idxg history churn` |
| how the project evolved, when something appeared or went away | `idxg history timeline` |
| what the repo's own docs say (README, CLAUDE.md, design docs) | `idxg docs search`, `idxg docs list --module M` |

## Commands

```bash
idxg status
idxg search "location chip mapper"
idxg search --name '^MyPrefix' --kind Struct,Class,InstanceMethod --limit 20
idxg search --file 'Sources/Feature/**' --min-degree 40
idxg trace MyReducer --direction in --depth 2 --first
idxg trace reduce --kind CALLS,REFERENCES --direction out --fanout 15 --first
idxg refs MyType
idxg snippet MyType
idxg sql "SELECT kind, COUNT(*) n FROM symbols WHERE in_repo=1 GROUP BY kind ORDER BY n DESC"
idxg arch
idxg dead --verify --module MyModule
idxg coverage Sources/Feature SomeFile.swift
idxg viz --scope MyModule --open
idxg schema

idxg history log --symbol MyReducer --limit 10        # commits touching its file
idxg history log Modules/Feature/X/ --since 2026-01-01 --files
idxg history log --narrate --module MyModule --limit 10   # one paragraph per commit
idxg history show '#21447'                            # PR description, files, module attribution
idxg history digest --week 2026-W36                   # the week narrated, grouped by area
idxg history digest --html --open                     # same, as the vault's digest page
idxg history churn --by module --since 2026-01-01
idxg history timeline --periods 4                     # prose, newest periods
idxg docs search "dependency injection"
idxg docs list --module MyModule
idxg docs show Documentation/Testing.md --max-bytes 8000
idxg history build                                    # pull new commits, resync docs
idxg history vault --out <vault dir>                  # clippings for a knowledge vault
```

`--json` on any query command, `--db <path>` to target another project's graph. Symbols
resolve by bare name, `Module.Name`, or USR; ambiguous names list candidates unless you
pass `--first`.

## Building and refreshing

```bash
idxg-build --jobs 8                     # from the repo root
idxg-build --store <path> --store <path2> --root <repo> --db <path>
```

Stores are auto-detected and merged: Xcode and xcodebuild's
`DerivedData/<Project>-<hash>/Index.noindex/DataStore` (matched to the project by the
`WorkspacePath` in its `info.plist`), sourcekit-lsp's background index
(`.index-build/index`, `~/.sourcekit-lsp/index-build`), a sourcekit-bazel-bsp store, and
the plain bazel build's `_global_index_store` when
`--features=swift.index_while_building` is on. Xcode indexes Objective-C by default,
which the Bazel Swift rules do not, so an Xcode build is the cheapest way to get `.m`
coverage. The database lands at
`~/.cache/indexstore-graph/<repo-dir-name>.db` and `idxg` resolves it from the current
directory's name.

## Operational notes and gotchas

- **It is a snapshot of the last compile, not the working tree.** After edits, line
  numbers drift and new symbols are missing until you rebuild. Rebuild rather than trust
  a stale trace.
- **Coverage equals what was compiled.** In a monorepo where the IDE's build server
  declares a subset of targets, everything outside that closure plus its dependencies has
  zero records, and unbuilt test/snapshot targets are usually the biggest gap. Run
  `idxg coverage <path>` before any "nothing calls this" claim; a miss means "not
  compiled", not "not used". Building a target with your normal build tool widens the
  graph, so re-run `idxg-build` after a build.
- **Objective-C needs a build-system change.** rules_swift has an index-while-building
  feature; clang has no Bazel equivalent, so ObjC targets emit nothing until you pass
  `-index-store-path` as a copt. See `docs/objc-index-store.md` in the repo. Until then,
  treat ObjC callers as invisible here and use ripgrep for them.
- **Accessors are their own symbols.** Swift properties appear both as
  `InstanceProperty foo` and `InstanceMethod getter:foo` / `setter:foo`; call edges land
  on the accessor, containment on the property. Trace with `--kind CALLS,ACCESSOR_OF`
  when a property looks orphaned.
- **`REFERENCES` is the broad edge.** It comes from the index's `containedBy` relation
  (enclosing symbol to everything mentioned inside it), includes type annotations, and
  outnumbers `CALLS` several times over. Default to `--kind CALLS` and add `REFERENCES`
  deliberately.
- **Edge direction is normalized to src to dst**: `CALLS` caller to callee, `CONTAINS`
  parent to member, `INHERITS` subtype to base, `OVERRIDES` override to requirement,
  `EXTENDS` extension to extended type, `ACCESSOR_OF` accessor to property.
- **`in_repo = 0` means the definition lives outside the repo** (SDK, package
  dependency, generated build output). `idxg search` hides those unless you pass `--all`;
  they are still valid trace endpoints.
- **Module names come from the compile unit**, so test targets appear as their own
  modules and the heaviest cross-module call weights are usually tests into the module
  under test. Filter those when reading `arch` output.
- **Symbols with no definition site** are declared only in a module interface the build
  consumed without source, so `snippet` cannot show them.
- **Dead-code output is capped by coverage.** `idxg dead` defaults to Swift, skips
  vendored trees, entry points, protocol witnesses and synthesis-driven members, and
  still cannot see references from files the build never compiled. Always pass
  `--verify`, and report survivors as candidates rather than as unused code.
- **`idxg viz` size scales with the slice.** Repo-wide defaults to the top 1500 symbols
  by degree; `--scope <Module>` includes every symbol in that module. Keep
  `--per-node-cap` near 14 to stay under ~8 MB.
- **Keep calls cheap.** `trace_path` caps rows and bytes and tells you when it truncated;
  narrow with `--fanout`, `--depth` or `--kind` rather than raising the caps. An anchored
  literal `--name '^Foo$'` is an indexed lookup, a general regex scans every symbol.
  `idxg status` reads counts cached at build time, so it is cheap to call first.
- `usr_hash` is a 64-bit blake2 of the USR and is the primary key everywhere. Join on it,
  and use `usr` when you need the stable compiler identity.

## History and docs

- **History is `git log --first-parent` of the history branch** (`main`, then `master`,
  or `idxg config history_branch`). One row per merge, with body, PR number and ticket
  keys. It lives in `<project>-history.db` beside the graph and refreshes after every
  `idxg-build`; `idxg history build` refreshes it alone, incrementally.
- **Module attribution follows the compiled index.** A changed file maps to a module by
  the directory prefix the graph knows for it, so files the build never compiled belong
  to no module: `--module` filters miss them and per-module churn is a lower bound. Filter
  by path when completeness matters.
- **`--symbol` follows the definition file, not the symbol.** Unrelated edits to the same
  file appear. For "why", read the PR description with `idxg history show <sha|#PR>`.
- **"Why" comes from pull request descriptions fetched through `gh`.** Squash commits carry
  no body here, so without `gh auth login` the narration says only what files moved, and
  `idxg history digest` lists that as a gap. `--narrate` and the digest prefer the PR
  template section whose heading mentions what, why or context.
- **Digest areas are ordered by the code graph, tags by subject and labels.** A module the
  rest of the code calls a lot sorts early, a leaf feature late; `prod` means the word
  hotfix appeared, `build` means only tooling files changed. Neither is a judgement of
  importance, and a fix whose title lacks the word is untagged.
- **The timeline is a factual digest.** Every sentence is a count from the log: commits,
  authors, most touched modules, distinctive subject words, directories first or last
  seen, largest change. Quote it as data, not as a judgement of what mattered.
- **Docs are the tracked markdown files.** `published` is a file's last commit date, not
  the date its content is true; `mechanical_refreshed` (from a `Last refreshed:` footer)
  dates generated sections. When a doc and the graph disagree, the graph is the compiled
  code. `idxg docs search` is BM25 over content; `idxg docs show` truncates at
  `--max-bytes` and says so.
- **Vault export is append-only.** `idxg history vault` writes clippings with
  `source: repo-doc` or `source: git-history` frontmatter and never rewrites a file; a
  changed source becomes a date-suffixed clipping. Closed periods are stable, so only the
  current period and changed docs produce new files on re-run.

## MCP

Tools: `index_status`, `search_graph`, `trace_path`, `find_references`,
`get_code_snippet`, `query_graph`, `check_index_coverage`, `get_architecture`,
`get_schema`, `build_visualizer`; history and docs: `get_history`, `get_commit`,
`get_churn`, `get_timeline`, `get_digest`, `list_docs`, `search_docs`, `get_doc`,
`refresh_history`. `get_history` with `narrate: true` gives one paragraph per commit. The server resolves the database from the session's
working directory; pass `db` to override.
