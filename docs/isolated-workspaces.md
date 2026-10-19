# Isolated workspaces

An isolated conversation uses a private linked worktree containing the selected
checkout's current source. Opening it copies current working bytes, including
dirty tracked files, deletions, ordinary untracked files, contained source
symlinks, and binary source. The primary checkout's files, index, branch, and
HEAD stay unchanged. The private index starts at the selected HEAD; it does
not reproduce the primary checkout's staged-versus-unstaged split.

```sh
loupe workspace hydrate . --plan
loupe chat --workspace isolated --approve-secret .env
```

The preview is read-only. Its secret approvals do not authorize a later
conversation: repeat an exact `--approve-secret PATH` on the command that opens
the isolated conversation. Multiple files require repeated flags. Opening without
consent still succeeds: each unapproved secret is omitted with an exact-path
notice naming `--approve-secret PATH`. Checks that need that file may fail;
reopen with the flag to supply it. Consent is bound to that one new conversation; it cannot be added during resume, applied
to shared mode, or reused automatically after switching models.

The same flags work with `loupe session open --workspace isolated`. Runtime
consent names paths only. Do not put secret values in command arguments.

## Source and runtime

The source snapshot includes current working bytes, independent of Git's index.
Creation crosses the publication barrier, checks pending recovery, scans prior
outside-edit observations, and captures a bounded source manifest. It pins HEAD
and Git configuration origins, ignore rules, attributes, and local/global
exclude-file identities. A changed policy or source snapshot invalidates
creation. Git filters, submodules, protected source files, and unborn repositories
require an explicit supported adapter or an initial commit; this implementation
refuses them instead of running a filter or fetching objects.

Ignored runtime is copied privately under the hydration policy:

- An ignored ordinary regular file receives a verified COW clone on a supported
  same-device filesystem, or a private byte copy. The clone probe modifies only
  disposable Loupe-owned files. Cross-device copies use private byte copying.
- A likely secret, such as `.env`, is optional. Without exact-file consent it
  is omitted, its path remains reserved as runtime, and the opening notice names
  `--approve-secret PATH`. An approved file receives a private copy with
  permissions no broader than `0600`, under private directories.
- Every file is read through no-follow descriptors and checked for source/path
  changes. File, apparent-byte, allocation, inventory, and free-space limits apply.
- Ignored `node_modules`, `.venv`, and `venv` roots use a directory adapter.
  The preview remains bounded without listing their descendants; materialization
  streams up to 200,000 entries and 5 GiB through no-follow descriptors, under a
  60-second copy budget. Files use private COW clones or copies, never hardlinks.
  Relative dependency links remain inside that tree or point at ordinary source
  packages copied into the same private workspace. Secret, excluded, administrative,
  and outside-checkout targets are refused.
- Virtualenv launchers, activation scripts, editable-install paths, and venv
  configuration are relocated into each private snapshot; other runtime data
  retains exact bytes. External Python links must match pinned `pyvenv.cfg`
  home/executable metadata and name a regular native Python executable. The
  adapter validates metadata without executing the interpreter. Command
  sandboxes expose that base interpreter read-only, including a different
  installed Python version. No installer runs automatically.
- Credential files inside dependency trees, such as a `.aws` directory or a
  `credentials.json` data file, are omitted with an exact-path notice unless
  individually approved. Library code named like a credential, such as
  `anthropic/lib/credentials/` or `google/auth/credentials.py`, is copied, since
  imports need it. Special files, escaping links, nested
  repositories, and unsupported mounts are refused. Administrative paths are
  excluded entirely.

Hydrated paths remain runtime-only even if a task changes `.gitignore`. Runtime
exclusions also reserve case and Unicode-normalization aliases: `LOCAL.DATA`
cannot expose hydrated `local.data` on a case-insensitive filesystem. This rule
is deliberately conservative on case-sensitive filesystems too; differently
cased source names cannot reuse a protected runtime path. Actual filesystem
paths are never rewritten. Runtime bytes do not enter ordinary model file tools,
source artifacts, source manifests, or proposals. Closing a conversation does
not copy runtime changes back to the primary checkout.

## Tasks and checks

Model reads and edits operate on the isolated source and its private proposal.
Approved model commands use disposable snapshots of the private source and
pending edits. Required checks verify the proposed source batch on the current
primary checkout in a disposable snapshot. Both receive exact private copies of
the isolated session's hydrated runtime files. Changing `.env` in a check does
not change either the session's `.env` or the primary checkout. Command changes
are discarded; file-tool edits remain the proposed source.

Command and check output remains available when ignored cache or build files
are present. Before output reaches the model, events, or check records, Loupe
redacts actual copied credential-file values and sensitive configuration values.
Values under secret-like keys (`PASSWORD=xy`) are redacted at any length; other
values, such as `DEBUG=true` or `PORT=8000`, only when at least eight characters
long, so ordinary output stays readable. Dependency trees are library code and
are not scanned for values, apart from approved credential files inside them;
screened source fixtures contribute only values of eight or more characters.
Known secret signatures are screened as well. Screening buffers complete lines,
so a value split across stdout reads cannot escape. Hook file returns containing
private values are refused. Private values are held only in memory for screening.
Runtime verification uses a process-keyed fingerprint; a daemon restart safely
makes old runtime check evidence stale.

