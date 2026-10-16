# Edit, verify, review, and undo

Loupe can run named checks against a private copy of the proposed source,
then publish successful work. A check never runs directly in the shared
checkout. Configure checks once per profile and checkout; existing tasks keep
the configuration with which they were launched.

## Configure checks

Create a TOML file and import it explicitly:

```console
loupe repo add /path/to/project
loupe checks configure --repo /path/to/project --file checks.toml
loupe checks list --repo /path/to/project
```

For this repository, replace `/absolute/path/to/uv` with the executable printed
by `command -v uv`. This example creates a fresh dependency environment inside
each snapshot, avoiding editable packages that point back to the original
checkout:

```toml
setup = [["/absolute/path/to/uv", "sync", "--locked", "--all-groups", "--all-extras"]]
runtime_paths = []

[checks.test]
argv = [".venv/bin/python", "-m", "pytest", "-q"]
timeout = 600
required = true

[checks.lint]
argv = [".venv/bin/ruff", "check", "."]

[checks.types]
argv = [".venv/bin/mypy"]
```

A check accepts `argv`, `cwd` (default `.`), `timeout` in seconds (default 600,
maximum 7200), `required` (default true), and an optional `env` table. Commands
are argument arrays, not shell expressions. Use absolute executable paths or
snapshot-relative executables. The subprocess receives a minimal environment,
private home/cache/temp directories, and explicit environment settings; it does
not inherit provider credentials or your login-shell environment.

`runtime_paths` can list ignored files or directories to copy into the snapshot.
Copies are private, not hard links. Missing paths, source collisions, symlinks,
and special files are rejected with an explanation. Use setup commands to
recreate virtual environments or dependencies that contain links or absolute
paths. Setup commands are trusted project commands just like checks, and their
time counts against the check's timeout. Network access is not sandboxed.

Configuration is stored outside the repository. Editing a repository config
file does not silently authorize new commands; import it again explicitly.
Use an empty configuration to disable checks for future tasks.

## Work with changes

```console
loupe chat --repo /path/to/project
loupe chat --repo /path/to/project --mode plan
loupe chat --repo /path/to/project --mode auto
```

New chats default to normal mode, retaining edits for `/diff` and `/apply`.
Auto mode publishes completed changes automatically. Plan mode only reads,
searches, asks clarification questions, and proposes work; it cannot edit files
or run configured checks. `/mode plan`, `/mode normal`, and `/mode auto` switch
future tasks without discarding conversation context. A running task cannot
change mode, and older retained proposals keep their original review policy.
Existing sessions retain their mode on resume. The legacy `--publish review`
and `--publish auto` flags still select normal and auto.

In normal and auto modes, required checks run before the agent finishes;
failures are returned to it so it can repair the proposal.
When files a task edited change in the checkout while it works -- another
session published, or you edited them -- its pending edits are merged onto
the current version before checks run and before it finishes. Overlapping
edits leave conflict markers for the agent to resolve. In auto mode, a task
whose files changed after it finished is merged and rechecked before it
publishes; a conflict at that point holds its proposal for review.
Repairs are bounded. After a check first fails, the agent has three repair
attempts: the repair stops after three more failing runs of that check, or
sooner if the same failure comes back three times in a row. Timings, temporary
paths, and run identifiers are ignored when comparing failures. A passing run
resets the count, and runs that could not start or were cancelled do not count.
Once the repair stops, the agent can no longer edit files, run checks, or run
commands. It finishes with a partial outcome that says what it changed and what
still fails, and its edits are retained for review. The terminal shows, for
example, `stopped repairing test after 4 failed runs`.
Advisory checks run when requested by the agent and do not gate publication.
Without configured required checks, publication remains available and is not
a claim that the source was tested.

Shared tools support regular UTF-8 file creation, deletion, renaming, and
source-directory creation inside the task's scope. Rename destinations must
be absent. They also provide these typed operations:

| Tool | Behavior |
| --- | --- |
| `read_binary` | Reads and pins the exact file base, returning base64. `metadata_only=true` returns size and identity instead and explicitly authorizes an opaque replacement. |
| `write_binary` | Stages strict `content_base64` bytes against a prior complete binary read, or creates an absent file. |
| `read_symlink` | Reads the link target itself without following it. |
| `write_symlink` | Creates a relative link, or retargets a link previously read with `read_symlink`. |
| `delete_file` | Deletes a regular text or binary file, or a contained symlink, preserving its original state for undo. |

