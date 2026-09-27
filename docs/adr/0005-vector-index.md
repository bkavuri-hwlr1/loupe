# ADR 0005: Lexical retrieval plus built-in vector fallbacks

- **Status:** Accepted
- **Date:** 2026-08-22
- **Decision owners:** Initial project maintainers

## Context

Semantic retrieval can improve agent context, but it must not become a
prerequisite for local coordination or exact code search. Native vector
extensions have platform, architecture, Python, SQLite ABI, dynamic-loading,
corruption, deletion, migration, and packaging risks. A new project cannot make
its safety-critical control plane depend on one before those paths are tested.

The product also needs deterministic retrieval tests that do not require a
hosted service. Small repositories and evaluation corpora can use exact cosine
scoring without an approximate index. Larger local indexes can adopt an
accelerator later while retaining the same source identities and degraded modes.

## Decision

SQLite FTS5/BM25 is the mandatory lexical retrieval arm. Lexical search works
without a model, embedding provider, native extension, or network connection.
Exact path, filename, symbol, and identifier resolution precedes or complements
ranked retrieval where applicable.

The base vector interface must support two built-in modes:

- `none`: do not build or query embeddings; operate lexical-only;
- `exact`: store normalized embedding vectors as compact typed blobs with model
  metadata and perform bounded exact cosine scoring in application code.

Both modes are required behavior. `none` is the universal degraded mode and
`exact` is the reference semantic implementation for small indexes, tests, and
backend comparisons. Exact search applies configured candidate, dimension,
memory, and latency bounds and refuses incompatible vectors.

`sqlite-vec` is an optional, opt-in accelerator pending supported-platform
qualification. It cannot become a default until a pinned reviewed release passes:

- macOS/Linux and supported architecture packaging;
- supported Python and SQLite combinations;
- extension origin, checksum, and loading controls;
- create, query, update, delete, vacuum/checkpoint, and rebuild behavior;
- dimension/model migration and incompatible-schema handling;
- malformed/corrupt database and interrupted-write recovery;
- deterministic comparison against exact scoring;
- backup omission and full projection regeneration;
- concurrency with index updates and search readers.

### Isolation and authority

Vector state lives only in `vectors.sqlite3` and is disposable. A native extension
is never loaded into `control.sqlite3`. The extension-loading window is narrowly
scoped to a validated vector connection and disabled immediately afterward.

`knowledge.sqlite3` owns canonical source, document, chunk, revision, content
hash, privacy profile, and graph provenance. Vector rows refer to stable chunk
identities and record embedding provider, model, dimensions, normalization,
content hash, and index generation. A model or dimension change creates a new
generation rather than silently reinterpreting old bytes.

### Provider independence

Embedding providers implement a narrow interface separate from chat/tool model
providers. Retrieval and coordination code do not import provider SDK types.
Remote embedding is never silently selected for local repository content; the
transmission policy and provider must be explicit.

The initial retrieval flow may combine lexical and semantic rankings with
reciprocal-rank fusion and bounded diversification, but it must always expose
which arms ran, their generations, and why another arm degraded. Vector outage
does not turn a valid lexical query into a coordination or daemon failure.

### Rebuild and evaluation

All vector projections are reproducible from canonical chunks plus an available
embedding provider/model. Rebuild uses generation staging and an atomic metadata
switch so queries do not mix incompatible generations.

A versioned local evaluation corpus covers semantic descriptions, exact symbols,
stale revisions, privacy boundaries, prompt injection, diversity, and vector
outage. Exact scoring is the correctness reference for optional accelerators.

## Consequences

### Positive

- The CLI remains useful offline and without embeddings.
- Exact scoring provides a simple, portable reference implementation.
- Native extension risk is isolated from authority and made opt-in.
- Provider/model migrations are explicit and reproducible.
- Optional accelerators can be evaluated without changing agent-facing search
  contracts.

### Costs and constraints

- Exact scoring is unsuitable for unbounded large indexes and must enforce
  candidate/latency limits.
- Maintaining multiple backends requires conformance tests.
- Vector storage duplicates some derived data by design.
- Semantic retrieval may be unavailable until a user configures an embedding
  provider and builds a compatible generation.
- `sqlite-vec` cannot be advertised as supported merely because a local demo
  succeeds.

## Alternatives considered

- **Require `sqlite-vec`:** rejected until packaging, recovery, and migration
  behavior pass the product matrix.
- **Use a remote vector database:** rejected as a base dependency because it
  breaks local/offline operation and complicates privacy.
- **Store vectors in the knowledge database:** rejected because native extension
  and rebuild churn should have a separate failure domain.
- **Semantic-only retrieval:** rejected because exact identifiers and degraded
  local operation require lexical search.
- **No vector interface until later:** rejected because defining source and
  generation metadata early prevents the lexical schema from baking in an
  incompatible identity model.

## Follow-up

- Establish exact-blob representation and cosine conformance fixtures.
- Pin and qualify a specific `sqlite-vec` release before enabling its feature
  gate outside development.
- Define index-size thresholds that recommend or require bounded candidate
  selection rather than full exact scans.