Repeated checks reuse digests only while full file metadata still matches, and
reuse secret-classification booleans only for exact source content identities.
A verified COW/copy transfers its digest to that destination's exact metadata;
any later write invalidates it. Pinned private dependency snapshots use at most
four copy workers with eight files in flight. Source and target descriptors stay
pinned, source paths are rechecked, and all queued work drains before failure or
cleanup. Every invocation still receives its own writable private runtime.

On macOS/APFS, a clone of Loupe with its actual virtualenv (6,576 regular targets,
including interpreter links) ran repeated `.venv/bin/python -c pass` checks in
1.43–1.47 seconds, down from 3.03 seconds; cold runs were 1.79 seconds versus
3.91 seconds. These are full `CheckRunner.run` measurements including Git worktree
creation, private runtime copying, source checks, output screening, and cleanup,
with no provider request. Other filesystems retain private-copy fallbacks.

Commands retain Loupe's existing minimized child environment. The general
per-terminal environment forwarding, automatic virtualenv rebuilding, and
long-running process interfaces in the workspace design are future work.
An isolated session does not grant additional access to provider credentials.

Configured MCP servers follow the same approval and plan-mode rules as shared
sessions. They remain external authority, outside the worktree's scope and
sandbox enforcement; isolation does not make them safe to trust. Exploration
helpers retain their read-only broker view and have no MCP access.

## Receive and publish source

Before model requests, checks, commands, and publication, Loupe receives complete
shared batches in event order and compares the current primary source for edits
made outside Loupe. Regular files with unchanged device, inode, mode, size,
mtime, and ctime reuse their stored source identity without rereading or
re-storing the body. The filesystem scan runs outside the publication lock;
its result is discarded if a publication races it. A crossing save or an
unavailable path keeps its last accepted version, reports a bounded path-only
deferral, and retries at the next safe point without failing the task. Unrelated
source changes can still advance.

Screened source fixtures, such as tests containing fake credentials, synchronize
privately when their local copy is unchanged. Their bodies stay out of ordinary
source artifacts, proposals, and model file tools. A prepared journal stores only
identities and random staging tokens; incoming bodies live under the owned
private worktree's Git administration with `0700` directories and `0600` files.
If both copies changed, Loupe preserves the private file, retains the incoming
version there, and reports that path once. The conflict survives restart;
repeating a safe point does not repeat its notice. Recovery validates staged
content and recognizes only its frozen before/after identities. Incoming opaque
transitions keep the existing 8 MiB changed-path limit; a larger path is deferred
with a path-only notice while unrelated changes advance. Closing the
session removes this staging with its private Git registration.

Unchanged private paths receive the incoming version, including operator edits
already incorporated into another session's batch since this session's accepted
snapshot. Clean three-way text merges preserve private edits alongside incoming
changes when they share the accepted base. Binary and symlink changes retain
exact identities and do not merge. A non-absent change between file, symlink,
and directory kinds remains a conflict requiring explicit resolution.

An overlap preserves the private file and retains the incoming source artifact.
Unrelated paths can still advance. The conflict survives restart and later
incoming batches; publication stops until the preserved path is explicitly
resolved. Edit the source to the intended combined result and rerun its checks.
An incoming directory deletion preserves a container with private children;
move the children to their intended location and remove the empty container to
resolve that directory conflict. Recovery never recursively deletes it.

Auto mode publishes the checked, scoped source delta through the same batch
journal used by shared sessions. The primary checkout's HEAD, index, and refs
remain unchanged. Normal mode freezes a proposal for `/diff` and `/apply`.
Applying a held proposal merges it onto the current primary source and reruns
required checks with its retained private runtime generation and frozen check
configuration. Later private edits are preserved rather than added to that held
proposal. `/undo` reverses the published batch with exact-base validation and synchronizes the private source.
These operations remain available after daemon restart. A published task's
exact-base undo also remains available after its closed private workspace has
been retired; it uses retained publication data against the shared checkout.

Publication retains the shared limits: 50 paths, 256 KiB per regular file, and
4 MiB of retained original and candidate bytes. Runtime paths never enter source
proposals, including after ignore-rule changes or alternate path spellings.
The manual single-file publication RPC remains shared-only; isolated tasks use
batch publication.

## Recovery

Incoming source transitions are journaled before any filesystem write. Recovery
accepts only each path's recorded before or after state and checks all paths
before continuing. An unrelated third state is preserved for inspection instead
of being overwritten. The accepted source manifest and local transition journal
remain separate from the private worktree's detached Git HEAD.

A confirmed primary publication remains successful if private catch-up cannot
complete. The task records that private synchronization needs recovery; later
recovery confirms the published batch independently of the private error.

