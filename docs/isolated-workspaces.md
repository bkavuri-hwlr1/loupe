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
the isolated conversation. Multiple files require repeated flags. Consent is
bound to that one new conversation; it cannot be added during resume, applied
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
- A likely secret, such as `.env`, requires exact-file consent and receives a
  private copy with permissions no broader than `0600`, under private directories.
- Every file is read through no-follow descriptors and checked for source/path
  changes. File, apparent-byte, allocation, inventory, and free-space limits apply.
- Ignored `node_modules`, `.venv`, and `venv` roots use a directory adapter.
  The preview remains bounded without listing their descendants; materialization
  streams up to 200,000 entries and 5 GiB through no-follow descriptors, under a
  60-second copy budget. Files use private COW clones or copies, never hardlinks.
  Relative dependency links must remain inside that dependency tree.
- Virtualenv scripts and editable-install paths are relocated into each private
  snapshot. An external Python interpreter link is allowed only when it resolves
  to the daemon's exact base interpreter; command sandboxes expose that trusted
  interpreter read-only. Environments built with another Python require an
  explicit adapter. No installer runs automatically.
- Credential-like files inside dependency trees are omitted with an exact-path
  notice unless individually approved. Special files, escaping links, nested
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
Known secret signatures are screened as well. Screening buffers complete lines,
so a value split across stdout reads cannot escape. Hook file returns containing
private values are refused. Private values are held only in memory for screening.
Runtime verification uses a process-keyed fingerprint; a daemon restart safely
makes old runtime check evidence stale.

Commands retain Loupe's existing minimized child environment. The general
per-terminal environment forwarding, automatic virtualenv rebuilding, runtime refresh,
and long-running process interfaces in the workspace design are future work.
An isolated session does not grant additional access to provider credentials.

Configured MCP servers follow the same approval and plan-mode rules as shared
sessions. They remain external authority, outside the worktree's scope and
sandbox enforcement; isolation does not make them safe to trust. Exploration
helpers retain their read-only broker view and have no MCP access.

## Receive and publish source

Before model requests, checks, commands, and publication, Loupe receives complete
shared batches in event order and compares the current primary source for edits
made outside Loupe. Unchanged private paths receive the incoming version. Clean
three-way text merges preserve private edits alongside incoming changes. Binary
and symlink changes retain exact identities and do not merge.

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
required checks with the owning isolated session's private runtime. Later private
edits are preserved rather than added to that held proposal. `/undo` reverses the
published batch with exact-base validation and synchronizes the private source.
These operations remain available after daemon restart.

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
