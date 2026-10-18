# ADR 0010: Bounded publication authority across profiles

Status: **Accepted** (2026-10-18), by explicit user acceptance of the Wave 5
plan and this ADR. G1 supplies the reproduction and implementation contract.
Acceptance authorizes G2 implementation; it does not claim G2/G3 are complete.

## Context and measured risk

Profiles separate credentials, preferences, sockets and control databases. A user
can therefore open the same checkout in a normal profile and a benchmark profile,
or keep one profile per provider account. The daemons currently have independent
Python publication locks and SQLite journals. Neither knows when the other is
inside its filesystem replacement barrier or recovering a partial publication.

`tests/integration/test_cross_profile_authority.py` reproduces the first problem
with two **real daemon processes**, two profile databases and ordinary shared
session/read/stage/publish RPCs. A test-only scheduling barrier pauses each daemon
immediately before the production atomic rename, after its last base check. Both
have observed the same original bytes and durably prepared separate journals.
Releasing A and then B produces two confirmed publications; B overwrites A's
result although B's recorded base never contained it. HEAD and index stay
unchanged. Exact-base checks alone cannot close this cross-daemon check/rename
window. An in-process lock is sufficient only when all writers share it.

On the current G1 baseline (`fa6632a`, product source from merged Waves 1–3), the
reproduction reaches that exact witness. Its strict expected failure catches only
`_CrossProfilePublicationViolation`, raised after checking both live PIDs, distinct
profile sockets/databases, equal bases, both prepared journals, both successful
responses, both confirmed journals and final bytes. Startup errors, missing
barriers, RPC errors and unrelated assertions remain test failures. G2 must
replace the expected-failure annotation with the owner/refusal assertions once it
implements the gate; scheduling instrumentation must never weaken a base check.

The larger workspace plan (§9, §14.22, §24.2 and Phase 11) additionally specifies
state-generation restore, global equality evidence and a new claim-free execution
model. Those capabilities do not exist in the current schema. This decision
separates a useful ownership guarantee from that later cutover instead of
mislabeling the current hybrid runner as the finished design.

## Decision: G2 is one cooperating publisher, with conservative recovery

### Physical identity and a persistent authority root

- Resolve the root from `getpwuid(geteuid()).pw_dir` and the fixed product suffix
  `.llm-coord/physical-authority/v1`. Profile IDs, `HOME`, XDG paths, the working
  directory and `LLM_COORD_*` overrides do not select this root. Production
  authority files never live in temporary/runtime directories.
- Require same-UID ownership, owner-only directories/files, no symlink components
  and supported local filesystem semantics. Refuse wrong-owner, permissive,
  network-backed or unverified roots; do not silently repair somebody else's
  directory permissions. Test helpers may inject a disposable root directly;
  there is no production per-process override that splits the lock domain.
- The checkout key hashes a version tag and verified device/inode identities of
  the selected checkout root and its Git worktree admin directory, with object
  format. Verify their Git relationship through pinned no-follow descriptors;
  retain available birth/generation and stable admin evidence in the binding.
  Canonical paths are diagnostics, never an alternative key. Aliases reaching
  the same physical objects reuse the key; insufficient or changed binding
  evidence disables mutation rather than minting a path-based lock.
- Distinct user-created worktrees have distinct checkout keys. Ref/config and
  worktree-registration mutations also take a short common-Git-store lock,
  derived from its verified physical directory identity, because their working
  trees share that administration. Acquire checkout ownership before the store
  operation lock; order multiple keys lexically if an operation needs several.

### Kernel ownership and lifecycle

An open descriptor holding `flock(LOCK_EX | LOCK_NB)` grants ownership. Never
unlink/recreate a lock inode to clear a stale record. The diagnostic record
includes profile, database-instance identity, daemon boot ID, PID **and process
start identity**, socket and acquisition time. PID existence alone proves nothing.
A new daemon first acquires the kernel lock; only then may it replace stale
metadata after validating the marker and its database binding.

Acquire before granting managed mutation capability, launching work that can
publish, isolated-worktree creation, held apply/undo, manual publication or
recovery. Revalidate ownership/binding at final mutation boundaries. Cover both
current batch and manual single-file publication, plus legacy Git/ref and managed
worktree side effects; claiming ownership only around one public RPC is
insufficient. Model reads participating in the owner's logical read barrier also
hold an ownership pin while they execute.

Keep ownership pinned while any task, command/check preparation, mutation,
recovery, active legacy reservation or applying journal can still act. Once all
pins drain and the marker is clean, release after **30 seconds** idle. Open
conversations and settled held proposals alone do not pin ownership forever:
their next mutable action reacquires ownership and revalidates exact bases.
Releasing ownership does not dispose of a held proposal's private runtime.
Shutdown releases only after work has drained; an abandoned worker must retain
its pin until it stops or the process exits. Clearing a Python task registry is
not proof that a worker thread stopped.

