# Security policy

## Project maturity

This repository is an early implementation foundation, not a completed security
boundary or production-ready agent coordinator. The architecture contains
security requirements, but most enforcement and adversarial tests are still to
be implemented. Do not rely on the project to isolate untrusted code, protect a
host from a malicious model tool call, or safely coordinate valuable repositories
until the relevant controls are implemented and verified.

## Reporting a vulnerability

Report suspected vulnerabilities through GitHub's private "Report a vulnerability"
form under this repository's Security tab.
Do not file a public issue containing credentials, private repository material,
working exploit instructions, or a vulnerability that could damage user data.

Include, when possible:

- the affected revision and platform;
- the trust boundary or invariant that is bypassed;
- minimal reproduction steps using disposable data;
- expected and observed behavior;
- likely impact, including whether Git refs, files, credentials, or coordination
  authority can be changed;
- any safe containment or workaround.

Receipt and remediation timelines are not yet guaranteed. Private reports are handled as repository security advisories.

## Threat model

The initial product supports cooperative agents and subprocesses running as the
same local OS user. It aims to prevent accidental concurrency conflicts, stale
publication, unsafe path handling, and unintended integration. It does not
provide strong isolation from a malicious same-user process.

In scope for defensive design:

- malformed or oversized local RPC frames;
- stale, duplicated, reordered, or forged requests from a same-user client;
- process crashes and machine restarts at transaction/side-effect boundaries;
- symlink, hardlink, path traversal, and unsafe cleanup targets;
- malicious Git configuration, hooks, aliases, filters, pagers, diff drivers,
  attributes, repository contents, and surprising worktree state;
- prompt injection and hostile text in indexed repositories or handoffs;
- accidental secret ingestion, logging, or provider transmission;
- dependency, plugin, model-provider, embedding-provider, and native-extension
  failures;
- stale agents attempting to renew, publish, or integrate;
- database replacement, incompatible migration, corruption, and partial backup;
- commands that exceed their declared scope or approval.

Not provided by the initial architecture:

