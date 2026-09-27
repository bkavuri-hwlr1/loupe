# ADR 0001: Runtime, packaging, platform, and command names

- **Status:** Accepted
- **Date:** 2026-08-22
- **Decision owners:** Initial project maintainers

## Context

The coordinator combines a local CLI, a long-lived per-user daemon, SQLite,
Git subprocesses, model-provider adapters, and optional retrieval acceleration.
The first implementation needs a runtime that supports reliable local process
orchestration and is readily available on developer machines without making the
control plane depend on a large framework.

The implementation plan uses `llm` and `llmd` as readable placeholders. Those
names cannot safely become the initial public interface without collision work:
`llm` is already an established CLI/distribution name, and `llmctl` is also
occupied. A foundation repository should not claim either command, create a
surprising shadow in user `PATH`, or couple its Python import name to a final
brand decision.

The first release also needs a bounded platform matrix. Unix domain sockets,
permissions, process signals, filesystem identification, and Git linked
worktrees have materially different Windows behavior. Treating Windows as
implicitly supported would leave critical authority and recovery paths untested.

## Decision

### Distribution and command names

Use the following temporary names:

- Python distribution: `llm-coord`
- user CLI command: `llm-coord`
- private daemon command: `llm-coordd`
- Python import package: `llm_cli`

The daemon name is exposed for diagnostics and service management but is not the
normal user entry point. The CLI may start it automatically once that lifecycle
is implemented.

These names are explicitly provisional. A future naming ADR may supersede them
before a public release. We will not install compatibility aliases named `llm`,
`llmd`, or `llmctl` by default.

### Supported platforms

The first supported platforms are macOS and Linux. Each released platform and
architecture must pass the daemon, SQLite, Git worktree, concurrency, crash, and
packaging matrices relevant to its code path.

Windows is deferred until the project specifies and tests:

- named-pipe framing, permissions, peer identity, and singleton behavior;
- process lifetime, signal, and detached-daemon semantics;
- runtime/state directory selection and security checks;
- linked-worktree and cleanup behavior;
- equivalent atomic filesystem and locking guarantees.

Platform-neutral interfaces should not preclude a Windows implementation, but
untested Windows code is not advertised as supported.

### Python and dependency management

Support Python `>=3.12,<3.15`. Python 3.12 is the compatibility floor; code
developed on Python 3.13 or 3.14 must not depend on newer-only behavior without a
local compatibility implementation and tests.

Use `uv` for environment creation, lockfile generation, development commands,
and eventual tool installation. Commit the resolved lockfile when packaging is
bootstrapped. CI must exercise the lowest supported Python version and the newest
supported version even if developer machines use only one of them.

The build backend will be recorded in project packaging when its offline and
supported-platform behavior can be verified. This ADR does not require a build
backend package to become a runtime dependency.

### Base implementation style

Use:

- standard-library `argparse` for the CLI;
- standard-library `asyncio` for socket, subprocess, event, and scheduler
  orchestration;
- standard-library `sqlite3` with explicit SQL and transaction boundaries;
- standard-library `pathlib`, `tomllib`, and narrowly scoped OS abstractions;
- plain typed dataclasses or equivalent domain objects for internal logic.

Pydantic 2 may be introduced for RPC envelopes, plugin/provider DTOs,
configuration boundaries, and persisted payloads when concrete validation needs
justify it. Pydantic models do not become the core domain or database abstraction
by default.

Do not add a CLI framework, ORM, general agent framework, hosted-provider SDK,
or native extension to the base package solely for convenience. Provider and
embedding dependencies belong behind adapters and optional dependency groups.

## Consequences

### Positive

- The control plane remains small, inspectable, and usable without a model
  provider or retrieval extension.
- Explicit Python bounds make compatibility review and CI expectations clear.
- macOS/Linux scope matches the first transport and permissions model.
- Temporary commands avoid shadowing two occupied ecosystem names.
- Delaying Pydantic until boundary DTOs exist avoids speculative schemas and an
  unnecessary bootstrap dependency.

### Costs and constraints

- `argparse` requires deliberate help, completion, and output design that a CLI
  framework might otherwise supply.
- Platform path and permissions behavior must be implemented and tested locally.
- Python 3.12 compatibility forbids relying solely on newer standard-library
  conveniences, including newer UUID helpers.
- Users cannot assume Windows support.
- Command names and documentation may require a controlled migration after a
  final product name is chosen.

## Alternatives considered

- **Install `llm` or `llmctl` anyway:** rejected because collision and user
  surprise outweigh the shorter spelling.
- **Support Windows immediately:** rejected because it would expand the critical
  transport, permissions, process, and worktree matrix before the Unix safety
  model is proven.
- **Use Typer/Click from the start:** deferred; neither is needed for the first
  health and coordination commands.
- **Use an ORM:** rejected for authority tables because transaction and fencing
  predicates must remain visible in review.
- **Use a general agent framework:** rejected for the first release because
  provider abstraction is small and coordination correctness must not inherit a
  framework lifecycle.
- **Require Pydantic throughout:** rejected; boundary validation is valuable,
  but persistence and domain state should not be coupled to one model library.

## Follow-up

- Record the actual build backend, lock strategy, and supported release matrix
  when the Python scaffold lands.
- Research a durable public product name before publishing packages.
- Add a future ADR before widening the supported OS set or Python bounds.