### Read-only peers and stable errors

Another profile may inspect owner/status, histories and ordinary source through
explicit read-only operations. Such reads are labeled **outside the owner's
cooperative read barrier**, since they can see an in-progress multi-file batch.
They gain no edit observation/capability and never create an authoritative
baseline, reconcile a journal, stage/publish a mutation or create a managed
worktree. An already staged proposal remains inspectable.

- `CHECKOUT_OWNED_BY_OTHER_PROFILE`: another live kernel-lock holder blocks the
  requested managed action. Return owner profile, boot/PID/start evidence and a
  retryable indication; never provider credentials or arbitrary process text.
- `CHECKOUT_FOREIGN_RECOVERY_REQUIRED`: a dirty marker belongs to a different
  profile/database instance, or the required owning journal cannot be verified.
  Name the owner profile and opaque journal locator so recovery can be run there.
- `CHECKOUT_IDENTITY_CHANGED`: root/admin binding no longer matches. Refuse
  mutation and require explicit repair/re-registration; never inherit history
  solely because a path spelling is reused.

Unknown/corrupt marker versions, unsafe authority storage and ambiguous local
recovery fail closed with a stable recovery-required error. `doctor` and
`workspace status`, including JSON output, distinguish owned, observed-only,
foreign recovery and identity-change states.

### Dirty markers and the database owner

The marker is owner-only, versioned, checksummed, atomically replaced and followed
by parent-directory fsync. It records physical key, monotonic marker generation,
state (`clean`/`dirty`/`attention`), owner profile, **database-instance ID**, daemon
boot, owner checkout ID and an opaque journal kind/ID when dirty. G2 identifies its
current claim-backed hybrid as `execution_contract = claim_backed_v1`; it does
not call that contract the claim-free `workspace` regime of §14.22.

Create a cryptographically random database-instance ID once in SQLite and bind it
to an owner-only external installation record in that profile's data directory.
The physical database binding and IDs must agree on every acquisition. A copy
or replacement at a different physical object cannot adopt ownership by sharing
a profile name. Missing/mismatched bindings require explicit rebind/repair and
remain read-only. A dirty marker always requires its exact referenced local
journal and current owner binding, even when the profile string matches.

This binding does **not** detect an in-place manual restore that preserves the
inode and database-instance ID. Restoring an earlier SQLite snapshot and its
external record in place can roll back both local witnesses. Such a restore is
an unsupported operation outside G2's protocol; no anti-rollback guarantee is
claimed. A missing/mismatched dirty journal still fails closed, but that check
is not a general database freshness proof. Complete restore fencing needs the
larger generation/tombstone or global monotonic watermark design and fault tests.
G2 introduces no restore command and no automatic authority repair.

For each mutation: reserve its journal ID, write/fsync dirty **before** making
that journal applicable or changing any checkout path, then use the existing
prepared/validated journal. Confirm durable DB events and final path identities
before CAS/fsync of the clean marker. A no-effect conflict may clear dirty only
with owning-journal proof. If a crash leaves dirty before the journal record was
created, keep attention rather than guessing whether an older copied database
lost the record. Dirty/attention ownership cannot be released as a clean handoff.

OS-lock release after a crash does not imply a clean checkout. Foreign profiles
never roll forward or erase the owning profile's journal. Original-owner recovery
reacquires ownership and uses the existing exact before/after proofs; unknown
third states preserve attention. All startup and explicit recovery paths pass
this gate before touching files. A missing marker for a checkout already bound
by G2 is recovery-required; it is not permission to create a new clean marker.

### Clean handoff and the guarantee boundary

A clean marker proves the former coordinated operation completed; it does not
promise that an editor made no later changes. On clean acquisition, validate
physical bindings and prior marker generation, then establish current stable
source observations before granting an edit capability. A change of profile or
ownership discontinuity emits a path-only handoff/history-gap notice and advances
that profile's workspace epoch; existing candidates retain their original bases
and must reconcile/recheck normally. C4 refreshes observed paths before the next
model turn. This is advisory context plus exact-base mutation protection, **not**
a reconstruction of another profile's intermediate history or a global revision
stream. Unstable scans defer mutation rather than install a partial baseline.
No shared marker contains source/runtime bytes or raw secret equality hashes.

This guarantee covers participating updated Loupe daemons under one OS account.
Pre-G2 binaries ignore the new lock, so users must stop old daemons and upgrade
all participating profiles. It is not a sandbox against arbitrary same-UID code
or raw filesystem/Git writers. External writers retain the documented final
check/rename race boundary. G2 does not claim to make an old binary obey a new
protocol.

