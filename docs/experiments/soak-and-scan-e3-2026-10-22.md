# E3: mixed-session soak and scan cost

This experiment uses the source at `7a525080c701d774cbdf8192de642e4fc47ac235`
(Waves 1–7 plus the Wave 8 plan). The provider is a deterministic local script;
no model API calls or credentials are used. Every daemon gets disposable profile
paths and an explicitly injected disposable physical-authority root. The soak keeps
the default legacy execution contract; it does not perform a cutover. Neither
script opens the developer's real profile or account authority records.

## Reproduction

Use an all-extras development environment. The duration is in seconds; the
soak defaults to 30 minutes and a repeatable seed.

```sh
PYTHONPATH=src python scripts/soak.py --duration 3600 --seed 20261022 \
  --restart-interval 120 --cycle-interval 12 --output /tmp/soak.json
PYTHONPATH=src python scripts/scan_scale.py --files 20000 100000 \
  --repeats 5 --output /tmp/scan-scale.json
# Optional growing-source stress (distinct from the default steady soak):
PYTHONPATH=src python scripts/soak.py --duration 3600 --growing-paths \
  --seed 20261022 --restart-interval 120 --cycle-interval 12 \
  --output /tmp/soak-growing.json
```

The soak opens long-lived shared and isolated conversations, then repeatedly
runs concurrent read/write/finish tasks against `docs/shared.txt` and
`docs/isolated.txt`. These two stable paths are updated again on every cycle.
Both conversations alternate automatic publication and held proposals followed
by `task.apply`; every third cycle undoes both changes to their previous accepted
bytes. Automatic publication enters the ledger before the held peer is applied,
and every publication, apply and undo gets an immediate ledger check. The
optional `--growing-paths` workload creates new paths each cycle instead; its
undos intentionally restore absence. A separate thread edits an unrelated source
file every 0.7 seconds. Every fifth cycle commits and switches between two same-tree Git
branches while both model executions hold private edits. Every two minutes the
daemon is killed after private edits, then restarted; durable provider state
resumes both tasks and both sessions are resumed before their following turn.

The byte ledger checks every prior accepted publication after each publish and
each cycle, retaining prior bytes for stable-path undos and explicit absences
for growing-path undos. Journal,
task and ownership-marker checks run after every cycle. At the end both sessions
close; the check requires no linked private worktree, active isolated workspace,
private runtime generation or copied private value. Source-only recovery
archives are allowed, but every recorded private path must have been removed
and the private runtime value must be absent from all archive files.

The driver freezes the daemon's script bytes before its first boot, so restarts
cannot silently pick up an edited harness. The result records its SHA-256,
every product source hash, UTC start/end, interpreter, bounds and measurements.
Failed runs retain their private disposable directory for diagnosis. Successful
runs remove it unless `--keep` is set.

## Soak result

The [bounded-path one-hour run](soak-bounded-e3-2026-10-22.json) **passed**
all periodic and final invariants and resource bounds: **300 cycles, 27 real
daemon restarts and 3,600.26 active seconds**. Both conversations closed, every
ownership marker was clean, no journal remained unrecovered, no task was stuck,
and no private worktree or copied runtime value remained. The entire disposable
root was then removed. The source and frozen harness hashes were independently
rechecked before any cleanup-fix merge.

The run started at 01:19:58 UTC and finished at 02:23:42 UTC on October 23
(October 22 locally). Closing the Mac's lid caused a sleep/wake from 21:58:08 to
22:01:54 local time despite the idle-sleep assertion. The monotonic clock paused
for about 224 seconds; the run continued unchanged to its complete active hour.
UTC wall duration was 3,824.15 seconds. No duration was shortened or counted
through sleep.

The separate earlier growing-path stress **failed** its original 128 MiB
SQLite database/sidecar bound at 52 minutes. Its bound was not changed.

| Attempt | Elapsed | Cycles / restarts | Result |
| --- | ---: | ---: | --- |
| [Frozen bounded-path steady run](soak-bounded-e3-2026-10-22.json) | 3,600.3 s active | 300 / 27 | Passed every invariant, original resource bound and final cleanup |
| [Initial creation-race workload](soak-initial-failure-e3-2026-10-22.json) | 229.1 s | 19 / 1 | Failed during an unstable isolated open; orphan-runtime defect recorded below |
| [Interrupted development run](soak-interrupted-e3-2026-10-22.json) | 132.7 s | 11 / 1 | Deliberately stopped to strengthen actual-directory cleanup checks; incomplete |
| [Frozen growing-path stress](soak-growing-e3-2026-10-22.json) | 3,135.4 s | 261 / 23 | Failed: SQLite files reached 134,693,672 bytes, exceeding 134,217,728 |

