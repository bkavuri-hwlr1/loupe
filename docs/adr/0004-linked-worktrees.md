# ADR 0004: Linked Git worktrees for every editing task

- **Status:** Accepted
- **Date:** 2026-08-22
- **Decision owners:** Initial project maintainers

## Context

Path claims prevent the coordinator from intentionally scheduling overlapping
work, but claims alone do not isolate filesystem mutations. Two agents operating
in one working tree can still change the index, switch refs, leave generated
files, run formatters over broad paths, or observe one another's partial writes.
Even disjoint source changes can collide through Git index state or build output.

Full repository clones would isolate working trees but duplicate object storage,
lose a simple relationship to local refs, and add significant setup and cleanup
cost. Git linked worktrees provide separate worktree and index state while
sharing the repository object store.

Worktrees are not sufficient authority on their own. Git does not understand
planned scopes, queue fairness, fencing, task ownership, or publication intent.
The coordinator must combine worktree isolation with durable claims and trusted
validation.

## Decision

Every task that may edit repository content runs in its own coordinator-managed
linked Git worktree. The task does not edit the user's main worktree. Read-only
planning may inspect a trusted snapshot without an editing worktree, but it
cannot be promoted to an editing pass without acquiring a claim and preparing a
managed worktree.

### Preparation

Trusted Git code:

1. discovers and validates repository identity and common directory;
2. records the target ref and exact base object ID;
3. acquires or confirms the task's active fenced claim;
4. creates a coordinator-owned worktree path derived only from validated
   repository and task identifiers;
5. prepares a detached worktree at the recorded base commit;
6. records worktree identity and preparation outcome durably;
7. starts the agent only after the worktree is ready and ownership checks pass.

Preparation uses explicit Git argument arrays and a sanitized environment.
Prompts, repository names, branch display names, and model output never form
filesystem cleanup targets. The implementation accounts for an unborn target,
detached target, SHA-1/SHA-256 object format, existing linked worktrees, shallow
repositories, submodules, and dirty user worktrees according to documented
support policy.

The agent receives the managed worktree as its working directory and accesses
Git through bounded tools or a restrictive execution policy. The coordinator
does not assume the agent will remain within its declared scope.

### Validation

Before accepting a result, trusted code derives the canonical change set from
Git, including staged, unstaged, untracked, deleted, renamed, copied, binary, and
mode changes as applicable. It validates both source and destination of a rename,
rejects path escapes and unsupported nested repositories, and compares every
changed path to the task's effective fenced scope.

Model-reported file lists, tool logs, handoff summaries, and planned scopes are
useful context but are not validation authority. The validation record includes
the base object, result tree/commit identity, normalized changed paths, and
bounded patch/tree hashes needed for later revalidation.

### Result and integration

The default accepted result is an internal task branch created under a durable
publication intent. The target branch and user's main worktree are not modified
automatically. An explicit later integration operation rechecks:

- claim and fencing identity;
- publication/result identity;
- current target movement;
- exact changes and effective scope;
- target worktree safety;
- selected merge, cherry-pick, apply, patch, or future PR policy.

A task holding a publishing or active-integration reservation is not released
only because its process lease expires. Release requires target ancestry proof,
explicit discard, or another verified adapter-specific terminal condition.

### Cleanup and recovery

Cleanup is idempotent and bounded to a recorded, validated coordinator-owned
worktree. It refuses broad roots, symlink escapes, unrecognized occupancy, and a
worktree whose Git administrative identity no longer matches the recorded task.
Material data is not deleted while publication/integration state is ambiguous.

Startup reconciliation compares control records with Git's worktree registry,
managed paths, refs, and running process identity. Orphans are quarantined or
reported before removal. User-created worktrees and refs are never inferred as
safe cleanup targets from naming resemblance alone.

## Consequences

### Positive

- Editing tasks have independent filesystem and Git index state.
- Disjoint agents can operate concurrently without sharing partial writes.
- Shared object storage makes isolation cheaper than full clones.
- The user's main worktree can remain dirty without becoming the agent's editing
  surface.
- Branch-only publication makes results inspectable and recoverable.

### Costs and constraints

- Worktree creation, registration, pruning, and interrupted cleanup require
  robust reconciliation.
- Shared repository config/object state is still a trust surface; linked
  worktrees are not a sandbox.
- Some Git features and repository shapes require explicit support tests.
- Agents can still write outside the worktree if general shell/process access is
  granted; OS sandboxing is a separate concern.
- Disk usage from build artifacts and untracked files needs retention limits.

## Alternatives considered

- **One shared worktree:** rejected because claims do not isolate the Git index,
  generated output, or accidental broad edits.
- **A full clone per task:** deferred because it duplicates objects and makes
  local ref/result management more expensive without adding a hostile-code
  boundary.
- **In-memory patches only:** rejected for the general coding workflow because
  tools and tests expect a real filesystem and Git repository.
- **Container or VM per task:** valuable for hostile-code isolation but outside
  the first local cooperative-process boundary; it may be added as an execution
  adapter later.
- **Automatic merge into the target:** rejected as the default because target
  movement and integration conflict require a fresh explicit decision.

## Follow-up

- Build sanitized Git fixtures for hooks, aliases, filters, pagers, SHA formats,
  symlinks, submodules, renames, dirty state, and interrupted worktrees.
- Specify supported Git versions and repository features during packaging.
- Add kill-point tests around worktree creation, registration, validation,
  publication intent, ref creation, and cleanup.
