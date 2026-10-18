# Working across profiles on one checkout

Loupe allows one cooperating publisher per physical checkout under an OS account.
Profiles keep separate credentials and histories; ownership prevents their
publication barriers from overwriting each other's same-base changes. This is
ADR 0010's bounded `claim_backed_v1` contract. Claims and legacy task refs remain
in use. G3's claim-free migration is a separate, explicit operation.

## Inspect or observe from another profile

```sh
loupe --profile work --json workspace status .
loupe --profile benchmark --json workspace observe src/example.py --repo .
loupe --profile benchmark doctor
```

Status includes `authority.state`, `owner_profile`, PID/process-start evidence,
the opaque unfinished-journal locator when present, and
`cooperative_read_barrier`. States distinguish `owned`, `observed_only`,
`recovery_required`, `foreign_recovery`, and `identity_changed`.

`workspace observe` reads ordinary source without opening an editable session.
Its response explicitly reports `cooperative_read_barrier: false` and
`edit_capability: false`. It may observe a multi-file publication in progress;
it never records an edit base or grants staging authority. Ignored runtime,
private paths and recognized secret material remain excluded. Histories, staged
proposals, status and doctor remain inspectable when mutation is blocked.

## Acquisition, idle handoff and workers

Opening/resuming an editable session, launching work, applying/undoing a proposal,
and managed publication/recovery acquire ownership. Model reads and preparation
participating in the cooperative barrier hold pins too. An actual running or
queued worker retains its pin through cancellation of its asyncio waiter and
shutdown bookkeeping. Active legacy reservations also prevent idle release.

Another operation in the same profile waits for a live mutation on that checkout
without holding the authority manager's global lock. Acquisition then revalidates
physical identity, database binding and the final marker. A dirty marker left by
an operation that has stopped still requires exact recovery.

When pins and active reservations drain, a clean owner releases after 30 seconds
idle. Background advisory scans do not reset that clock. An open conversation or
settled held proposal does not own a checkout forever. Releasing ownership keeps
the held proposal's private runtime available for a later `/apply`.

A new ownership period verifies the checkout and database binding, re-observes
stable current source, advances the workspace epoch and emits a bounded
history-gap notice. The model and terminal are told to re-read source. Loupe does
not reconstruct another profile's intermediate publications. Retained proposals
keep their original bases and still require normal merge/check/base validation.

Symlink aliases to the same physical root/admin objects contend on one key.
Independent clones have different keys. Linked user worktrees have separate
checkout keys and a shared short Git-store lock for shared administration.

## Stable refusals and recovery

| Error | Meaning and next step |
| --- | --- |
| `CHECKOUT_OWNED_BY_OTHER_PROFILE` | A kernel lock is held. Inspect the named owner or wait for clean idle release; retry is safe. |
| `CHECKOUT_FOREIGN_RECOVERY_REQUIRED` | A dirty/unknown marker or invalid database binding requires the recorded owner's journal. Stop and diagnose through that owner; another profile cannot replay or clear it. |
| `CHECKOUT_IDENTITY_CHANGED` | Root/admin objects or their Git relationship changed. Stop mutation and explicitly repair/re-register after inspecting the preserved state. |

For a known original owner with an intact database and journal:

```sh
loupe --profile OWNER task recover
loupe --profile OWNER --json workspace status .
```

Startup uses the same gate before any recovery side effect. Batch and manual
publication recovery require exact before/after path proofs; legacy publication
uses its exact execution/ref intent. Sessionless claims and executions retain
early authority journals bound to their physical checkout, so registering another
clone with the same remote cannot retarget retry or recovery. A third path state,
missing or ambiguous anchor, missing journal, unknown Git effect or mismatched
binding retains attention. An affected checkout does not prevent inspection or
work on an independent checkout.

Dirty is written and fsynced before an applicable journal or checkout side
effect. Clean is written only after durable events and verified final results.
A crash before the owning journal exists is intentionally not inferred to mean
"nothing happened". Shutdown can release a drained process's kernel descriptor
while retaining its dirty marker; this never grants a clean handoff.

There is no automatic authority repair/rebind command in G2. Do not delete,
unlink, replace or chmod authority files to bypass an error. Preserve the owning
profile's database, candidate/runtime evidence and opaque journal locator while
investigating an unresolved state.

## Storage and guarantee limits

The authority root is the account directory returned by
`getpwuid(geteuid()).pw_dir`, followed by `.llm-coord/physical-authority/v1`.
`HOME`, XDG variables, profile directories and `LLM_COORD_*` overrides cannot split
that lock domain. Files/directories must be owned and private, have no symlink
components and use verified supported local filesystem semantics. Checkout/admin
storage must also have supported local identity/locking semantics.

The lock inode is held with `flock`; PID metadata, including process start time,
is diagnostic. Loupe never unlinks a lock inode to clear a stale record.
Versioned, checksummed, atomically replaced and fsynced markers contain physical
identity and opaque journal metadata, never source/runtime bytes or secret
content hashes. Each control database has a random instance ID bound to an
external private witness and its physical database object. A copied/replaced
SQLite object or missing witness cannot inherit authority merely by reusing a
profile name.

An in-place manual restore that preserves the database inode and instance ID can
roll back both local witnesses and is unsupported. G2 provides no general
anti-rollback proof, restore-generation migration, global cross-profile revision
stream or old-binary downgrade fence. Stop old daemons and upgrade every
participating profile: pre-G2 binaries ignore this protocol. Arbitrary same-UID
programs and raw editor/Git writers are outside the cooperative guarantee and
retain the documented external final-check/rename race boundary. Network-backed
or unknown filesystem semantics fail closed.
