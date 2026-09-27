# ADR 0003: Split SQLite authority, knowledge, and vector storage

- **Status:** Accepted
- **Date:** 2026-08-22
- **Decision owners:** Initial project maintainers

## Context

The coordinator has three storage workloads with different correctness and
rebuildability properties:

- fencing, queues, task state, and publication intents are safety-critical;
- documents, lexical search, graph facts, and user memory mix authoritative and
  rebuildable data;
- vectors are model/version-specific projections that can be large and replaced.

Putting these workloads in one SQLite file would let bulk indexing, vector
migration, extension loading, or a long search reader interfere with claim
renewal and authoritative checkpoints. Treating every file as equally disposable
would risk user memory; treating every projection as authority would make backup
and migration unnecessarily fragile.

## Decision

Use three SQLite database files under the per-user profile data directory.

| Database | Contents | Authority | Initial durability |
| --- | --- | --- | --- |
| `control.sqlite3` | repositories, tasks, claims, scopes, blockers, fences, delegations, worktrees, executions, publication intents, handoffs, events, approvals, and artifact metadata | authoritative | WAL and `synchronous=FULL` |
| `knowledge.sqlite3` | sources, documents, chunks, FTS projection, graph facts/projection, and user-authored memory | mixed: memory authoritative; repository-derived content rebuildable | WAL and `synchronous=NORMAL` |
| `vectors.sqlite3` | embeddings, vector-backend metadata, and vector projection | disposable and rebuildable | WAL and rebuildable |

The daemon owns writes. Control-plane transactions do not attach or span the
knowledge or vector database. A knowledge/vector failure cannot roll back,
delay, or weaken a claim transition. Cross-database convergence uses durable
generation identifiers and idempotent jobs rather than multi-database atomicity.

### Control database

`control.sqlite3` is the source of truth for who may work or publish. Its schema
uses explicit migrations with ordered versions and immutable checksums. Fencing
updates use compare-and-swap predicates visible in SQL. Success is reported only
after commit. Startup performs bounded integrity, schema, ownership, and pragma
checks before accepting state-changing RPCs.

No third-party SQLite extension is loaded into the control connection. Dynamic
extension loading is disabled unless a narrowly scoped vector connection
explicitly enables and immediately re-disables it.

### Knowledge database

`knowledge.sqlite3` stores source and chunk identity, revision provenance,
lexical FTS5 data, graph facts, and user memory. Repository-derived data is
regenerable from its source and generation metadata. User-authored memory is
classified as authoritative and included in compatible backup/restore.

Index generation swaps are transactional within this database. Search results
must identify the source revision/generation used and never cross the requested
repository or privacy profile.

### Vector database

`vectors.sqlite3` is a replaceable projection. Its metadata binds embeddings to
the source chunk identity, content hash, embedding provider/model, dimensions,
normalization, and index generation. Incompatible or corrupt vector state is
quarantined/rebuilt rather than trusted.

Optional native vector extensions are restricted to this database. Coordination
and lexical retrieval continue when it is absent.

### Connection and filesystem policy

- Use direct standard-library `sqlite3` access and explicit transactions.
- Set and verify required pragmas on every connection; do not assume file-level
  persistence of all settings.
- Keep write transactions short and use bounded busy timeouts.
- State must be on a supported local filesystem. `doctor` rejects or strongly
  warns about network filesystems because SQLite WAL is a same-host design.
- Data directories use owner-only permissions, reject unsafe symlink components,
  and are not placed inside a managed repository by default.
- Backups use SQLite's backup API after a bounded checkpoint; copying the main
  file alone while WAL exists is invalid.
- Backup manifests classify which data is authoritative, derived, omitted, and
  compatible with the restoring version.

## Consequences

### Positive

- Claim renewal and recovery are isolated from indexing and vector workloads.
- Native vector experiments cannot load code into coordination authority.
- Disposable projections can be rebuilt or migrated independently.
- Backup policy can preserve user memory without treating every repository
  embedding as irreplaceable.
- SQL transactions and fencing predicates remain auditable.

### Costs and constraints

- Cross-database updates are eventually consistent and need explicit generations,
  outbox jobs, and reconciliation.
- Operations and diagnostics must check three files, not one.
- Search may temporarily degrade to lexical-only or unavailable projections while
  a rebuild completes.
- Separate migrations and backup manifests add implementation work.
- SQLite remains single-writer per database and unsuitable as shared network
  authority.

## Alternatives considered

- **One SQLite database:** rejected because indexing/native-extension failures
  would share a failure and contention domain with fencing authority.
- **One database per repository:** rejected for control authority because tasks
  may coordinate normalized clones and profiles need global scheduling/events.
- **An ORM:** rejected for authoritative coordination because transaction modes,
  query plans, and compare-and-swap predicates must stay explicit.
- **A client/server database:** deferred; it conflicts with the first release's
  local-first, single-user, offline operation and adds operational burden.
- **Treat all knowledge as disposable:** rejected because user-authored memory is
  not necessarily reconstructible.

## Follow-up

- Define migration checksum and downgrade policy before the first persisted
  schema is released.
- Add pragma, corruption, checkpoint, backup, restore, and kill-point tests.
- Add generation/outbox reconciliation before cross-database indexing is enabled.
