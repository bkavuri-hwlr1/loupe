# Optional local lexical search

Loupe can index a registered checkout for local source discovery. This is an
explicit CLI/RPC feature: it does not add retrieval to the model's file tools,
acknowledge changes, select a write base or authorize publication. It needs no
provider, embedding service, native extension or network connection.

## Enable and build

Indexing is off by default. Set the shared configuration and restart the daemon
for the profile you use, then register and build the selected checkout:

```sh
loupe config set knowledge.index lexical
loupe daemon restart
loupe repo add .
loupe index build --repo .
loupe index status --repo .
loupe search lexical SharedBatchPublisher --repo . --explain
```

The equivalent configuration is:

```toml
[knowledge]
index = "lexical"
```

The setting accepts `"off"` or `"lexical"`. The configuration file is shared by
profiles; each daemon loads it on startup. Keep the same `--profile` for config,
restart, registration and search when using a non-default profile.

A build subscribes that physical checkout to indexing and creates its initial
generation. A background worker then projects accepted shared publications and
observed outside edits. An isolated session's successful publication updates the
primary checkout's index; its private intermediate files are never search inputs.

## Commands and citations

| Command | Purpose |
| --- | --- |
| `loupe index status [--repo PATH]` | Show this checkout's health, or all subscriptions when `--repo` is omitted. |
| `loupe index build [--repo PATH]` | Subscribe and build an initial generation; repository defaults to `.`. |
| `loupe index rebuild [--repo PATH]` | Reenable the subscription and rebuild from an exact current Git HEAD. |
| `loupe index drop [--repo PATH]` | Disable that checkout's subscription, then remove its derived source. |
| `loupe search lexical QUERY [--repo PATH] [--limit N] [--explain]` | Search one checkout. The limit defaults to eight and accepts one through twenty. |

Quote a multi-word query. `--explain` shows the lexical retrieval reason;
JSON output also includes its rank. Human output includes the file and actual
line span, full content identity,
indexed revision and generation. A revision is an exact `git:<commit>` or
`checkout-event:<sequence>`. Changed or unavailable current files are labeled
`stale indexed version`; an old chunk is never presented as current merely
because its path still exists.

Use global `--json` for the structured citation fields:

```sh
loupe --json search lexical SharedBatchPublisher --repo . --limit 8 --explain
loupe index rebuild --repo .
loupe index drop --repo .
```

Dropping disables one checkout's subscription before removing derived rows.
Other indexed checkouts remain subscribed. Drop/rebuild on unchanged source
produces the same chunk IDs.

## Which versions are indexed

An initial build reads immutable regular Git blobs at one exact HEAD commit.
Existing accepted immutable artifacts can supplement that baseline only when
their exact identity still matches the checkout. Unobserved dirty edits and
untracked files are not silently imported. Use direct file tools for those
files, or let an ordinary outside-change observation accept a stable version.
Rebuild to adopt a newer Git HEAD baseline.

After a publication or observed outside-change event, the worker reads that
event's accepted artifact. A later save is not relabeled as the older event's
content. Builds and queries exclude Git metadata, ignored and private paths such
as `.env`, pinned runtime, dependency trees, symlinks, binary/non-UTF-8 content and
recognized secret material. Search reapplies current source exclusions and
withholds results whose current source contains recognized secret material.

Builds are bounded to 20,000 Git entries, 1 MiB per file, 64 MiB per generation
and 30 seconds. Deterministic windows cover sixty lines with ten-line overlap;
UTF-8-safe pieces are at most 8 KiB and retain exact line citations. Search uses
FTS5/BM25 with exact-path preference and bounded, diversified results.

The baseline reads selected blobs through one Git batch process. Rebuilds select
only the latest accepted version per path in SQL, so the accepted-history cap
counts distinct paths rather than the number of past publications.

## Health and recovery

`loupe doctor`, `loupe db status` and `loupe index status` report index health.
An unavailable or unwritable knowledge store does not block sessions or source
publication. Publication/outside-event transactions record IDs in a control
outbox; the independent worker writes knowledge generations outside the
publication barrier. Background passes process at most 32 intents per checkout,
rotate through at most eight subscriptions and have a five-second pass deadline.
Ordinary passes replace only changed documents' chunks and FTS rows. Unchanged
chunks retain their IDs and rows; the active generation and event cursor advance
atomically, and citations name that active generation.

Exclusion-policy changes revalidate path metadata without re-chunking unchanged
source. A worker restart refreshes this metadata once. Checking for new ignore
rules still depends on the number of watched directories and policy files;
ordinary source updates do not reload every indexed document. Unexpected worker
exceptions appear in index health, and later polling continues normally.

A failed generation transaction preserves the prior verified generation and
cursor. Durable job failures remain visible after restart; interrupted jobs
return to pending. An interrupted initial build has no active baseline, so event
projection waits for an explicit build/rebuild rather than exposing an event-only
partial index.

Run `index rebuild` after an index failure. It verifies SQLite/FTS and can recreate
a corrupt owned regular derived store. It preserves symlinks, hard links,
directories, unrelated files, healthy busy databases and control data. Repairing
a damaged optional store removes all of its derived checkout indexes; rebuild
any other indexed checkouts named by the repair notice. Permission/path failures
need the underlying path repaired. A disabled subscription stays disabled even
if removing its derived rows fails.

To stop indexing, set `knowledge.index` to `off` and restart. Off creates no
optional knowledge/vector database, worker or projection intent; existing files
are preserved. `db status` and `db check` report absent optional stores without
creating them. `db backup` includes control and optional stores that already
exist, listing absent ones under `omitted`.

Agent retrieval tools and their live answer-quality comparison, code graphs,
embeddings and exact-vector scoring remain planned. See the
[implementation status](implementation-status.md) and
[retrieval ADR](adr/0005-vector-index.md) for that boundary.
