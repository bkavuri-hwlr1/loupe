# C16: bounded isolated synchronization journals

The source at `e915693935a47c369d473a76f51fb53827705b97` was frozen before the
required growing-path hour and remained unchanged through completion. The
unchanged soak harness is `c4af9ea63fab31c77b467383cd4547074a3088eecfc990c71f74e94962a2a9f6`. Both benchmark scripts use
disposable repositories, profiles, and explicit disposable physical authority;
the soak uses its deterministic local provider and the growth run uses an unused
local adapter. No billed calls, user profile, or account authority were used.

Prepared journals retain their full replay manifest. One confirmation transaction
advances the accepted manifest/generation, updates each path's complete unresolved
conflict step, clears the applied manifest to `{}`, and retains only eight applied
rows per workspace. The migration replays the last applied step per path, including
duplicate paths and later clearances, before retiring history. Invalid applied
step evidence aborts the migration transaction. Prepared rows remain unchanged.

The [full growing-path hour](soak-growing-c16-2026-10-24.json) passed all original
invariants and bounds: **3603.14 seconds, 355 cycles,
27 daemon restarts**, followed by successful session/worktree/private
runtime cleanup and removal of the complete disposable root. The run used
`--duration 3600 --growing-paths`; `max_cycles` stayed null. The unchanged default
cycle interval is 10 seconds. The earlier [failed receipt](soak-growing-e3-2026-10-22.json)
remains preserved; its 128 MiB database-and-sidecar bound was not raised.

| Resource | Observed peak | Original bound |
| --- | ---: | ---: |
| SQLite database and sidecars | 25,395,976 bytes | 134,217,728 bytes |
| Candidate artifacts | 75,138 bytes | 134,217,728 bytes |
| Open files | 15 | 256 |
| Threads | 6 | 48 |
| Memory | 54,067,200 bytes | 805,306,368 bytes |
| RPC p99 | 2263.26 ms | 5,000 ms |

The unchanged [7,000-to-20,000-file real synchronization run](scan-scale-c16-2026-10-24.json)
completed 289 increments of 45 files in 496.92 seconds.
It accepted 20,000 ordinary leaves plus 14 directory markers, with no deferrals or
conflicts. Database and sidecar size was **17,575,936 bytes**, compared
with the earlier **981,360,640 bytes**. Five unchanged complete safe points had
median **1855.44 ms**; one external change took
**2053.00 ms** and its installed private
bytes were verified. Initial 20,000-file admission still reports its existing
hydration metadata limit. Growth starts from an admitted 7,000-file workspace;
admission, capture, and 50-file batch limits are unchanged. These are local
macOS 26.6.2 / Apple Silicon / Python 3.12.13 timings while other test work and
the soak were running.

A separate [legacy SQLite page-reuse fixture](sync-journal-legacy-pages-c16-2026-10-24.json)
held 100 applied journals with 23,889,000 manifest bytes. Upgrade left eight rows,
16 manifest bytes and 5,892 free pages. Actual SQLite file sizes stayed at
28,426,992 bytes during migration. A subsequent 512 KiB probe consumed 129 free
pages without increasing page count or actual file size. No vacuum or manual
checkpoint was used. Existing databases retain their physical high-water file
size and reuse freed pages.

The full correctness run passed **3,535 tests**, with one case-sensitive-filesystem
skip and only the two production timing gates deselected. Combined line/branch
coverage displayed **88%**. Both unchanged production scale gates then passed
separately without coverage. Lint and formatting passed for changed source and
tests; strict mypy passed for both changed source modules. New tests cover 200
growing syncs, exact conflict/artifact retention after compaction and restart, legacy upgrade with prepared rows, every
migration SQL boundary, every confirmation/compaction SQL and commit boundary,
and screened staging through owned-worktree retirement. Existing conflict tests
passed unchanged.

Candidate artifacts and proposal references keep their existing lifetime; the
candidate store has no garbage collector to change here. Compact conflict steps
retain exact local/incoming artifact hashes and private screened staging tokens.
A crash after confirmation but before unlink can leave resolved screened staging;
the existing owned private Git-registration cleanup removes it on retirement.
Tests verify both that orphan case and unresolved staging after history pruning.
Sync state and workspace records remain as evidence while owned worktree retirement
continues to succeed.

[Verification metadata](c16-verification-2026-10-24.json) records commands, test
results, checksums and final source verification. Supplementary
[read-only journal samples](sync-journal-samples-c16-2026-10-24.json) show only eight
applied rows and 16 manifest bytes despite thousands of confirmed generations.
One supplementary monitor comparison used the C13 review worktree by mistake;
that entry names its actual comparison root and is followed immediately by the
correct absolute-C16-root check. The original harness and measured C16 source
were untouched.