Each creation phase has a durable intent: planned, registered, source materialized,
runtime hydrated, then ready. After a daemon crash, a ready workspace is reopened
only when its exact registered worktree, base HEAD, lock, and private ownership
marker still match. Runtime exclusions are restored before model access.

Closing a conversation retires its private worktree and Git registration once
no task is running and no held proposal or unresolved publication still needs it.
A held proposal keeps its private runtime across close and daemon restart so
`/apply` can rerun its checks. Applying or discarding the last held proposal lets
cleanup proceed. Published undo history uses retained source changes and does not
need to keep runtime copies.

An incomplete creation is retired only when its existing source paths are
recognized creation output. A crash between Git registration and the private
ownership marker can be recovered when the worktree is empty apart from its Git
pointer and has the expected private lock reason. Copied runtime, dependency
roots, and screened source fixtures are disposable Loupe-owned files: cleanup
unlinks them through no-follow descriptors, including copies in owned recovery
archives. It never copies runtime changes back to the primary checkout.

Recovery moves ordinary source evidence through no-follow directory descriptors
into a private `.recovery-WORKSPACE_ID/files/` archive under Loupe's managed
worktree directory. This preserves later writes through an already-open source
file descriptor and removes only empty source directories. A crash after moving
a nested source file can resume from the archive. The source evidence persists
after retirement, outside source artifacts and model tools, under an owner-only
directory. Copied private runtime and screened source are not retained there.
Startup also scrubs recognizable private inputs from older owned archives.

Unknown files, directories, links, changed creation bytes, or ambiguous ownership
stop retirement. The remaining worktree and ordinary source evidence are
preserved, and the workspace is quarantined with the archive location in its
reason. Newly added files and replacements discovered during cleanup are
preserved too. Recovery never recursively removes the source worktree. Known
retired creations can be retried with the same session ID; ambiguous states need
operator inspection.

The private worktree is a cooperative filesystem boundary. Shell programs with
arbitrary same-user capabilities are not treated as hostile code. Normal command
approval and sandbox policies still apply.

### Runtime refresh and retained proposals

Isolated sessions recheck ignored runtime metadata at safe points before model
turns, commands and checks. A stable primary update replaces an unchanged
private copy; a private edit on both sides is preserved and produces one
path-only conflict notice. Changes during a running command, check or hook wait
until a later safe point. Dependency roots are inspected recursively by stat
receipts, so changes below an unchanged directory timestamp are detected without
reading unchanged file contents. New ignored inputs use the ordinary hydration
budgets and adapters. A change in ignore rules cannot turn a previously pinned
runtime input into source.

For example, with `local.data` already ignored, open
`loupe chat --workspace isolated`, then edit `local.data` in the primary checkout.
The next prompt or check adopts that stable update if the private copy is
unchanged. Editing both copies keeps the private version and reports
`Runtime conflict; preserved private copy: local.data` once for that primary
change, including across restart.

Discovery retains the hydration inventory defaults: 20,000 entries, 4 MiB of
metadata, depth 64, and 10 seconds. Each runtime root's recursive stat scan is
separately bounded to 200,000 entries, depth 64, and 60 seconds. New regular-file
copies retain the 256 MiB per-file and 2 GiB aggregate apparent/allocation budgets;
dependency copies retain the adapter limits described above. See
[ADR 0009](adr/0009-isolated-runtime-hydration.md) for the full copy policy.

An incomplete inventory, unstable input, or copy failure emits a refresh deferral
and retries at a later safe point. Any prepared installation must recover before
another command or check can snapshot runtime. Unchanged stat-only refresh
medians were 62 ms at 10,000 dependency files and 213 ms at 50,000, from three
passes per size in a disposable synthetic repository. These measure detection,
not changed-tree copying; see the
[measurement record](experiments/runtime-refresh-c8-2026-10-18.json).

Exact `--approve-secret PATH` consent given when opening a conversation also
allows that file's later stable updates. Without opening consent, an appearing
or changed likely-secret file is reported and remains absent. Dependency
credential files still need their own exact-file consent. Notices contain paths,
never runtime values or secret digests.

Retaining a proposal freezes a private runtime generation and its check
configuration. `/apply` checks current shared source plus the held proposal
against that generation, including after a daemon restart. The live session can
adopt newer runtime without changing the held proposal's inputs. A changed
retained generation is refused rather than silently accepted.

Refresh stages private copies outside the shared publication lock. Installation
journals its before/after stat receipts, then atomically renames individual roots
under the publication lock. After a crash, recovery completes the recorded
whole generation before another action can use it; unrecorded staged copies are
discarded. Unexpected later private edits are preserved and require recovery.
When publication authority is attached, an installed receipt survives staging
cleanup until that authority acknowledges it; a later private write blocks that
acknowledgment and retains the recovery evidence.
Runtime contents never enter source batches, the source artifact store or
knowledge indexes. Frozen runtime copies remain only while their workspace and
retained proposals need them; closing a conversation removes the owned runtime
generation directory after those proposals have settled.
