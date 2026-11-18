# Clean handoff and cutover at scale, November 17, 2026

On this Apple Silicon Mac, C24 reacquired a checkout with 50,000 tracked
paths in 0.53 seconds. The same fixture on the 0.1.0a4 source at
`6c7f72b` refused clean handoff above 20,000 paths. Both tests used one
shared session and a forced idle release through the normal authority manager.

| Paths | Source MiB | 0.1.0a4 handoff | C24 handoff | C24 process peak RSS at handoff | 0.1.0a4 cutover source check | C24 cutover source check |
| ---: | ---: | --- | ---: | ---: | --- | --- |
| 20,000 | 70.8 | 0.26 s | 0.23 s | 65.3 MiB | Refused (64 MiB aggregate budget) | 4.24 s |
| 50,000 | 177.0 | Refused (path count) | 0.53 s | 89.7 MiB | Refused (path count) | 10.74 s |
| 100,000 | 354.0 | Refused (path count) | 1.13 s | 132.2 MiB | Refused (path count) | Refused (100,000 observed; limit 50,000) |

The 100,000-path handoff covers the upper configurable isolated count, including
handoff between profiles with different settings. The default admission level
remains 50,000. The 100,000-file row exercises recovery on a previously registered
checkout that grew. A successful handoff alone does not establish that isolated
hydration or a full task at that size is supported.

At the default 50,000 paths, 177 MiB of mixed-size text passed the new 256 MiB
aggregate budget with headroom. Cutover can read and screen every eligible file,
which cost roughly twenty times as much wall time as handoff here. Its 120-second
limit leaves room for slower storage; this run does not establish a worst-case
latency. The existing 16 MiB cap on a single file still bounds each individual
read. At 100,000 paths the count check runs first. At 20,000 on the old code, the
fixture exceeds its aggregate byte cap while every file fits individually and
the run took under four seconds. That code reports only a generic refusal.

## Reproduce

From the candidate checkout with the project environment active:

```sh
PYTHONPATH=src python scripts/scan_scale.py --authority --output /tmp/authority-after.json
```

For a baseline, point `PYTHONPATH` at the `src` directory of a separate
checkout of `6c7f72b` and run the same script. Each size starts a fresh Python
process. It opens a session on a one-file repository, grows the checkout to the
target count, stages the files, releases ownership, then times reacquiring it.
The cutover measurement invokes the production source/index checkpoint directly,
so an unsuccessful handoff cannot mask the first bound hit in cutover.

File sizes cycle through 512 bytes, 2 KiB, 4 KiB and 8 KiB. The files contain
synthetic source and benefit from warm filesystem caches. Timings exclude
fixture construction. RSS includes the daemon and fixture setup; the JSON also
records increases above the previous process high-water mark. These are single
samples on macOS arm64, with watchers off and no model calls. [Baseline
data](authority-scale-c24-2026-11-17-before.json) and [C24
data](authority-scale-c24-2026-11-17.json).