All attempts requested 3,600 seconds. Only the bounded-path run completed a
full active hour and passed; the other three remain failed or incomplete.
The growing-path run used harness SHA-256
`25d88f734939d80a9ba32ed7c99c8b78970b6cc98738e36154dac94257f18196`;
the final bounded workload uses
`c4af9ea63fab31c77b467383cd4547074a3088eecfc990c71f74e94962a2a9f6`.
Both freeze identical product-source bytes at the baseline named above. The old
harness predates the workload option, so its original JSON has no workload
field; its frozen hash identifies the growing-path implementation.

Bounds for both workloads are 128 MiB each for SQLite database/sidecar files
and candidate bytes,
256 open descriptors, 48 threads, 768 MiB RSS and 5,000 ms RPC p99. Missing metrics
fail the run, and the client retains its real five-second per-RPC timeout.
The bounded run recorded 301 samples, including final cleanup:

| Metric | Original bound | Bounded-path high water |
| --- | ---: | ---: |
| SQLite database and sidecar bytes | 134,217,728 | 28,124,920 |
| Candidate bytes | 134,217,728 | 60,887 |
| Open descriptors | 256 | 15 |
| Threads | 48 | 6 |
| RSS bytes | 805,306,368 | 55,050,240 |
| RPC p99 | 5,000 ms | 1,870.36 ms |

Its 2,405 RPCs measured p50 25.71 ms, p95 1,676.77 ms, p99 1,870.36 ms and
maximum 2,387.27 ms. The growing-path run's accepted samples peaked at
55,552 candidate bytes,
15 descriptors, 6 threads and 56,868,864 bytes RSS. Its 2,110 RPCs measured
p50 14.1 ms, p95 1,848.1 ms, p99 2,256.3 ms and maximum 2,927.5 ms.
The failing database sample is recorded in the exception rather than appended
to the accepted samples. The ledger, recovery and marker checks passed in the
preceding cycles, but the database failure prevented final cleanup checks.
Its failed temporary root remains diagnostic evidence.

The growing workload retains synchronization manifests as its source set
expands. A diagnostic snapshot at 153 cycles contained 1,524 sync-journal rows,
including 37 MB of manifest JSON. The bounded workload instead updates the same
two paths throughout; it is a separately frozen steady-conversation experiment,
not a repaired result for the growing stress. Journal retention remains a
product follow-up rather than a bound increase in this experiment.

The first attempted workload opened a fresh isolated conversation each cycle
while the outside writer was already running. At 19 cycles / 229 seconds,
creation correctly refused an unstable source snapshot. No lost write was
observed. The final workload instead opens its long-lived conversations before
starting the outside writer, then exercises repeated turns under that writer.
Creation races are a different admission scenario, not silently retried here.
That rejected open also left a quarantined partial worktree containing its
approved `.env` copy, with no activated session row. This is recorded as a
baseline lifecycle defect; the separate cleanup follow-up has its own focused
regressions. The failed temporary root is retained as diagnostic evidence, and
no developer profile or real credential was used.

The scripted negative control deletes a file immediately after its successful
publication. `--inject-lost-write` must exit nonzero with `published change lost
or corrupted`; the automated test drives this through the same real daemon,
kill/restart, publication and file ledger as the normal run.

## Harness verification

The [verification manifest](e3-verification-2026-10-22.json) records commands,
receipt hashes, resource high water and the distinct source/validation versions.

The final harness commit `675a173f5fffe6ee838ff9846017085e7f8a5361` passed
**3,398 tests**, with two expected platform skips and the two scale gates
selected separately. Those scale gates also passed. The 22 focused E3 tests
cover both real workload modes, repeated stable-path publication and undo to
previous bytes, actual daemon restart, installed outside changes, lost-write
injection, missing metrics and every resource bound. Repository Ruff, formatting
of the three new files and mypy's 128 source files passed.