## Decision: G3 is explicit legacy migration, after G2

Current shared/isolated tasks still create claims, renew leases and store
claim-bound `task_executions`; optimistic scheduling did not remove those
contracts. Sessionless runs, fixtures and claim-only commands additionally use
legacy task refs/integration. Merely adding a `workspace` flag would either leave
two authorities active or disable the current runner.

G3 therefore adds the claim-free workspace execution/advisory-intent path before
activating that regime. Keep old execution/publication tables and refs readable;
use additive tables/contracts instead of silently making mandatory legacy claim
fields optional. G3 upgrades G2's marker format: supported writers adopt the
monotonic `legacy → transition → workspace` regime protocol, and older G2 writers
reject that unknown format. This is distinct from stopping pre-G2 binaries.

A read-only migration plan enumerates **every** physical checkout mapping across
repo keys, target refs and adapters, with provenance/confidence. Current remote-
derived repository rows can overwrite clone paths; ambiguous historical mappings
must block conversion, never be guessed from the latest registered path.
The default upgrade preserves legacy behavior and every row/ref. Cutover is an
explicit operator action after reviewing the complete plan, with these choices:

| Existing state | Required disposition |
| --- | --- |
| Queued | Keep in explicit legacy mode by default, or explicitly cancel with a durable reason. Never silently activate after cutover or drop it. |
| Active/expired work | Drain/recover under the old contract, or explicitly cancel and retain its checkpoint/proposal. Lease expiry alone does not authorize deleting evidence. |
| Publishing/integration | Reconcile the exact intent/ref first. Ambiguous side effects block cutover. Retain/export confirmed results. |
| Held/completed work | Keep diff, apply/undo where supported, history and internal refs inspectable. Export requires an explicit action. |

The physical owner gates new claim requests **and waiter activation** for the
complete mapping set in one transition. Persist an idempotent cutover intent,
write/fsync transition before enabling either new authority, commit the drained
dispositions and exact current checkpoint in SQLite, then write/fsync workspace
and complete the intent. A crash resumes that intent; it never rolls the global
generation backward. Every action validates its compatible regime at its final
boundary. In workspace mode, sessionless coding runs use a durable one-shot
workspace session; fixture/ref workflows require explicit legacy mode or export.
`loupe claim` mutation remains available in legacy mode with deprecation guidance
for one compatibility release; inspection remains available in both modes.

## Deferred, dropped and deliberately retained boundaries

| Larger-plan mechanism | Decision for G2/G3 |
| --- | --- |
| Global protected handoff equality key and exact cross-profile manifest comparison | Deferred. G2 never compares protected bytes across profiles or claims exact shared history; it records a gap and re-observes current source. |
| Offline-drift adoption with immutable reconciliation member chains | Deferred. No fabricated intermediate authorship/history; unstable current source blocks mutation. C4 is advisory, not a substitute for the larger reconciliation protocol. |
| `CURRENT` state-generation layout, fixed-path tombstone, restore/GC lock handoff | Deferred. G2 rejects changed physical DB objects, missing bindings and dirty-journal mismatches. In-place manual restoration preserving the inode/ID is unsupported and not fenced; no general anti-rollback or downgrade guarantee is claimed. |
| Authenticated import of a foreign profile's journal | Deferred. Recover through the recorded owner; no cross-profile journal copying. |
| Shared global revision/event ledger across profiles | Not promised by these packages. Profiles retain independent histories and explicitly report handoff gaps. |
| Claim-free workspace execution and irreversible regime cutover | Retained in G3, not smuggled into G2 or bypassed by optimistic claims. |
| Distributed/network-filesystem arbitration or hostile same-UID protection | Out of scope. Unsupported physical identity/locking semantics fail closed. |

## Verification and rollout

G2 is complete only when the checked-in reproduction becomes a passing owner/
refusal test, real-process competing/idle/crash tests pass, every mutation and
recovery boundary is covered, and unaffected checkouts remain usable when one
checkout needs attention. Include symlink aliases, independent clones, linked
worktrees sharing Git administration, environment overrides, PID reuse, unknown
markers, database replacement, runtime/source privacy and shutdown workers.
Fault-inject dirty marker/journal/file/DB/clean-marker boundaries; a foreign
profile must never repair or overwrite them.

G3 requires upgrade fixtures for every legacy state, several target/ref mappings,
overwritten clone-registration ambiguity, queued-activation races and crashes at
each cutover boundary. Old history/refs remain inspectable/exportable; old clients
receive explicit capability errors for unsupported mutation. No feature rollback
may erase new ledger data or restore obsolete physical authority.

The user accepted this contract on 2026-10-18 before G2 implementation.
Acceptance does not automatically perform an irreversible G3 migration on any
user's checkout.
