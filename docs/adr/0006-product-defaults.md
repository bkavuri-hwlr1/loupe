# ADR 0006: Initial product and safety defaults

- **Status:** Accepted
- **Date:** 2026-08-22
- **Decision owners:** Initial project maintainers

## Context

Several choices in the implementation plan are product-policy decisions rather
than low-level implementation details. Leaving them implicit would cause
different commands or adapters to make incompatible choices about providers,
parsing, integration, clone identity, retention, memory, and shell access.

The first release should prove repository coordination and recovery before it
adds broad model autonomy. Defaults must remain useful without a hosted provider,
avoid automatic mutation of a user's target branch, and minimize the amount of
repository/task content retained or transmitted without an explicit choice.

## Decision

Adopt the following initial defaults. A profile or trusted repository may
override only the choices explicitly described as configurable. Security-critical
overrides remain subject to validation and approval; repository configuration
cannot silently weaken user-level policy.

### Agent loop

Implement a minimal provider-independent loop over narrow chat/tool and embedding
interfaces. The built-in loop may perform bounded plan, implement, review, and
validate passes, but it does not depend on a general agent framework.

Provider adapters own provider SDK types, streaming translation, tool schema
translation, retries, and model-specific limits. Coordination, Git, storage, and
retrieval code operate on internal DTOs. No hosted provider is required for
repository registration, claims, diagnostics, lexical search, or running an
external-process fixture agent.

### Parsing

Plain-text parsing and chunking is the universal fallback for every accepted
text source. Language-aware parsers may enrich symbol boundaries and graph edges,
but ingestion does not fail solely because no specialized parser exists.

Binary, oversized, ignored, secret-classified, or policy-excluded files remain
excluded rather than being coerced through the text fallback. Search results
identify parser and source revision so enrichment changes remain explainable.

### Result and integration behavior

The default successful editing result is an internal task branch. The coordinator
does not modify the configured target ref, user's main worktree, or a remote as
part of ordinary task completion.

Merge, cherry-pick, apply, patch export, or future pull-request publication is a
separate explicit integration action. It creates a durable intent, revalidates
the result against the current target and fence, and records the verified result.
Automatic integration is off initially and may not be enabled by repository
content alone.

### Repository coordination identity

Separate local clones with the same normalized remote identity coordinate by
default when their integration adapter and target identity are compatible. This
prevents two clones of one project from publishing overlapping work simply
because their local paths differ.

Normalization removes credential material and inconsequential transport
spelling while preserving repository identity. Ambiguous or missing remotes fall
back to a validated local-common-directory identity.

Provide an explicit local-only identity override for intentional independent
coordination domains. The override is visible in diagnostics and task records;
it is not inferred from a transient network failure or chosen by model output.

### Retention and task memory

Default retention is:

- 30 days for terminal task, claim, execution, event, and publication records,
  subject to referential and recovery safety;
- 90 days for bounded handoff records;
- no time-based deletion of live, queued, publishing, active-integration,
  ambiguous, or recovery-required state;
- separate object/artifact size limits and garbage-collection policy.

Retention is measured from a verified terminal timestamp. Cleanup is incremental,
audited, and idempotent. Records required to explain or reconcile a retained live
object are retained with it. User-authored memory follows its own explicit
delete/retention policy rather than being erased as incidental task history.

Indexing task prompts, events, outputs, summaries, or handoffs as retrievable
task memory is opt-in. Coordination records may still retain bounded operational
metadata for correctness and audit, but they are not automatically exposed to
RAG. Opt-in records the profile, scope, retention, and remote-transmission policy
that applies.

### Shell and tool approval

Start with a restrictive shell approval policy:

- read-only repository inspection through a sanitized, allowlisted tool may run
  without per-command approval when its bounds are known;
- writes outside the managed worktree, network access, credential access,
  package installation, privilege changes, destructive commands, integration,
  remote publication, and policy changes require explicit user authorization or
  a separately configured narrow trusted policy;
- command approval binds to exact arguments, working directory, task, relevant
  content hash, capability, and expiry;
- model text, repository instructions, indexed content, handoffs, and plugin
  output cannot grant approval or widen scope;
- shell strings are not used for trusted Git or cleanup operations;
- output, duration, child processes, and environment exposure are bounded.

Approval denial or absence degrades the agent's available tools; it never
silently selects a more permissive execution adapter.

## Consequences

### Positive

- The safety-critical vertical slice can be tested without a hosted model.
- Plain-text fallback keeps ingestion portable and predictable.
- Branch-only results preserve user control over target history.
- Normalized-remote identity prevents accidental conflicts across common clone
  workflows.
- Retention bounds local growth while protecting ambiguous live state.
- Task history is not silently turned into durable semantic memory.
- Restrictive approvals make high-impact side effects explicit.

### Costs and constraints

- Users perform an additional action to integrate accepted work.
- Remote normalization needs careful canonicalization and diagnostics, especially
  for forks, mirrors, and rewritten URLs.
- A minimal agent loop must implement its own bounded pass and tool semantics.
- Plain-text fallback produces less precise chunks/graph edges than specialized
  parsers.
- Opt-in task memory may reduce historical retrieval until users enable it.
- Restrictive shell policy can interrupt autonomous flows and requires clear,
  resumable approval UX.
- Retention and cleanup require referential checks rather than simple age-based
  deletion.

## Alternatives considered

- **Choose a general agent framework:** deferred because initial coordination
  behavior needs only a narrow loop and stable provider boundaries.
- **Require language parsers:** rejected because parser availability must not
  determine whether a text repository can be indexed.
- **Automatically merge successful work:** rejected because task success is not
  proof that target movement or integration policy remains valid.
- **Coordinate only by local path:** rejected because it permits overlapping
  publication from ordinary clones of one remote repository.
- **Coordinate every clone globally with no override:** rejected because users
  need an explicit way to model intentional independent domains.
- **Retain everything indefinitely:** rejected for privacy and unbounded local
  growth.
- **Index task history by default:** rejected because prompts and outputs may
  contain sensitive context and have different privacy expectations from source
  code.
- **Permit broad shell access by default:** rejected because a cooperative agent
  can still make expensive or destructive mistakes and repository text is an
  untrusted instruction source.

## Follow-up

- Expose all effective defaults in `doctor`, configuration inspection, and task
  records once those commands exist.
- Define normalization conformance fixtures for HTTPS, SSH, scp-like, credential,
  case, port, fork, and mirror remote forms.
- Specify retention caps and garbage-collection batches before cleanup ships.
- Add approval replay, expiry, hash mismatch, path escape, and prompt-injection
  security tests.