The full correctness run used the all-extras development environment and
`PYTHONPATH=src python -m pytest -q --cov=llm_cli -m 'not index_scale and not
admission_scale'`; it finished in 1,439.78 seconds. Isolated test subprocesses
measure the installed environment's identical source at another path. All 128
source-file SHA-256 values were compared before coverage aliases were combined;
no code was excluded. Their combined line coverage is **87.41%**. The Wave 8
integrated candidate, including later product changes, has separate validation
and is not the frozen product source of this hour experiment.

The orphan-runtime cleanup follow-up `2a158a32644204ae762888ec8e7c501954ed51c1`
was independently reviewed and all 15 new lifecycle regressions passed. They
prove cleanup after source-capture and initialized-worktree failures, recovery
and rebuild rejection; live, held, foreign and unknown-source data retain their
ownership protections. The frozen hour source stays unchanged while that fix
is validated separately.

## Scan measurements

Raw samples: [scan-scale-e3-2026-10-22.json](scan-scale-e3-2026-10-22.json).
Each synthetic file contains the same 35 bytes of Python source. The identical
content makes cold capture a lower-bound workload for candidate-artifact I/O;
warm metadata scans still visit every supported source path. The production
limits and caches are unchanged. Fixture construction is outside the timings.

| Repository files | Outside cold | Outside warm median, 5 passes | Outside one changed file | Isolated capture cold | Isolated capture warm median, 5 passes | Full isolated safe point |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 20,000 | 140 ms | 85 ms | 84 ms | 2,674 ms | 1,173 ms | Not admitted: hydration metadata budget |
| 100,000 | 183 ms | 140 ms | 148 ms | Not supported | Not supported | Not admitted: 20,000 source-path budget |

An outside scan covers **only the latest 1,000 paths read by its session**, on
both repositories. These times do not represent full 20,000- or 100,000-file
coverage. The benchmark seeds all read identities, then reports the actual
number of resulting external observations, without raising that cap.

The isolated capture timings call the same `capture_source(..., cache=...,
defer_changed=True)` component used by a coding runner's safe point. They
exclude synchronization journal/manifest work, private-worktree validation and
authority acquisition. They are explicitly component timings, not completed
end-to-end safe points. On repositories admission can support, the script also
measures the full `IsolatedSynchronizer.synchronize` path; a small fixture test
checks that an actual external change is installed by that path.

A separate **real end-to-end growth run** starts with an admitted 7,000-file
isolated workspace, then adds source in 45-file increments. Each increment
passes through the production synchronizer; the 50-file publication limit is
unchanged. After 289 increments / 520.8 seconds, the accepted manifest contains
exactly 20,000 regular source leaves plus 14 directory markers. Five further
unchanged full safe points take **1,944 ms median** (range 1,891–1,967 ms).
Changing one primary source file takes **2,382 ms** and its new bytes are
verified in the private worktree. There are no deferrals or conflicts. This
provides the requested real 20,000-file safe-point measurement without raising
admission, capture or batch limits.

The growth setup also leaves **981,360,640 bytes** in SQLite database and sidecar files
before teardown. This is separate from the hour-soak resource bound: each real
growth sync retains a whole accepted manifest in its journal. Large-repository
work therefore needs a storage-retention review as well as faster invalidation.
The source scanner samples and this growth run were recorded separately; the
raw result includes both script hashes. During the growth run, only the CLI's
failure exit-status clause changed; removing that clause reproduces the exact
measured script hash, and its measurement functions are unchanged.

At 20,000 source files, initial isolated admission cannot complete the hydration
inventory's default metadata budget. At 100,000 files, source enumeration itself
refuses the 20,000-path budget. The script records each unsupported outcome;
it never truncates a fixture or calls a rejected scan complete.

## Recommendation for C12

C12's 250 ms gate is exceeded by both measurements: an unchanged
20,000-file capture takes 1.17 seconds, and the full safe point after legitimate
source growth takes 1.94 seconds. Proceed
with C12's watcher design for invalidation hints, preserving exact identity
checks at publication and a bounded full-rescan fallback after watcher loss.

First make the admission and coverage limits explicit in that design: an
outside scan currently watches a 1,000-read window, and a 100,000-file isolated
workspace cannot be opened. Watchers do not by themselves remove either limit.
The hydration metadata budget also prevents the nominal 20,000-file isolated
case from reaching an end-to-end safe point. Raising those budgets is separate
product work and was not done for this experiment.

Measurements were made on macOS 26.6.2 / Apple Silicon / Python 3.14.8 while
other development tests and the background soak were running. These are local
diagnostic timings, not an idle-machine release performance claim.
