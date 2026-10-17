# C7 isolated runtime measurements — 2026-10-17

A cloned Loupe checkout with its actual `.venv` (6,576 regular targets) ran the
configured no-op check `.venv/bin/python -c pass` through `CheckRunner.run`.
Measurements include worktree creation, exact source capture, private runtime
copy, sandboxed command, output screening, and cleanup. No model calls were made.

| Full check | Wave 3 baseline | C7 candidate |
| --- | ---: | ---: |
| First run | 3.912 s | 1.785 s |
| Repeat 1 | 3.032 s | 1.428 s |
| Repeat 2 | 3.033 s | 1.452 s |
| Repeat 3 | 3.037 s | 1.471 s |

The baseline is `fa6632a` (product source from `0a0c9f7`); the candidate is
`3ecf6b1d24c160799679f2fd3295637f3b26cc92`. This was CPython 3.12.13 on
macOS arm64 with filesystem COW available. Timings are local observations,
not cross-platform latency guarantees. Every measured repeat met the plan's
1.5-second target; the first cold run did not.

Profiling found repeated runtime identities and path operations dominated the
baseline. C7 uses descriptor scans, cached exact identities and successful-copy
evidence, and at most four COW workers/eight in-flight regular files for an
already pinned private dependency tree. It caches source secret-classification
booleans by exact content identity, never file bodies. Source/race checks still
re-read bytes; copies stay writable and private. Failed workers are drained
before their directory descriptors close. A disposable write-isolation probe
must pass before filesystem clones touch dependencies.

Tests cover same-size/restored-mtime changes, parent replacement, nested Git
refusal, copy-worker failure, broken COW implementations, newly screened output
values, private incoming fixture recovery, and close cleanup. Incoming screened
changes retain the existing 8 MiB changed-path limit; larger changes defer that
exact path without blocking unrelated work. Opening without secret consent
omits each unapproved file with an exact `--approve-secret PATH` notice.

See [the isolated guide](../isolated-workspaces.md) and
[ADR 0009](../adr/0009-isolated-runtime-hydration.md) for privacy and recovery.
