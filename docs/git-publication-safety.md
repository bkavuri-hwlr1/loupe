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
publication success and forces the next preflight to rescan. Reconciliation waits
while the checkout has an unresolved publication journal; publication then reports
that journal (`CHECKOUT_RECOVERY_REQUIRED` or the batch error) rather than asking
for a retry.

At existing scan boundaries, an ordinary commit records `git.commit_observed`,
without changing the workspace epoch or creating another file batch. A HEAD
change is ordinary when its observed working files are unchanged, or when HEAD's
reflog shows only `commit`, `commit (amend)` or `commit (initial)` moves since the
last observation. The reflog matters because the metadata baseline covers only
paths that sessions read or published: committing your own edits to other files
is still a commit. Stable outside-edit observations also advance that baseline.

A published batch is linked once, to the first observed commit whose tree contains
every resulting mode and raw Git blob while the previous HEAD's tree did not.
Later commits that still contain it, and results that were already committed, are
not linked again. Directory-marker batches and transformed content that cannot be
proven from those exact bytes stay unlinked.

When any other HEAD movement (checkout, reset, merge, pull, rebase, cherry-pick)
accompanies changed working files, or the reflog is unavailable, Loupe increments
the shared workspace epoch and sends a checkout-change notice to every active or
disconnected session. Pending candidates keep their original bases and must pass exact-base
validation before publication. Isolated sessions retain their private HEAD and
receive primary checkout changes through their existing incoming sync.

The checks are read-only and cannot serialize an uncooperative Git process with a
filesystem write. Resolve any Git conflict or unexpected later file state before
retrying recovery. Loupe's journal never overwrites an unrecognized third state.
