# ADR 0002: Per-user daemon and local RPC protocol

- **Status:** Accepted
- **Date:** 2026-08-22
- **Decision owners:** Initial project maintainers

## Context

Claims, queue order, fencing tokens, publication intents, and reconciliation
must have one local authority. Allowing every CLI or agent process to open a
write connection independently would make ownership, scheduling, migrations,
and crash recovery harder to reason about. It would also expose coordination
latency to indexing and plugin behavior.

The CLI still needs a local protocol that supports ordinary request/response
operations, idempotent retries, durable event replay, and eventually streaming
task output. The protocol must reject malformed and oversized input before it
allocates unbounded memory, and it must distinguish an old process reusing a PID
from the actual daemon boot instance.

## Decision

Run one per-user, per-profile daemon named `llm-coordd`. The daemon is the sole
writer to authoritative coordination state and the only component allowed to
perform trusted publication or integration operations. `llm-coord` is a client:
it resolves configuration, connects or starts the daemon, sends a request, and
formats the response.

### Transport

On macOS and Linux, use an owner-only Unix domain socket in a validated runtime
directory. The directory uses mode `0700` and the socket is accessible only to
the owner. Startup rejects unsafe ownership, excessive permissions, and symlinked
security-sensitive path components.

Keep a transport interface so Windows can later provide a named-pipe
implementation. TCP loopback is not the default because it introduces port,
firewall, and authentication concerns without a local product benefit.

### Framing and encoding

Every non-streaming RPC frame is:

1. a four-byte unsigned big-endian payload length;
2. exactly that many bytes of UTF-8 JSON.

The protocol rejects a zero or configured-maximum-exceeding length, truncated
payload, invalid UTF-8, duplicate or unexpected envelope fields where the schema
forbids them, invalid JSON, excessive nesting, and unsupported protocol version.
Size and parsing bounds are defined before public release and applied before
dispatch.

The first protocol version uses request envelopes containing at least:

- protocol version;
- request ID;
- method;
- validated parameters;
- optional idempotency key;
- client version;
- profile ID.

Responses contain the same request ID, success flag, a typed result or stable
error code, and a daemon revision where relevant. Normal responses never expose
a raw exception traceback. Unknown methods and fields receive stable protocol
errors rather than falling through to dynamic dispatch.

Event streaming uses a separately specified framed message kind with monotonic
event sequence values and bounded payloads. Event data is committed to the
control database before the daemon notifies connected clients, so reconnect and
replay do not depend on an in-memory queue.

### Lifecycle and identity

The daemon holds an exclusive startup lock for its state directory. A second
daemon exits rather than opening another authoritative writer. The daemon writes
a runtime record containing its PID, protocol and executable versions, profile,
boot ID, and process start identity. Clients do not trust a PID by itself.

The first state-requiring client:

1. attempts a connection and negotiates the protocol;
2. acquires or waits on the bounded startup lock when the socket is absent;
3. starts `llm-coordd` through an explicit argument array;
4. waits for a bounded readiness handshake;
5. reports a stable sanitized error and log location on failure.

Daemon state survives client disconnect. The first interrupt detaches from a
running task; cancellation is a distinct explicit request.

### Concurrency model

Use `asyncio` for socket I/O, subprocess communication, scheduling wakeups, and
bounded event delivery. SQLite writes remain short, explicit transactions on the
daemon's authority path. Blocking SQLite or filesystem work runs in bounded
workers only when needed; transaction ownership must never cross an `await` in a
way that permits accidental interleaving.

All retryable authority mutations have an idempotency strategy. A client timeout
means the outcome is unknown until queried; it never authorizes blind repetition
of a Git side effect.

## Consequences

### Positive

- One writer makes fencing, migrations, event order, and reconciliation easier
  to establish and test.
- Length-prefix framing is simple, streaming-safe, and independent of newline
  content inside JSON strings.
- The protocol is inspectable without a generated RPC framework.
- Durable event replay decouples task lifetime from terminal lifetime.
- A local UDS avoids opening a network listener.

### Costs and constraints

- The daemon needs robust singleton, readiness, upgrade, and stale-socket logic.
- Schema evolution requires explicit protocol version negotiation.
- JSON is not the most compact encoding and binary artifacts must be referenced
  by bounded metadata rather than embedded without limits.
- Same-user socket permissions are not a hostile-process security boundary.
- Framing, cancellation, backpressure, and partial-read behavior require dedicated
  tests.

## Alternatives considered

- **Direct database access from the CLI:** rejected because it creates multiple
  authority writers and leaks migrations/transactions into presentation code.
- **HTTP over TCP loopback:** rejected for the first release because a network
  stack and port lifecycle add no needed capability.
- **Newline-delimited JSON for every message:** rejected as the primary RPC
  framing because exact length bounds and partial reads are simpler with an
  explicit prefix. A bounded NDJSON-like event representation may exist inside
  the versioned stream message contract.
- **gRPC or another generated framework:** deferred; it adds packaging and
  compatibility costs before the local protocol surface is known.
- **One daemon per repository:** rejected because cross-clone coordination,
  provider limits, and shared knowledge need a per-profile scheduler.

## Follow-up

- Specify initial maximum frame size, nesting limits, timeouts, and stable error
  mapping in protocol tests and documentation.
- Add fuzz/property tests for split reads, combined frames, invalid lengths,
  invalid UTF-8, unknown versions, cancellation, and backpressure.
- Record the named-pipe transport in a new ADR before Windows support.
