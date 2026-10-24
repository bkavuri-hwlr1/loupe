# Wave 9 C13 workspace switching verification

The initial implementation/test freeze is
`c2d2b0b666bf53d137a2a233df32f2a301fa5aa2`. Normal evidence/ancestry merge
`550f40cc65baf78f9899a28864b5ae7bf9cce673` adds final C15/C16 documentation.
All **454 tracked source, test, configuration, CI and script objects** are
unchanged across that merge. The sorted path/mode/Git-object manifest has SHA-256
`81f668df7cbd8533c9e4642e17eb60c68d78ce16f53f6ff896c64acd1d408a27`.

Migration 31 remains
`4d1a7425550a67cd314e1a329174982727718e163c28e1c061a29c1ea1a7e130`.
Migration 32, `durable_workspace_mode_switches`, is
`6baeb333ba0b8854168515ad7079599027914f83e5cdf12123bca73f1bf3c5ae`.
The reviewed upgrade preserves old isolated rows, runtime holds, compact conflict
steps and foreign keys, gives existing sessions binding generation 1, and allows
multiple isolated workspaces while creation keys remain unique.

The first covered full run was interrupted after two obsolete `SELECT *`
upgrade fixtures failed: the G2/schema-27 inventory compared a newly added
session binding column, and the schema-26 rebuild fixture copied 12 current
isolated-workspace values into an 11-column legacy table. The accompanying
validation-only correction captures each legacy schema's original column
projection, compares **all** those columns after upgrade/recovery, and separately
checks the new creation-key and binding-generation defaults. Both corrected
fixtures plus migration checks passed **32 tests**. No production, harness,
configuration or script bytes changed from `c2d2b0b`; exactly these two test
objects intentionally differ from the original 454-object manifest. The
interrupted diagnostic log remains preserved separately.

The first restart was stopped early to close one existing-target admission
consistency issue. A raw generation-1 `session.open` retry selected the current
isolated binding before checking newly requested runtime consent. Both current
binding and exact creation-key fallback now check consent, session ownership and
checkout before opening an authority mutation journal, then recheck admission
and retain full worktree/token validation inside the journaled path. Seven real
RPC/disposable-authority cases passed after the fix; five failed before it. The
regressions also prove rejected requests leave ordinary authority pins usable,
open no mutation journals, and never copy an unapproved file. This refinement
changes only `workspace/isolated.py` and its switching regression test in
addition to the two legacy fixture corrections; migration checksums, harnesses,
configuration, scripts and all other production source remain unchanged.

The final source is frozen for the combined covered correctness run, excluding
`index_scale` and `admission_scale`. Those unchanged production gates run
separately without coverage. Full-run and scale results are recorded in [PR #81](https://github.com/bkavuri-hwlr1/loupe/pull/81)
when verification completes; no final readiness claim depends on a partial run.

## Focused evidence

- After the final admission guard, the combined switching, CLI, isolated
  creation/cleanup, dependency rebuild, migration and immutable-execution
  concurrency group passed **202 tests**. Ruff and strict mypy also passed.
- The last cancellation regression failed before that fix: cancelling the RPC
  waiter allowed forced idle release to change physical authority to
  `observed_only` while its worker continued. It passed after the pin moved inside
  the synchronous worker and remained `owned` until the real worker returned.
- All forward switch states have actual Python `-I` daemon-death/restart tests
  with `install_test_authority` and disposable authority roots. Additional process
  deaths cover rollback-pending, rolled-back and attention-required states.
  Inner creation faults preserve the recorded target ID and never allocate a
  replacement target. Ownership epoch mismatches require explicit attention and
  rollback rather than accepting a stale target bootstrap.
- Scripted model turns read and write after both switch directions, preserve the
  original native messages, clear saved coordination cursors/read observations,
  receive a fresh target bootstrap and use the target execution workspace. Old
  binding generations cannot launch tasks. The existing actual CLI stages from
  a fresh generation receipt after switching back to shared mode.
- Every source disposition is exercised. Checked publication rejects failed
  required checks and source/runtime changes during checks. A deterministic
  slow-check barrier accepts durable rollback or closes the owner, then proves
  no publication or cancelled-intent revival occurs. The publication barrier is
  free while checks run. Only the current workspace's proposals enter its
  publication; earlier workspaces' proposals/runtime holds remain unchanged.
- A normal proposal created in an isolated workspace is applied after a shared
  switch and changes to shared `.env`, ignored dependency inputs and daemon
  environment. Checks still use the original retained private inputs, proposal
  code and rewritten C9 session paths. Status records the actual action batch,
  and exact-base undo succeeds.
- Portable Git binary patches round-trip text, executable mode, binary bytes and
  symlink targets. A real partial-write process death leaves no final digest name
  poisoned; restart exports and applies the patch. Existing unknown artifacts
  are preserved. Exact JSON sidecars contain their own bodies and identities.
- Real disposable-authority probes and repository regressions verify failed
  targets scrub copied private inputs, preserve unknown ordinary source and
  restore the source's clean authority. Replacing the target owner token instead
  preserves its bytes and keeps recovery blocked. Clean/disposed workspaces
  remain eligible for safe close-time cleanup; retain and unresolved proposal
  choices keep their explicit pins.
- Ruff passes for all source/tests; strict mypy passes for **132 source files**.
  Providers/installers are local scripted fixtures. No billed model calls or
  real account authority are used. Admission, hydration, batch and scale limits
  are unchanged.

C15's [final checks](wave9-c15-runtime-export-2026-10-24.md) and C16's
[full-hour storage evidence](bounded-sync-journals-c16-2026-10-24.md) remain
separate measurements of their frozen implementations.
