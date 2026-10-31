# Larger isolated admission (C18), October 30, 2026

The recorded experiment supports defaults of **50,000 source paths** and
**100,000 hydration entries**. The unchanged byte, depth, time, source identity,
no-follow and secret-consent bounds still apply. A 100,000-file checkout remains
unsupported; a configurable ceiling is not a claim that every layout fits it.

## Method

[The complete machine-readable record](isolated-admission-c18-2026-10-30.json)
retains the initial refusals and all intermediate attempts. The final admission
revision is `08dceb9fb9c0bf02929cd91248f6cd90273f958e`, on
macOS 26.6.2 arm64 with Python 3.12.13. Each fixture has 35-byte Python files in
1,000-file shards. Each size runs in a fresh interpreter with native watchers
active and explicit 100,000 source/inventory count limits. An unused scripted
provider avoids model requests. No installer or model API runs.

```sh
uv run python scripts/scan_scale.py --admission \
  --files 20000 50000 100000 --repeats 5 \
  --source-max-paths 100000 --hydration-max-entries 100000 \
  --output admission.json
```

The script records creation, cold/five warm/one-edit safe points, SQLite plus
sidecar bytes, separate parent/subprocess peak RSS, and close/removal. It checks
that the changed source bytes reached the private worktree. Source preparation,
fixture creation and open timings remain separate.

## Final measurements

| Source files | Admission | Open | Cold safe point | Warm median | One edit | DB bytes | Parent peak RSS | Close |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20,000 | Complete | 11.474 s | 2.687 s | 169.98 ms | 256.88 ms | 20,717,568 | 174,047,232 B | 2.930 s |
| 50,000 | Complete | 30.401 s | 6.596 s | 249.48 ms | 399.15 ms | 50,417,664 | 352,698,368 B | 7.079 s |
| 100,000 | Refused | 17.399 s | — | — | — | 921,600 | 130,383,872 B | — |

Both admitted workspaces closed with state `removed`, with their private roots
absent. At 50,000 files the warm samples were 240.67–253.44 ms: this default has
little margin around 250 ms, and its changed-file sample is 399.15 ms. The existing
20,000-file performance gate remains 250 ms; C18 does not raise it. The 20,000-file
changed sample above was 256.88 ms and is retained without selecting a better run.
These small-file measurements justify counts, not a latency guarantee for large
bodies, deeper layouts or different hardware.

The 100,000-file attempt reports `entry_budget_exceeded` and
`git_metadata_budget_exceeded`. Directories and administration also consume
inventory entries. Its staged-index stream also exceeds the unchanged 4 MiB
Git output bound. No successful safe point is reported for that refusal.

## What made admission possible

The initial count-scaffold baseline refused all three sizes under the old
hydration metadata representation. Ordinary source entries retained the same
full stat metadata as runtime. C18 keeps every spelling/alias in a private
bounded catalogue and retains full receipts for runtime, policy, directories
and unsafe decisions. Actual catalogue allocation, retained metadata and active
pipe/parser buffers share the existing 4 MiB budget. The catalogue never reads
file content and is removed before the preview returns; checkout-controlled
`TMPDIR` cannot redirect it into the checkout.

Profiling the first compact implementation at 50,000 files found 9.63 seconds in
Git ignore inspection, including 9.53 seconds waiting on 39 child processes.
Batching and streaming alone still exhausted the unchanged 10-second inventory
budget. The final implementation streams staged-index records into bounded
catalogue flags, then uses no-index ignore inspection only for paths whose
tracked status has been accounted for. Exact Git spelling is required for
ordinary tracked files. Directory, case/Unicode ambiguity and indexed
file/symlink/gitlink prefixes use ordinary Git classification. Both staged-index
fingerprints and ignore classifications are repeated; drift disables copies.

## Actual-default privacy and boundary evidence

Revision `ef7a32c70cfab535b2e5c900fd186a27ecf09459` sets 50,000/100,000 as the
product defaults. Two separate actual-default openings contain exactly 50,000
ordinary source files, including `.gitignore`, plus an approved `.env`. They
complete safe points, retain a protected non-signature private-value receipt,
copy that value into a late source file, clear the in-memory guard, then recover
through a fresh manager and close. Their opening measurements include repository
registration and session acknowledgement, unlike the table's session.open-only
measurement.

The clean case opens in 34.046 s, has five warm samples of 224.92–236.90 ms, and
closes in 10.730 s with its private root removed. The case with ordinary unknown
build output opens in 34.736 s, has warm samples of 233.87–238.53 ms, and closes in
3.153 s preserving that output. Both remove the `.env` and late private copy,
retire protected private-value seeds, and keep the canary absent from database
and sidecar files after recovery. The 5-second/1 GiB private-source scrub bounds
are unchanged. The optional generated-output classifier retains its 20,000-entry
bound, so larger process workspaces may preserve ordinary recovery evidence.

The actual-default boundary control attempts a 50,001-source opening and requires
refusal naming `isolated.source_max_paths=50000` before any isolated-workspace
record is created. Configured ceilings remain 100,000, and byte/time/depth limits
still take precedence when they cannot admit a particular checkout.


## Bounded cleanup follow-up

A later quiet combined run without coverage exhausted the unchanged five-second
private-source sweep after deleting known canary copies, so its protected seed
correctly remained for recovery. A direct diagnostic reached the deadline at
5.00009 seconds. Moving relative-path calculation to matching leaves reduced
redundant work, but one of two actual-default cleanup flows still refused; that
partial improvement was not accepted as the final result.

Revision `a56e1246c955e720732743b3ac243625b1864cfc` also excludes only stable
ordinary leaves whose byte length proves they cannot contain any retained
private value. The conservative minimum uses character counts: replacement
UTF-8 decoding cannot produce more code points than input bytes. No-follow
metadata pinned before and after the observation must agree. Changed leaves,
symlinks and blocked guards retain validated full reads; a changed private-value
epoch prevents retirement. Every leaf still charges its count and actual byte
length, under the original per-file, 1 GiB and five-second limits. The exclusion
creates no content identity or receipt.

Two fresh actual-default 50,000-source flows completed. Their full private sweeps
were 1.15333 and 1.16881 seconds, leaving 3.84667 and 3.83119 seconds under the
existing cap. Both removed the approved `.env` and later source canary and
retired protected seeds after restart. The clean worktree was removed; ordinary
unknown build output remained in the other worktree. Opens took 34.628/34.727
seconds and closes 9.291/1.751 seconds. Warm safe points were 222.23–234.05 ms
and 231.43–241.06 ms. These receipts describe this fixture and hardware; all
other count, byte, depth and time refusals remain in force.
