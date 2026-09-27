# ADR 0007: The product is a coordinated coding-agent harness

Status: accepted

## Context

The early implementation could be read as a coordination service that happens
to invoke Claude. That is not the intended product. The intended experience is
a general terminal coding agent, similar in shape to Pi, whose independently
started local instances coordinate automatically when they operate on the same
repository.

This distinction affects durable identity. A model provider, a coding-agent
harness, a top-level terminal session, and a coordination task are different
things. Treating any two of them as one makes provider replacement, multi-prompt
conversation, or cross-terminal coordination unnecessarily model-specific.

## Decision

1. The built-in agent loop is the provider-neutral `coding_agent` harness.
   Anthropic is one adapter selected by the durable launch recipe; it is not the
   harness identity.
2. Provider adapters implement a neutral stateful chat protocol and own their
   native message serialization. A durable checkpoint binds that state to the
   adapter and model that created it.
3. Driver construction and resumability are resolved through registries. The
   daemon and recovery code do not recognize Claude or any future provider by
   name.
4. A top-level CLI chat is a durable agent session. Its model conversation will
   span user prompts. Coordination tasks and publication attempts are
   shorter-lived children of that session; they must not hold write authority
   while the user is thinking.
5. Multiple terminal instances discover one another through the owner-only
   daemon and checkout-scoped durable state. The shared/isolated workspace and
   deterministic change-batch design is governed by the superseding
   multi-session workspace-modes plan.

## Consequences

- Additional providers can be added without changing the harness loop,
  execution lifecycle, or recovery algorithm.
- Provider and model selection must be explicit durable launch data so a daemon
  restart cannot silently resume through a different backend.
- The current per-prompt `chat` implementation is transitional. Durable
  top-level sessions and the shared/isolated workspace ledger precede further
  expansion of autonomous execution passes.
- Coordination remains same-host and cooperative. This ADR does not make model
  or plugin code a security boundary.
