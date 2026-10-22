# Moving a checkout from legacy claims to workspace execution

An ordinary Loupe upgrade keeps each checkout in the `legacy` regime. Installing
this version does not migrate a checkout, cancel its work, change its refs, or
remove its history. Explicit cutover advances one physical checkout through
`legacy → transition → workspace`. This is irreversible: there is no command
that restores legacy editing authority or deletes the new execution ledger.
[ADR 0010](adr/0010-cross-profile-publication-authority.md) defines the contract.

In `workspace`, coding tasks use durable workspace executions and advisory
intents without legacy editing claims. Sessionless `loupe run` creates a durable
one-shot shared session; its conversation and history remain inspectable after
it settles and closes. An unresolved publication/recovery state keeps that
session available for recovery instead of closing it early. Interactive chats
retain their existing shared or isolated workspace choice.

## Preview every mapping before applying

Stop old writers and upgrade every participating profile. Older G2 binaries
reject the new marker format; pre-G2 binaries do not participate in physical
checkout authority. All profiles still use the same account-wide physical lock.

```console
loupe workspace cutover plan .
loupe --json workspace cutover plan .
```

The normal preview shows the regime, generation, readiness and exact
`checkpoint`, then every mapping (target, adapter, path and registration
provenance/count), every disposition (kind, ID, state, default and allowed
actions), and every blocker (kind, ID and reason). It omits raw task/execution
payloads. `--json` retains the complete evidence envelope, including the
physical key and registration provenance. Planning never starts the transition
or repairs authority. It covers relevant repository
keys, target refs and adapters instead of selecting only the latest registered
path. An incomplete inventory or ambiguous overwritten clone registration blocks
conversion; Loupe does not guess a historical clone from its current spelling.

The checkpoint also binds a stable source and index observation. Observation is
bounded to 20,000 paths, 16 MiB per file, 64 MiB of file contents in total and
30 seconds. Protected/runtime exclusions apply, and files matching secret
signatures contribute metadata only. The preview exposes one compound digest,
not source bodies or individual file hashes. An unstable or oversized snapshot
refuses cutover with `CUTOVER_BLOCKED`; let concurrent writes settle before
reviewing a fresh plan.

Review all mappings and dispositions. The normal choices preserve queued work
as explicit legacy history and let active work drain or recover. Publishing or
integration with ambiguous side effects must be reconciled first. Completed and
held work retain their history, evidence and internal refs. Existing held apply
and undo remain subject to their supported proposal type, exact bases and checks.

## Begin one explicit cutover

Copy the exact checkpoint from the reviewed preview:

```console
loupe workspace cutover apply . --checkpoint CHECKPOINT
```

`apply` is the explicit irreversible action. It does not ask a second approval
question. An optional `--intent CUTOVER_ID` supplies a stable identity for that
same attempt. A stale checkpoint is refused before mutation; obtain and review
a new complete plan when the checkout or retained legacy state has changed.

To cancel particular queued claims explicitly, name each one:

```console
loupe workspace cutover apply . --checkpoint CHECKPOINT \
  --cancel-claim CLAIM_A --cancel-claim CLAIM_B
```

Only the selected queued claims receive cancellation dispositions, with durable
reasons and retained evidence. Active work blocks `apply` before transition:
let its actual worker drain/recover, or cancel the task and wait for the worker
to stop, then obtain and review a fresh plan.

Claim-only work has no worker to settle a `task cancel` request. Release that
reservation explicitly while the checkout is still legacy:

```console
loupe claim release CLAIM_ID --reason cancelled
```

Do this before applying cutover. Lease expiry alone does not discard retained
checkpoints or publication evidence. Unselected queued work is retained and never
silently activated after cutover. It does not block new workspace tasks. No flag
bypasses an ambiguous publication,
integration or physical binding.

The transition gates new legacy claim requests and waiter activation across the
complete mapping set. A durable intent binds the selected dispositions and
checkpoint. The marker and database advance monotonically; a crash cannot
restore an earlier regime or activate both execution contracts on one checkout.

