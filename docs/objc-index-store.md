# Indexing Objective-C

Swift and Objective-C are not equal here, and the asymmetry is in the build system, not
in this tool.

## Why ObjC produces nothing by default

rules_swift ships two features, `swift.index_while_building` and
`swift.use_global_index_store`. The first passes `-index-store-path` to `swiftc` and
declares the resulting directory as a Bazel `TreeArtifact` output. The second points
swiftc at a shared store instead, then has the worker copy records back into the declared
artifact with `index-import`, which is what keeps the action legal for Bazel.

Nothing equivalent exists for clang. Searching rules_apple, apple_support, rules_cc and
Bazel's own embedded tools for `index-store-path` returns nothing, and rules_xcodeproj
collects index stores only from Swift outputs. So an ObjC or ObjC++ target emits no index
records at all, and `.m` files are absent from the graph.

## Enabling it

Pass the flag as a raw copt on ObjC compiles. In a Bazel project with wrapper macros,
inject it centrally rather than per target:

```python
# config/objc_index_store.bzl
OBJC_INDEX_STORE_COPTS = select({
    "//config:objc_index_store_enabled": [
        "-index-store-path",
        "__BAZEL_EXECUTION_ROOT__/bazel-out/_global_index_store",
        "-index-ignore-system-symbols",
    ],
    "//conditions:default": [],
})
```

with a `bool_flag(name = "objc_index_store", build_setting_default = False)` and its
`config_setting`, then append `OBJC_INDEX_STORE_COPTS` to `copts` in your `objc_library`
wrapper and to `clang_copts` in your `mixed_language_library` wrapper. Two edits cover
every ObjC target, including macro-generated ones.

`__BAZEL_EXECUTION_ROOT__` is substituted with the action's working directory by
apple_support's `wrapped_clang`, which drives ObjC compiles on Apple platforms. That makes
the store path absolute at action time. A bare relative path also works for local spawns,
but is fragile.

Build with the flag, then re-index:

```bash
bazel build //... --//config:objc_index_store=true
idxg-build --jobs 8
```

## What to expect

- **Cache hits produce no records.** The write is an undeclared side effect with no output
  group, so an ObjcCompile served from the disk or remote cache never runs clang. Records
  accumulate only from actions that really execute. They persist in the store, so coverage
  builds up over time.
- **Sandboxed or remote spawns discard the write.** It survives when ObjC compiles run
  locally and unsandboxed, which is the common setup for large iOS builds
  (`--spawn_strategy` without `sandboxed`). Under a remote executor the records are lost.
  Getting them in CI would need a copy-back step like the one rules_swift's worker
  performs.
- **Every ObjC action key changes**, so the ObjC half of your local and remote caches is
  invalidated once.
- **Never put `-index-store-path` in Swift copts.** rules_swift disables its own indexing
  when it sees a user-supplied one, which would drop the declared `.indexstore` output.

Keeping the flag default-off means CI and other developers are unaffected until someone
opts in.
