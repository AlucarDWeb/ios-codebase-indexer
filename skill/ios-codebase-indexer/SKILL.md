---
name: ios-codebase-indexer
description: Query a Swift/ObjC code graph built from the compiler's own index store, with exact call and reference edges. Use for "who calls X", "what does X call", "where is X referenced", callers of a protocol requirement, override chains, cross-module call weights, or any structural question where a tree-sitter graph or grep would guess. Also covers building and refreshing that graph and generating its HTML explorer.
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
```

`--json` on any query command, `--db <path>` to target another project's graph. Symbols
resolve by bare name, `Module.Name`, or USR; ambiguous names list candidates unless you
pass `--first`.

## Building and refreshing

```bash
idxg-build --jobs 8                     # from the repo root
idxg-build --store <path> --store <path2> --root <repo> --db <path>
```

Stores are auto-detected and merged: sourcekit-lsp's background index
(`.index-build/index`, `~/.sourcekit-lsp/index-build`), a sourcekit-bazel-bsp store, and
the plain bazel build's `_global_index_store` when
`--features=swift.index_while_building` is on. The database lands at
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

## MCP

Tools: `index_status`, `search_graph`, `trace_path`, `find_references`,
`get_code_snippet`, `query_graph`, `check_index_coverage`, `get_architecture`,
`get_schema`, `build_visualizer`. The server resolves the database from the session's
working directory; pass `db` to override.
