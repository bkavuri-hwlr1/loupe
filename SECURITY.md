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
results are retained or sent to the model. A search skips files that match these
formats and reports only how many it withheld, not their names. This
conservative pattern screening is not a complete secret scanner.
The `core.excludesFile` setting is read once per tool call, so a changed setting
applies from the next tool call; the excluded patterns themselves are read on
every check.
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
  GnuPG, cloud CLIs, `gh`, Docker, `.netrc`, Git's credential cache, the
  1Password agent, package-registry tokens, Codex and Claude credentials,
  keychains and keyrings, and browser profiles on macOS and Linux, including
  Snap and Flatpak installs) are unreadable;
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

### Exploration helpers

The `explore` tool hands a read-only question to a helper: a separate model
session with the same provider. A helper has no more authority than the task
that started it:

- its only tools are `list_files`, `read_file`, `search_text`, and `read_diff`,
  through a view of the task's broker with the same source exclusions, secret
  screening, and pending edits; it cannot edit, run checks or commands, ask the
  user, or start further helpers;
- it keeps its own observations, so a file a helper read never counts as the
  task having read it before a full-file write;
- its tool calls are reserved from the task's call budget before it starts,
  always leaving the task some calls of its own, and every call counts,
  including calls to tools it does not have; its token usage is added to the
  task's total, and it starts no model request after the task's deadline;
- its report is tool output for the task's model: screened for recognized
  secret material, bounded in size, and labelled so the task still reads a
  file itself before editing it.

A helper sends repository content it reads to the configured provider, as the
task's own reads do. Set `explore = false` under `[agent]` to disable helpers.

### MCP servers

Model Context Protocol servers configured under `[mcp.servers]` are external
authority: they run as the user, outside Loupe's scope enforcement and
sandbox, so Loupe cannot limit what they do. Loupe controls only what it hands
them and how it treats what they return:

- a server runs only when configured, for the length of one task, in its own
  process group that is ended when the task finishes;
- by default each server needs the user's approval before its first call in a
  task, given through the interactive session; model output can never approve
  it, and a server that needs approval is not offered without someone to ask;
- MCP tools are not offered in plan mode or to exploration helpers;
- a server inherits only a minimal environment (such as `PATH` and `HOME`)
  plus its configured variables, so provider keys in Loupe's environment never
  reach it, and its working directory is the user's home unless configured;
- tool names, descriptions, and input schemas from a server are validated and
  bounded before they reach the model, and descriptions name the server;
- results are text only, bounded in size, withheld if they contain recognized
  secret material, and labelled as external data rather than instructions;
- events record which servers started or failed by name and reason category,
  never server output.

Arguments the model passes to an MCP tool are sent to that server, and its
output is sent to the configured model provider as tool output.

### Hooks

Hooks under `[[hooks.pre_tool]]` and `[[hooks.post_edit]]` are commands from
the user's configuration. They run with the same governance as model-chosen
commands, without per-run approval since the user configured them:

- each run uses a disposable snapshot of the checkout with the task's pending
  edits, inside the operating-system sandbox, with no network, a private home,
  only `PATH` inherited, and no access to the real checkout or Loupe's state;
- if no sandbox is available, hooks do not run and the conversation says so;
- a `pre_tool` hook can only allow or block a call; a blocked call still
  counts against the task's tool budget;
- a `post_edit` hook's changes are read back only for the paths that triggered
  it, only as regular files within the size limit, and are staged through the
  same broker checks as the agent's edits (scope, secret screening, size), so
  they are reviewed before they apply;
- hook output is never streamed to the conversation; a blocking reason or a
  failed formatter's output reaches the model as screened tool output, and
  events record only the hook's name, the tool, the paths it changed, and a
  reason category.

Hooks apply to the agent's own calls in shared-workspace tasks; exploration
helpers' reads do not run them.

### Web pages

`web_fetch` reads one public page as text. It is a network request the model
chooses, so:

- `[agent] web_fetch = "ask"` (the default) asks the user before the first
  fetch from each domain in a task, showing the full URL; domains listed in
  `web_domains` and `web_fetch = "allow"` skip the question, and without
  either and without someone to ask, the tool is not offered;
- only `http` and `https` URLs without credentials are fetched, and URLs
  containing recognized secret material are refused, so a recognized secret
  cannot be sent in a query string;
- every address a host resolves to must be public: loopback, private,
  link-local (including cloud metadata endpoints), carrier-grade NAT, and
  multicast addresses are refused, and the connection is pinned to the checked
  address so DNS rebinding cannot redirect it; each redirect is checked and
  approved the same way, up to five;
- requests are GET only, with no cookies, credentials, or proxy settings, a
  fixed user agent, a timeout, and a 2 MiB download limit; only text content
  types are read;
- pages are converted to text, bounded per call, withheld if they contain
  recognized secret material, and labelled as external content rather than
  instructions;
- events record only the domain and the page's size, never the URL, whose
  query string could carry data.

A page's text is sent to the configured model provider as tool output.
Exploration helpers cannot fetch pages.

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
