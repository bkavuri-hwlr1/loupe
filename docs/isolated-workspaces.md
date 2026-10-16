# Isolated workspaces

An isolated conversation uses a private linked worktree containing the selected
checkout's current source. Opening it preserves dirty tracked files, staged
versus working-tree differences, deleted files, ordinary untracked files,
contained source symlinks, and binary source. It does not change the primary
checkout's files, index, branch, or HEAD.

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
- Ignored `node_modules` and virtual environments are left absent with an explicit
  notice. Loupe does not enumerate opaque dependency trees or run an installer.
- Runtime symlinks, special files, nested repositories, and unsupported mounts
  are omitted with a notice. Administrative paths are excluded entirely.

Hydrated paths remain runtime-only even if a task changes `.gitignore`. Their
bytes do not enter ordinary model file tools, source artifacts, source
manifests, or proposals. Closing a conversation does not copy runtime changes
back to the primary checkout.

## Tasks and checks

Model reads and edits operate on the isolated source and its private proposal.
Configured checks and approved model commands use disposable snapshots based
on that source, with exact copies of its hydrated runtime files. Changing `.env`
in a check does not change either the session's `.env` or the primary checkout.
Command changes are discarded; file-tool edits remain the proposed source.

When a command or check can read hydrated runtime, its output is withheld before
model results, task events, check records, or hook file returns are exposed.
The result still reports completion, exit status, and check status. Runtime
verification uses a process-keyed fingerprint; a daemon restart safely makes
old runtime check evidence stale. This conservative output policy also covers
unknown secret formats that ordinary pattern matching would miss.

Commands retain Loupe's existing minimized child environment. The general
per-terminal environment forwarding, virtualenv rebuilding, runtime refresh,
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

An incomplete creation is cleaned only when all existing paths are recognized
creation output: source matches its retained ordinary artifacts and runtime
matches the primary input still available for comparison. Unknown files,
directories, links, changed runtime, or ownership ambiguity preserve the entire
worktree and quarantine its record. Recovery never recursively deletes an
unregistered user path. Successfully cleaned creations can be retried with the
same session ID.

The private worktree is a cooperative filesystem boundary. Shell programs with
arbitrary same-user capabilities are not treated as hostile code. Normal command
approval and sandbox policies still apply.