- an OS, container, VM, or hostile-code sandbox for Loupe itself or configured
  checks (model-chosen commands do run in an OS sandbox; see "Model-chosen
  commands" below);
- protection from an administrator, root process, or malicious process with the
  same account and unrestricted filesystem/debugging access;
- multi-host coordination over a network filesystem;
- Windows named-pipe or worktree support in the first release;
- automatic proof that arbitrary generated code is safe or correct;
- safe automatic integration of every agent result.

Agents that require hostile-code isolation must run behind a separately managed
sandbox or VM boundary.

## Security invariants

- The per-user daemon is the sole writer of authoritative coordination state.
- Runtime directories are owner-only (`0700` on Unix); sockets and database
  files are owner-only (`0600`). Sensitive path components are rejected if they
  are symlinks.
- RPC uses a versioned, bounded, four-byte length-prefixed JSON protocol over an
  owner-only Unix domain socket. Peer identity and daemon boot identity are
  checked; a PID alone is not trusted as daemon identity.
- Every authority-changing request is validated and idempotent where retry is
  possible.
- Claims use monotonic fencing. A stale holder cannot renew, publish, integrate,
  or widen scope.
- Git changed paths and object identities are recomputed by trusted code before
  publication; model-reported files are never authoritative.
- Durable publication intent precedes a Git ref, patch, or remote side effect.
- Default results are branch-only. Target-branch integration requires a separate
  explicit operation and fresh validation.
- The control database never loads third-party SQLite extensions.
- Vector and graph projections are non-authoritative and replaceable.
- Project configuration cannot set secrets, the authority path, plugin search
  paths, or unsafe shell policy unless it passes an explicit trust mechanism.
- Cleanup operates only on validated application-owned identifiers and paths.

## Secrets and privacy

Secrets must not be stored in project configuration, prompts, task events,
handoffs, logs, patches, indexes, or graph properties. Provider credentials
belong in the OS credential facility or an explicitly supported secure source;
only short-lived resolved values should enter process memory.

Repository ingestion must honor ignore and exclusion policy, reject oversized or
binary content by default, and apply secret classification before persistence or
remote transmission. Task-memory indexing is opt-in. A user selecting a hosted
model or embedding provider must be shown which repository content may leave the
machine.

Redaction is defense in depth, not permission to ingest secrets. Logs should use
stable identifiers and bounded metadata instead of prompts, source contents,
environment dumps, URLs with credentials, or raw subprocess output.

Model source tools apply repository ignore rules and the user's configured
`core.excludesFile` to tracked and untracked paths. User exclusions are evaluated
independently so repository negations cannot override them. Common credential
files, private-key files, and credential directories are excluded from both
discovery and explicit reads. The same exclusions apply to model file mutations,
including both source and destination paths of a rename. Recognized private-key
and provider-token formats are screened before source observations or tool
results are retained or sent to the model. This conservative pattern screening
is not a complete secret scanner.
Saved direct-file reads recheck current exclusions, but older aggregate search
and diff results lack reliable per-file provenance. Changing exclusions does not
erase already stored conversation content; start a new conversation when
tightening privacy rules. Finalization screens known secret formats but does not
have worktree context to recheck changed path exclusions.

Regular-expression searches run in an isolated Python child process with a
five-second deadline and cancellation. Git exclusion checks share that deadline;
regex matching runs after releasing the shared publication lock.

## Git and subprocess safety

Trusted Git operations must:

- use argument arrays without a shell;
- set a controlled environment and disable hooks, pagers, interactive prompts,
  unsafe filters, and user-controlled helpers where possible;
- separate repository inspection from mutation;
- validate repository identity, worktree ownership, target refs, and object IDs;
- account for SHA-1 and SHA-256 repositories, symlinks, submodules, nested repos,
  dirty state, and target movement;
- avoid following application-managed paths through symlinks;
- retain a recoverable publication or integration record before mutation.

Trusted Git operations use an allowlisted environment without provider tokens,
disable external diff/text-conversion helpers, and refuse active
repository-configured clean, smudge, or process filters. Repositories requiring
these filters (including Git LFS filters) are currently unsupported for operations
that inspect or populate worktree content. The daemon must be installed in its
Python environment (including an editable install); startup ignores repository
imports, `PYTHONPATH`, and user-site packages and runs from the private runtime
directory.

General shell execution starts with a restrictive approval policy. Model output
cannot approve a command, widen a scope, choose a cleanup root, expose credentials,
or authorize integration. Approval records must bind to the exact operation,
arguments, working directory, relevant content hash, task, and expiry.

### Model-chosen commands

The `run_command` tool runs an argv list the model chooses. It is offered only
in shared-workspace normal and auto tasks, only where an operating-system
sandbox starts successfully (Seatbelt through `sandbox-exec` on macOS,
bubblewrap on Linux), and never unsandboxed. Each command runs in a disposable
copy of the checkout with the task's pending edits applied:

- the network is off; only Unix sockets inside the command's own copy and home
  can be used, so host sockets such as SSH agents and Docker are unreachable;
- writes are limited to the copy and a private home, both deleted afterwards;
- the real checkout, Loupe's configuration, data, state, and runtime
  directories, and common credential stores under the home directory (SSH,
  GnuPG, cloud CLIs, `gh`, Docker, `.netrc`, package-registry tokens, Codex and
  Claude credentials, keychains, browser profiles) are unreadable;
- ignored dependency folders (`.venv`, `venv`, `node_modules`, and configured
  `runtime_paths`) are readable in place but not writable;
- the environment carries only `PATH` and fixed, non-secret settings;
- arguments containing recognized secret material are refused, and output after
  recognized secret material is withheld;
- each command has a timeout (at most 600 seconds) and stops with its task.

Other files on disk remain readable, so this is weaker than a read allowlist:
a command could read a credential stored somewhere this policy does not list,
and the pattern screening of its output is not a complete secret scanner.

Approval follows `agent.commands` in the user configuration. The default,
`ask`, offers commands only in interactive sessions and asks the user before
each one, showing its exact arguments, directory, and timeout; the user may
allow it once, allow commands for the rest of that task, or decline. The
approval is answered only through the question channel, never by model
output, and a task-wide approval expires with the task. `allow` runs sandboxed
commands without asking, and `off` never offers them.

## Plugins and optional native code

External plugins are disabled by default. User-installed plugins must declare
capabilities and receive narrow services and DTOs rather than database handles,
raw credential stores, or arbitrary integration access. Repository-provided
plugins are untrusted until explicitly approved.

`sqlite-vec` is an optional vector acceleration path pending supported-platform
packaging, deletion, migration, corruption, and recovery tests. Built-in lexical
and exact-vector modes must continue to work without it. A native vector
extension is loaded only into the disposable vector database, never into the
control database.

## Backups and recovery

Authoritative SQLite backups use the SQLite backup API after a bounded checkpoint.
Copying only the main database file while a WAL exists is not a valid backup.
Restore validates manifests, checksums, schemas, and permissions and preserves a
recoverable copy of current authority before replacement.

Shared publication preserves filesystem access permissions separately from Git
executable modes, including through rename, recovery, and undo. New files and
directories default to owner-only access, subject to the process umask. Older
journals without permission metadata preserve live permissions or create
privately; they do not restore broad default access.

Repository-derived lexical, vector, and graph projections should be rebuilt
rather than trusted across incompatible versions. User-authored memory is
authoritative and follows the knowledge-database backup policy.

## Supported versions

There are no security-supported releases yet. Security support, release signing,
dependency update cadence, and end-of-life policy will be published before the
first general release.
