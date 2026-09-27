# Contributing

Thank you for helping build the local multi-agent coordinator. The repository is
currently an early foundation: architecture and safety invariants are being
turned into the first implementation. A documented command is not necessarily
available yet, and contributors should avoid filling planned package trees with
empty or speculative modules.

## Before making a change

1. Read the [implementation plan](docs/llm-cli-implementation-plan.md), especially
   its invariants, trust model, package boundaries, and phased rollout.
2. Read the relevant records under `docs/adr/`, beginning with
   [ADR 0001](docs/adr/0001-runtime-and-packaging.md).
3. Keep the change within one implementation phase or a clearly bounded
   prerequisite. Large cross-phase changes are difficult to validate and undo.
4. Treat coordination and publication correctness as safety properties. If a
   change can weaken a fence, scope check, or durable intent, describe the new
   failure states before writing code.

Changes that reverse an accepted architectural decision should add a superseding
ADR rather than silently changing the implementation.

## Supported development baseline

- macOS or Linux
- Python `>=3.12,<3.15`
- Git with linked-worktree support
- SQLite with FTS5 enabled
- `uv` for dependency locking and repeatable commands

The project must remain compatible with Python 3.12 even when developed on a
newer interpreter. Do not use newer-only APIs without a compatible local
implementation. In particular, a sortable request ID abstraction must not rely
solely on standard-library APIs introduced after Python 3.12.

Packaging and automated test commands are not yet established. When they land,
this guide will list the exact locked commands. Until then, do not represent an
ad hoc local command as the supported project workflow.

## Design invariants

Every contribution must preserve these constraints:

- Only the daemon writes authoritative coordination state.
- The CLI calls the daemon; it does not mutate coordination databases or target
  Git refs directly.
- Durable state is committed before a success response or external side effect
  is reported.
- Claim fencing tokens increase monotonically within a repository coordination
  key and stale holders cannot renew, publish, or integrate.
- Earlier overlapping waiters cannot be overtaken; disjoint tasks may progress.
- An editing task receives its own linked worktree.
- Claimed scope is a planning and scheduling boundary, while the actual Git
  change set is the trusted validation input.
- Publication intent is durable before any Git ref or remote side effect.
- A publishing or integration reservation is not released merely because a
  process lease or wall-clock deadline expired.
- Repository indexing cannot mutate claims or delay control-plane renewals.
- Vector and graph projections are optional and rebuildable; control authority
  never depends on loading a native vector extension.
- Provider SDK types do not leak into coordination, storage, or Git logic.
- Plugins receive narrow DTOs and capabilities, never raw database handles.

If a proposed implementation cannot maintain an invariant after a process is
killed at an arbitrary statement boundary, it is not ready to merge.

## Package boundaries

Follow the package layout in the implementation plan, but create modules only as
their behavior is implemented. Important dependency directions are:

- CLI commands depend on the RPC client, not SQLite or Git internals.
- Coordination does not import provider or retrieval implementations.
- Retrieval cannot mutate coordination state.
- Agent drivers use a bounded tool broker rather than integration adapters.
- Only trusted Git integration code mutates target refs, main worktrees, or
  remotes.

Core logic should use plain typed Python. Use Pydantic 2 at untrusted or persisted
DTO boundaries when validation value outweighs the dependency and conversion
cost. Avoid introducing CLI frameworks, ORMs, general agent frameworks, hosted
provider SDKs, or native extensions into the base package without an ADR.

## Testing expectations

Every behavioral change should include the smallest tests that prove both its
success path and its relevant failure paths. The planned test layers are:

- unit tests for normalization, overlap, state transitions, fencing, parsing,
  ranking, configuration, and redaction;
- property tests for algebraic scope and queue invariants;
- multiprocessing tests against a real daemon for concurrency behavior;
- kill-point tests around durable commits and external side effects;
- real Git fixtures for worktree, hash format, rename, dirty-tree, symlink,
  hook/config, and integration behavior;
- retrieval evaluations for correctness, freshness, privacy, and degraded mode;
- security tests for socket permissions, framing, paths, approvals, and prompt
  injection.

Prefer deterministic clocks, seeded identifiers, temporary local repositories,
and explicit time bounds. A mocked SQLite transaction or mocked Git diff is not
enough to establish cross-process or repository safety.

Tests that create repositories, state directories, sockets, or databases must
use dedicated temporary paths and must never clean a path derived only from an
unvalidated environment variable, glob, repository name, or model output.

## Code and documentation style

- Optimize for readable invariants and reviewable transactions over cleverness.
- Keep SQLite transaction boundaries and compare-and-swap predicates visible.
- Use explicit subprocess argument arrays and sanitized Git environments; do
  not build trusted Git commands through a shell string.
- Bound message sizes, event payloads, graph traversals, retrieval results,
  subprocess output, and retry loops.
- Return stable error codes at protocol boundaries and keep raw tracebacks out
  of normal CLI output.
- Document recovery behavior alongside each state transition or side effect.
- Update user-visible documentation when a planned command becomes real or its
  semantics change.

## Adding a dependency

Base dependencies have an unusually high bar because the coordinator must keep
working offline and during degraded provider/retrieval conditions. A dependency
change should explain:

1. why the standard library or an existing dependency is insufficient;
2. whether the dependency is in the control plane, optional data plane, or
   development toolchain;
3. supported Python, OS, and architecture coverage;
4. native code, dynamic loading, network, credential, and subprocess behavior;
5. locking, update, vulnerability, and removal strategy;
6. what remains functional when the dependency is unavailable.

Native SQLite extensions must never be loaded into `control.sqlite3`.

## Architecture decision records

ADRs live in `docs/adr/` and use a small structure: status, context, decision,
consequences, and alternatives. Accepted records are historical evidence. To
change one, add a new ADR that names and supersedes the old record, then update
the index. Minor clarifications that do not change the decision may update an
existing ADR.

## Security-sensitive changes

Read [SECURITY.md](SECURITY.md) before changing path handling, daemon identity,
socket framing, database opening, Git subprocesses, credentials, approvals,
plugin loading, cleanup, or publication. Do not include live secrets, private
repository content, or exploitable vulnerability details in tests or public
issues.

## Review checklist

- The change states which plan phase and invariant it advances.
- New state has a migration, ownership classification, and recovery behavior.
- New side effects have a durable intent and idempotent reconciliation path.
- CLI JSON output and error codes remain deterministic.
- Logs and events are bounded and redact secrets.
- Tests cover stale, duplicate, concurrent, and interrupted execution where
  relevant.
- Documentation describes only behavior that is actually implemented, with
  future behavior labeled as planned.