Binary bodies use the same 256 KiB decoded per-file limit and 4 MiB total
retained-byte budget as text. A truncated base64 response does not authorize a
replacement; use an explicit metadata-only read for a large opaque file. Binary
changes, including ASCII content submitted through `write_binary`, never merge
when their exact base changes. Task diffs show byte counts rather than treating
binary bodies as text.

Both the original and proposed symlink targets must be relative, at most 1,000
UTF-8 bytes, and contained within the claimed scopes. Absolute or escaping
links, protected or ignored targets, links through other links, and cycles are
refused. A symlink target is stored as bytes in the same durable journal as a
regular candidate. Required checks and command snapshots recreate links without
following them. Publication and undo preserve object kinds, compare exact bases,
and recover interrupted journaled operations; unrecognized external changes
require attention rather than being overwritten.

Nested repositories, recursive directory deletion, type-changing replacements,
and case-only renames remain unavailable. All edits remain private until the
daemon validates and journals publication.

| Chat command | CLI equivalent | Purpose |
| --- | --- | --- |
| `/diff [TASK_ID]` | `loupe task diff TASK_ID` | Inspect the task's changes and check status |
| `/checks [TASK_ID]` | `loupe task checks TASK_ID` | List check outcomes |
| `/apply TASK_ID` | `loupe task apply TASK_ID` | Apply a retained shared-session proposal |
| `/undo TASK_ID` | `loupe task undo TASK_ID` | Revert one published task |
| `/stop [TASK_ID]` | `loupe task cancel TASK_ID` | Stop work and retain private edits |

Omitting an optional task ID uses the last task in the conversation. In an
interactive task stream, type `/stop` and Enter to stop; Ctrl+C still detaches.
Plain/piped viewers can cancel from a second terminal. A non-interruptible model
request may need to return before cancellation settles; its response cannot
start new tools or publish edits after cancellation.

Normal mode, failed required checks, or incomplete verification retain changes
for review and release their work claim. The user can continue with another
prompt. Use `loupe task discard TASK_ID` to discard a retained proposal.

Check results are bound to the exact source baseline, copied runtime inputs, pending edits, and frozen
configuration. A source or copied dependency change makes previous results stale.
`/apply` merges a held proposal onto the current checkout and reruns missing or
stale required checks on that exact result. Checks run in disposable snapshots
without holding the publication lock; the daemon revalidates the checkout and
check evidence before journaled publication. If the checkout changes during
checks, retry apply. `/apply` and `loupe task apply` wait for those checks;
Ctrl+C stops waiting, not the apply. Overlapping edits, deleted files, and binary or symlink
changes refuse the entire proposal, name its paths, and require a fresh change
from the session. Conflict markers are never applied. `loupe task retry` does
not apply to conversation tasks. To explicitly apply despite failed or missing checks:

```console
loupe task apply TASK_ID --allow-unverified
```

This override is recorded and cannot bypass scope checks, changed-file conflicts,
or unresolved recovery journals. Check-generated source changes are discarded;
a check that modifies source is not valid verification. Streamed output and
exit status are replayable through `task watch`; output is capped at 10 MiB per
check, with explicit truncation, and model results remain bounded at 64 KiB.
If a check prints a recognized private-key or token format, that line and the
remaining output are withheld before storage or model delivery, and the check
is marked as an error. See the [privacy limits](../SECURITY.md#secrets-and-privacy).

## Undo and recovery

Undo creates a new inverse publication. Every affected file must still match
what the original task published. Undo of a merged apply preserves the checkout
changes that existed before that apply. Otherwise the entire undo is refused, leaving
the checkout unchanged. Task-created directories are removed only when empty;
subsequent additions are preserved. Undo does not run checks automatically.
HEAD, the index, and user refs remain unchanged.

Older tasks without retained originals cannot be undone. Terminal task history
uses the existing 30-day retention window; unresolved proposals and recovery
journals remain pinned.

Interrupted checks are uncertain, never successful by inference. Subprocess
supervision stops check process groups if the daemon disappears. On Linux it also
stops processes a check detached from its group, such as daemons started with
`setsid`; on macOS such a process can outlive the check, though the check still
finishes on time. File operations,
apply, and undo use the same publication journal and `loupe task recover`
mechanism as ordinary shared edits. A stop arriving after publication begins
cannot interrupt its filesystem journal.

The supported platforms remain macOS and Linux. Worktrees isolate normal check
outputs from the checkout; they do not sandbox malicious project code or prevent
unmanaged external writers from racing a publication.
