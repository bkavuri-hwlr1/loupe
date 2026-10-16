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
Configured checks and approved model commands use disposable snapshots based
on that source, with exact copies of its hydrated runtime files. Changing `.env`
in a check does not change either the session's `.env` or the primary checkout.
Command changes are discarded; file-tool edits remain the proposed source.

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

C2 retains source proposals and reports that publication requires the isolated
batch publisher. C3 adds publication and incoming shared-checkout batches; the
single-file legacy publication RPC remains unavailable for isolated sessions.

## Recovery

Each creation phase has a durable intent: planned, registered, source materialized,
runtime hydrated, then ready. After a daemon crash, a ready workspace is reopened
only when its exact registered worktree, base HEAD, lock, and private ownership
marker still match. Runtime exclusions are restored before model access.

An incomplete creation is retired only when its existing paths are recognized
creation output: source matches its retained ordinary artifacts and runtime
matches the primary input still available for comparison. A crash between Git
registration and the private ownership marker can be recovered when the worktree
is empty apart from its Git pointer and has the expected private lock reason.

Recovery first moves recognized files through no-follow directory descriptors
into a private `.recovery-WORKSPACE_ID/files/` archive under Loupe's managed
worktree directory. It retains these bytes, including later writes through an
already-open file descriptor, and removes only empty directories. A crash after
moving a nested file can resume from the archive without requiring the original
name to remain present. The archive persists after successful retirement and is
not automatically deleted; it may contain private runtime inputs. It is outside
source artifacts and model tools, under a directory accessible only to its owner.

Unknown files, directories, links, changed bytes, or ambiguous ownership stop
recovery. The worktree and any already-archived files are preserved, and the
workspace is quarantined with the archive location in its reason. Newly added
files and replacements discovered during cleanup are preserved too. Recovery
never recursively removes a worktree. Known retired creations can be retried
with the same session ID; ambiguous states need operator inspection.

The private worktree is a cooperative filesystem boundary. Shell programs with
arbitrary same-user capabilities are not treated as hostile code. Normal command
approval and sandbox policies still apply.
