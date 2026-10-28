# C12: native file watchers and scan cost

This measures C12's watchers on E3's 20,000-file fixture with
`scripts/scan_scale.py`, at source `e907f4b5be96e6926c0fd81a963e31a5ac729cc3`.
The later reconcile-loop fix on the same branch does not touch the measured
paths. No provider is called and no limit is raised. Raw samples:
[scan-scale-c12-2026-10-28.json](scan-scale-c12-2026-10-28.json).

```sh
uv run python scripts/scan_scale.py --files 20000 --repeats 5 \
  --watchers both --output /tmp/scan-scale-c12.json
```

Each size is measured twice, in fresh daemons: with `[workspace] watchers =
"off"` and with `"auto"`. The growth run admits a 7,000-file isolated
workspace, grows the primary checkout to 20,000 files in 45-file steps through
the real synchronizer, and then measures the same admitted workspace with
watchers on and, after detaching them, off.

## Results

macOS 26.6.2, Apple Silicon, Python 3.12.13, on AC power with idle sleep
prevented. Medians of five passes unless one sample is shown.

| 20,000 source files | E3 (before C12) | C12, watchers off | C12, watchers on |
| --- | ---: | ---: | ---: |
| Real isolated safe point, unchanged | 1,944 ms | 1,273 ms | **144 ms** |
| Real isolated safe point, one outside edit | 2,382 ms | 1,366 ms | **215 ms** |
| Isolated capture component, warm | 1,173 ms | 1,087 ms | 19 ms |
| Outside scan of 1,000 reads, warm | 85 ms | 83 ms | 41 ms |
| Outside scan of 1,000 reads, one edit | 84 ms | 101 ms | 76 ms |
| Growth from 7,000 to 20,000 files | 521 s | — | 81 s |

With watchers on, both real safe points are under the 250 ms gate, and the
outside edit is installed in the private worktree. The edited path is the only
source file read again; Git does not list the source set for an in-place edit.

Watchers off is faster than E3 because C12 also validates the private worktree
twice per safe point instead of six times and skips identical manifest entries
before comparing them. E3 ran on Python 3.14.8, so its column is a baseline,
not a same-interpreter comparison.

The outside scan was already under the gate, because it covers only a session's
latest 1,000 reads. Watchers halve its warm cost; what remains is the Git
reconciliation check and the observation query, not file metadata. Its cold
pass also starts the watcher (295 ms against 269 ms).

As in E3, a 20,000-file isolated session cannot be opened directly: hydration
admission refuses it (`metadata_budget_exceeded`). The growth run reaches a
real 20,000-file safe point without raising that limit. The growth run's SQLite
files totalled 17,637,376 bytes, in line with C16's bound.

These are local diagnostic timings on a development machine, not a release
performance claim. Correctness evidence is in the test suite, not here.
