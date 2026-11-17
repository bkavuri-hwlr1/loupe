# ADR 0011 — Start new checkouts without editing claims

**Status:** Proposed, November 17, 2026. Maintainer decision pending.

Today every checkout starts with legacy claims. A user who wants advisory intents
must review and apply a cutover, even on a fresh clone with no work to migrate.
We propose that first registration of a demonstrably new physical checkout
select `workspace`. Selection happens once for the physical checkout and applies
to every profile. Existing legacy checkouts keep the explicit cutover procedure.

## Establishing first registration

Use the account-wide authority root and the existing physical key for the checkout
and its Git administration objects. A different directory spelling, branch, or
profile does not make a checkout new. A linked worktree has its own checkout key.
Read-only discovery, status and observation must not choose a regime.

At the first editable acquisition take the maintenance and physical ownership
locks in the order defined by ADR 0010. Before admitting work:

1. Validate the physical identity and installation binding. A valid marker takes
   precedence over profile defaults: v1 is legacy, v2 carries a transition or
   workspace decision. A second profile adopts that decision after normal clean
   handoff. A foreign dirty marker needs its owner. Missing bound markers,
   invalid records and ambiguous identity still require recovery.
2. Consult registration history. Old profiles can have registrations predating
   physical markers, and maintenance could have retired a legacy marker after a
   move. An empty directory in the authority root alone cannot prove first use.
   The rollout needs an account-wide inventory of known legacy physical keys,
   including unresolved registrations from participating profiles, before enabling
   automatic selection. Keep a durable record for a previously registered key
   across pruning. If the inventory cannot rule out prior use, preserve legacy
   behavior for an unbound checkout, or refuse if a binding needs repair. Do not
   discover other profiles' history by inspecting their private conversations.
3. For a verified new key, record a versioned, checksummed **first-registration**
   decision under the lock. Keep it as a permanent workspace marker. Distinguish
   that origin from a reviewed cutover: there was no legacy disposition, source
   checkpoint or cutover intent. Journal the physical decision before enabling
   any task or claim. On interruption, replay the same decision into the local
   checkout-regime row and repository mappings. A profile taking over can
   reconstruct these mappings from the clean marker. Conflicting state blocks
   writes. A crash must never make both execution contracts available.

Profiles predating physical authority cannot participate in this inventory while
they continue writing. An equivalent safety test for the implementation is to
register a checkout with an older profile, then present it to an empty profile
first after upgrade. It must never be classified as new. A separate clean clone
with its own identity should get the new default.

## Observable command behavior on a new workspace checkout

| Operation | Proposed behavior |
| --- | --- |
| `loupe chat`, session open/resume/switch, coding prompts | Use durable workspace executions. The session may use shared or isolated files. Declared scopes become advisory intents. |
| `loupe run` without a session | Create a shared one-shot session, retain history, and close on settlement. Keep the session available when publication needs recovery. Retrying the same task ID uses its existing attempt. |
| Claim-only reservations, including `run --claim-only` and exclusive reservations | Refuse the legacy contract before creating a task, waiter or fence. An explicit request for exclusivity must never silently become advisory. |
| `run --fixture-write` and drivers requiring legacy task refs | Refuse with `EXECUTION_CONTRACT_UNSUPPORTED`. A compatible coding driver uses workspace publication. |
| `claim renew`, `claim release`, `claim reconcile` | Retain legacy command syntax for a compatibility release. ID-based mutations require a compatible legacy record. Reconciliation skips workspace checkouts and never grants a legacy waiter there. |
| `claim list/show`, task history/diffs, workspace status, intents | Inspection stays available. A newly initialized workspace has no historical claims. Status identifies first-registration origin and current regime. |
| Task apply, undo, retry and recovery | Route by the retained attempt and its contract. Workspace work uses existing workspace bases and validation. No legacy ref is manufactured. |
| `workspace cutover plan/status/export` | Report an already-workspace checkout and its origin. Legacy inventory is empty. There is no pending conversion. |
| `workspace cutover apply/resume/recovery` | Report already initialized or no matching cutover intent. Do not create or replay a fictitious migration. |
| `workspace authority prune` | Preserve the regime decision across idle release, restart, profile changes and path renames. |

Legacy checkouts retain the existing command behavior throughout the deprecation
window. Removing legacy mutation commands later requires a separate decision.

## Compatibility and rollback

Give the new marker an explicit format version. The 0.1.0a4 authority reader
accepts v1 and v2 only and refuses managed access to an unknown version. Writing
a v2 workspace marker would incorrectly advertise a reviewed cutover intent.
Pre-authority binaries ignore the lock protocol entirely and must be stopped.
A downgrade can therefore strand editable sessions until a compatible binary
is reinstalled. Preserve the marker, ledger and private trees across that interval.

Gate the default **by release**, after the evidence below and maintainer acceptance.
A corrective release can restore legacy selection for future first registrations
while retaining the reader and recovery path for all existing markers. A per-profile
switch could make the result depend on which profile first opened the checkout.
A per-checkout opt-in preserves choice but keeps a setup step on every fresh
clone. Keeping the legacy default indefinitely preserves compatibility but also
keeps queued claims on the normal coding path.

## Evidence required before rollout

- Run upgrade and downgrade/re-upgrade tests with a used legacy profile, a
  converted profile, a genuinely new key, a missing marker, and two profiles
  racing first registration. Inject a crash at each persistence boundary.
- Complete the planned multi-session soak across ownership loss, watcher pressure,
  publication and daemon restart. Require no lost edit, duplicate execution,
  resurrected waiter, or fallback to legacy on a workspace marker.
- In the Wave 13 planning run, 20,100 files passed the first task in both modes
  but failed clean reacquisition after release and after restart; a 300-file
  control passed. First registration avoids the legacy cutover checkpoint, but
  still needs a reliable handoff. Before rollout attach C24's measured handoff
  times and memory at 20k, 50k and 100k, plus drift and refusal results at the
  supported limit. [Existing scale data](../experiments/isolated-admission-c18-2026-10-30.md)
- Run an end-to-end coding task with a supported live driver on a fresh workspace,
  resume from another session, and inspect the resulting proposal. E2's provider
  cache and overflow checks measure adapter behavior separately.
- An opt-in development build can exercise the tests. Propose the default for a
  subsequent alpha once these gates pass; a stable release is not a prerequisite.

## Follow-up packages if accepted

| Package | Scope and size | Exit evidence |
| --- | --- | --- |
| G6 | M. Extend authority marker, maintenance and regime storage with durable registration provenance and inventory. | Old, missing, moved, pruned, new and ambiguous checkouts remain distinguishable across profiles and upgrades. |
| G7 | M. Initialize first-registration state under the physical lock, persist profile mappings, expose origin in status, and route launch through the existing workspace contract. | Concurrent starts, crash/replay, mixed binaries, explicit claims and sessionless retries meet the table above. |
| G8 | S. Run rollout/rollback and live evidence, update CLI help, release notes and compatibility timeline. | A release restoring the prior default can operate all existing workspaces; maintainer approves the recorded evidence and activation. |
