# Git publication safety

Loupe leaves HEAD, the index, refs and Git locks under your control. Before a
shared publication, isolated publication, held apply/undo or manual file
publication, it checks the checkout's own Git administration directory. Linked
worktrees use their own index and operation markers.

An existing `index.lock`, rebase, merge, cherry-pick, revert or sequencer operation
returns retryable `GIT_OPERATION_ACTIVE`. Finish the Git operation and retry;
Loupe never removes the lock or aborts the operation. Paths with unresolved index
stages always return `INDEX_PATH_UNMERGED`.

A path whose stage-zero index entry differs from HEAD produces a staged-path
notice. Publication updates the working file while retaining your staged version.
To refuse such publications instead, save and restart the profile daemon with:

```toml
[core]
refuse_staged_paths = true
```

This policy returns `INDEX_PATH_STAGED`. All three refusals use CLI exit code 75.
Held proposals remain available when a preflight refuses apply.

Git may start after a preflight. Loupe fingerprints HEAD, index bytes and operation
markers before and after apply; the existing durable journal still decides the
result from exact file identities. A changed Git fingerprint records
`git.concurrent_operation_attention` with affected paths and requires a scan
before the next publication. A failed advisory postflight preserves confirmed
publication success and forces the next preflight to rescan.

At existing scan boundaries, a HEAD change with unchanged observed working files
records `git.commit_observed`, without changing the workspace epoch or creating
another file batch. Stable outside-edit observations advance that baseline before
a later commit. Recent published batches can be linked to a commit only when all
resulting modes and raw Git blobs match its tree exactly. Directory-marker batches
and transformed content that cannot be proven from those exact bytes stay
unlinked.

When HEAD movement accompanies changed working files, Loupe increments the shared
workspace epoch and sends a checkout-change notice to every active or disconnected
session. Pending candidates keep their original bases and must pass exact-base
validation before publication. Isolated sessions retain their private HEAD and
receive primary checkout changes through their existing incoming sync.

The checks are read-only and cannot serialize an uncooperative Git process with a
filesystem write. Resolve any Git conflict or unexpected later file state before
retrying recovery. Loupe's journal never overwrites an unrecognized third state.