The source and index part of the checkpoint binds only the reviewed plan to
`apply`. Once the intent is saved, edits, staging and commits no longer block
completion. The remaining stages and `resume` require only the checkout's
retained legacy history to stay unchanged: its claims, tasks, executions,
publications, workflows, batches and ownership journals. From the moment the
intent is saved, this profile treats the checkout as `transition`, even if a
crash interrupted the cutover before its transition marker was written.
`resume` refuses with `CUTOVER_CHECKPOINT_STALE` if retained legacy history
changed, or if another profile owned the checkout after the intent was saved.
Such a cutover needs operator attention.

## Inspect or resume the same intent

```console
loupe workspace cutover status .
loupe workspace cutover resume . --intent CUTOVER_ID --checkpoint CHECKPOINT
```

Keep the original intent and checkpoint returned for that attempt. `resume`
validates them against the durable intent and continues its exact journal; it
does not start another conversion or use a newly generated plan's checkpoint.
Status reports the regime, generation, blockers and current intent, including
whether existing work still needs to drain or recover. Status and export inspect
retained metadata without rescanning source or issuing a fresh approval
checkpoint. When an intent exists, status returns its saved original checkpoint;
`checkpoint_observed` remains false and `current_checkpoint` is null. Obtain a
new source-bound checkpoint from `plan`, and use the original plan checkpoint
when resuming an existing intent. Ordinary read/status operations stay available
during transition even when source is changing or exceeds the preview budget.

After the marker and database confirm `workspace`, new coding work uses the
claim-free contract. Legacy fixture/ref workflows must use a checkout that still
has the legacy regime, or export/inspect their retained evidence. There is no
`--legacy` bypass on a converted checkout. `loupe claim` mutation is deprecated
for one compatibility release and refused in transition/workspace; claim and
task inspection remain available.

## Read and explicitly export old history

```console
loupe claim list --repo .
loupe claim show CLAIM_ID
loupe task show TASK_ID
loupe task events TASK_ID
loupe task diff TASK_ID
loupe workspace cutover export . --out legacy-history.json
```

Export writes the daemon's transparent metadata/history/ref envelope to a new
`0600` JSON file. It refuses an existing file or symlink and never overwrites
retained evidence. The output path stays local to the CLI. Export includes
locators and dispositions, while private runtime, provider conversations,
credentials and source bodies remain outside that envelope. Its `checkpoint`
is null: metadata export does not run the source observation used by `plan`,
`apply` and `resume`. It does not remove
the original database rows, checkpoints or Git refs. Keep the original profile
and repository available for supported diff/apply/undo operations.

## Stable refusals and capabilities

| Code | Meaning and next action |
| --- | --- |
| `CUTOVER_CHECKPOINT_STALE` | The reviewed snapshot or saved intent checkpoint does not match. Inspect status and review the complete plan before a new attempt. |
| `CUTOVER_BLOCKED` | A mapping, live worker or unresolved effect prevents this step. Follow the listed disposition/recovery evidence; do not erase it. |
| `EXECUTION_CONTRACT_UNSUPPORTED` | This mutation belongs to another regime. Inspect/export its retained record, or use an independent checkout whose regime supports that workflow. |

Checkpoint/blocker refusals exit with status 75; incompatible-contract mutation
exits with 65. JSON errors retain the stable code and diagnostic details. Existing
physical-owner, foreign-recovery and changed-identity errors keep their meanings.

The daemon advertises additive `workspace.cutover.v1`,
`workspace.execution.claim_free.v1` and `legacy.history.read.v1` capabilities.
The versioned RPC envelope remains compatible; capability support never bypasses
the checkout's final regime/ownership validation. Restoring an old binary cannot
roll the marker or new ledger backward. General anti-rollback protection against
an in-place manual database restore remains outside this bounded contract.

The F3 comparison still keeps indexing off and F4 gated. E2's live provider
checks remain deferred; cutover needs no billed model request.
