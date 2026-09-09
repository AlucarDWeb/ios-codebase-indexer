# codebase-brain

A queryable code graph for Swift and Objective-C, built from the index store the
compiler already writes, plus the main branch's commit history (with PR descriptions) and
the repo's own docs. Python 3.9+, macOS, standard library only. No third-party packages,
no build step, no tests directory yet. Renamed from `ios-codebase-indexer` on 2026-09-09;
the CLI stayed `idxg`.

## Layout

| File | Lines | Role |
|---|---|---|
| `src/idxstore.py` | 112 | ctypes bindings for `libIndexStore.dylib`: units, records, occurrences, symbol relations |
| `src/build.py` | 492 | parallel extractor, SQLite writer, atomic swap, registry update |
| `src/project.py` | 316 | project registry, store detection, layered config, staleness |
| `src/idxg.py` | 1751 | the CLI: every subcommand, plus the shared query helpers |
| `src/viz.py` | 1161 | HTML explorer: data slicing and the whole page as one Python string |
| `src/deadcode.py` | 98 | dead-code candidate query and its text cross-check |
| `src/history.py` | 1513 | git log, PR descriptions (via `gh`) and repo docs into `<project>-history.db`; module attribution via the graph; per-commit narration, weekly digest, timeline, vault export |
| `src/templates/weekly-digest.html` | | the knowledge vault's fixed digest layout, copied verbatim; only `{{TITLE}}` and `{{DIGEST_JSON}}` are substituted |
| `src/mcp_server.py` | 333 | stdio MCP server wrapping the CLI functions |
| `bench/bench_mcp.py` | | latency and payload size per MCP tool |

`bin/idxg`, `bin/idxg-build` and `bin/idxg-history` are POSIX sh wrappers that resolve symlinks before
locating `src/`, so `install.sh` can link them into `~/.local/bin`.

## Data model

Everything is keyed by `usr_hash`, a 64-bit blake2 of the compiler's USR. Join on it;
use `usr` when you need the stable compiler identity.

Tables: `symbols`, `edges`, `occurrences`, `defs`, `files`, `units`, `meta`, plus an
FTS5 index over camel-split names and its `fts_map`.

History lives in a second file, `<graph>-history.db`, so a graph rebuild never touches
it. Tables: `commits` (one row per first-parent commit of the history branch), `commit_files`
(with `module` and `component` attribution), `components` (module-depth directories with
first and last commit and whether the branch still has them), `docs` plus `docs_fts`, `meta`
(`head_sha` is the incremental cursor). `commit_files.path` equals `files.rel` in the graph;
that equality is the only join between the two databases.

Edges are normalized to `src -> dst` at extraction time, from the index store's relation
roles: `CALLS` (calledBy), `REFERENCES` (containedBy), `CONTAINS` (childOf), `INHERITS`
(baseOf), `OVERRIDES`, `EXTENDS` (extendedBy), `ACCESSOR_OF`, `RECEIVED_BY`,
`SPECIALIZES`, `IB_TYPE_OF`. Every row carries the source location of the relation.

## Traps, all of which have bitten

- **`viz.py` holds the page as a Python string, so escapes are interpreted at import.**
  A bare `\u0000` written in that string becomes a real NUL byte in the HTML, which the browser
  parser silently drops, breaking whatever depended on it. Write `\\uXXXX` for a JS-level
  escape, or prefer a printable separator. This cost an hour once already.
- **Never reuse a cursor for lookups while iterating a query on it.** Doing so resets the
  running statement and truncates the iteration, which once produced 14 edges instead of
  347,000. `slice_data` keeps separate `lookup` and `ecur` cursors for that reason.
- **Restore `row_factory` if you change it.** `slice_data` borrows the caller's
  connection; clobbering the factory breaks the caller's dict-style row access.
- **Anything embedded in the page's JSON must escape `</`.** The history slice carries the
  digest template, which ends in `</script>`; unescaped inside `__DATA__` it closed the
  explorer's script early and the page rendered raw JSON. `render` and `render_digest`
  both replace `</` with `<\/`, and the iframe `srcdoc` path does the same in JS.
- **`GLOB`, not `LIKE`, for name matching.** SQLite's `LIKE` is case-insensitive for
  ASCII and will return more than the equivalent regex, disagreeing with the slow path it
  is meant to replace.
- **Swift symbol names carry argument labels** (`map(_:)`, `subscript(_:)`). Strip at the
  first `(` before any text search, or ripgrep finds nothing.
- **`build.py` writes to `<db>.building` and swaps.** Clear `-wal`/`-shm` on *both* sides
  of the swap: a stale journal beside the destination makes SQLite report
  `database disk image is malformed`. Any writer touching a live database (the status
  cache does) must leave no WAL behind. A `<db>.lock` file keeps two builds from racing.
- **The explorer path must derive from the database path.** `viz` and `build` once
  derived it differently and wrote two divergent files, so `idxg open` served a stale
  page.
- **Structural edges make `in_deg` useless for dead code.** Every member has a `CONTAINS`
  parent, so dead-code queries must filter on the semantic edge kinds only. See
  `deadcode.py` for the full exclusion list and the reason behind each.
- **Module attribution comes from the graph, so build the graph first.** `history.py`
  derives each module's directory prefix from `files.rel` and cuts it before a `Sources` or
  `Tests` segment. Modules whose files share no directory get no prefix and are
  unmatchable; two modules on one prefix keep the one with more files. About a quarter of
  Wallapop's changed files map to a module; the rest are uncompiled or legacy trees.
- **Bare columns beside MIN/MAX are intentional.** `_rebuild_components` relies on SQLite
  filling a bare column from the row that produced the aggregate. Do not "fix" it into a
  subquery without keeping the same row.
- **The history build must not leave a WAL either.** It opens the history db in DELETE
  journal mode and removes `-wal`/`-shm` on exit for the same reason `build.py` does.
- **git quotes unusual paths.** The log runs with `core.quotepath=false` and `_rename`
  strips residual quotes; without that a directory named `"Modules/...` appeared in
  `components`.

## Semantics to preserve

- **The graph is a snapshot of the last compile.** Staleness is a fingerprint of each
  store (unit count plus directory mtime) recorded in `meta`, compared on use. Never
  present a query result as current without that check.
- **Coverage bounds every negative claim.** A file the build never compiled has no
  records, so "nothing references this" is unprovable there. Commands that could imply
  absence say so in their output, and so should any new one.
- **Agent payloads are budgeted.** `trace_path` and `get_code_snippet` cap rows and bytes
  and state why they truncated. Summary calls cache their expensive counts in `meta` at
  build time. Keep new tools in that shape, and re-run `bench/bench_mcp.py` if you touch
  an output path.
- **The old name must keep working for anyone who installed it.** `project.py` moves the
  legacy config and cache directories on import and rewrites registry paths; `idxg init`
  and `deinit` recognise the `ios-codebase-indexer` CLAUDE.md markers and the
  `codebase-index` project skill (carrying its Project notes over); `autoindex` unloads the
  old launchd label; `install.sh` removes the old skill link and MCP registration. Remove
  these paths only with a release note that says which version dropped them.
- **Nothing may assume one repository's layout.** Layers are derived from path segments,
  not hardcoded module names. This ships to other projects. The same holds for history:
  the branch is resolved (`main`, `master`, origin's HEAD), component depth is the median
  depth of module prefixes, docs are every tracked `.md` unless a manifest says otherwise.
- **The narrative is computed, never interpreted.** Every sentence `history.narrate`,
  `narrate_commit` and `week_digest` emit maps to a field or count in the db. Add a fact by
  adding a query; do not add adjectives. The digest template is the vault's and is not
  edited here: a layout change belongs in the vault first, then a fresh copy.
- **PR descriptions are optional enrichment, never required.** `enrich_prs` runs only when
  `gh auth status` succeeds and the origin is GitHub; every reader must cope with
  `pr_body` being NULL (not fetched) or `''` (fetched, PR gone).
- **The vault export is append-only.** `_write_clipping` never rewrites a file; a changed
  source becomes a date-suffixed clipping and the state file records which. That is the
  contract the knowledge vaults compile from, so a re-run must produce zero new files when
  nothing changed.

## Verifying a change

```bash
python3 -c "import ast,pathlib; [ast.parse(pathlib.Path(f).read_text()) for f in __import__('glob').glob('src/*.py')]"
cd /path/to/an/indexed/project && idxg-build --jobs 8 && idxg status
python3 /path/to/repo/bench/bench_mcp.py          # latency and payload per MCP tool
idxg history build && idxg history timeline --periods 2 && idxg docs search "architecture"
idxg history vault --out /tmp/v && idxg history vault --out /tmp/v   # second run writes 0 files
```

For an explorer change, render it headless rather than trusting the code, because the
interactive parts fail silently:

```bash
python3 - <<'PY'
import pathlib
src = pathlib.Path("<explorer>.html").read_text()
pathlib.Path("/tmp/probe.html").write_text(src.replace(
    "overview();\n</script>",
    "overview();showTab('modules');selectModule(modState.idx.get('SomeModule'));\n</script>"))
PY
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
  --window-size=1500,1200 --virtual-time-budget=9000 --screenshot=/tmp/probe.png \
  "file:///tmp/probe.html"
```

`--dump-dom` instead of `--screenshot` when you need to assert on structure. Appending a
probe element that writes computed values into the DOM is how the NUL separator bug was
found.

## Conventions

- Standard library only. A dependency would break `install.sh` being a symlink script.
- Comments explain the non-obvious: a framework quirk, an ordering that matters, a value
  that looks wrong but is intentional. No narration of what the code says.
- Documentation states limits plainly. Coverage caveats and false-positive classes belong
  in the output and the docs, not only in a commit message.
- `README.md` is the user-facing surface, `skill/codebase-brain/SKILL.md` is what
  an agent reads. Both need updating when a command or a default changes, and so do the
  MCP tool descriptions in `mcp_server.py` and the project skill text in `idxg.py`.
- `idxg init` writes a project skill and a CLAUDE.md block into the target repo.
  Everything below `## Project notes` in that skill is the user's and survives re-init.
