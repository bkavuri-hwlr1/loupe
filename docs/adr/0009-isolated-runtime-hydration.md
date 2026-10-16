# ADR 0009: Bounded isolated-runtime hydration

Status: accepted (Wave 2 C1, 2026-10-14)

## Context

An isolated worktree needs runtime files that Git deliberately omits. Reusing
the primary checkout's `.env`, virtual environment, or `node_modules` through
shared links would let isolated commands modify the user's runtime. Blindly
copying ignored directories would also import secrets, repository internals,
special files, and unbounded caches. Sections 18–19 of the workspace plan
describe the eventual creation, execution, synchronization, and recovery model.
This decision selects its first independently useful piece: a read-only plan.

## Decision

1. **C1 produces a preview, never an isolated session.**
   `loupe workspace hydrate [PATH] --plan` inventories the selected repository's
   actual filesystem and current Git ignore rules, including local/global
   excludes. It works without a daemon, account, registered checkout, or saved
   profile. Human output summarizes all action counts, planned candidate bytes,
   unknown descendant sizes, and incomplete reasons, with bounded path samples.
   `--json` exposes the complete structured plan, including every entry and its
   observations and budgets. It creates no directories,
   writes no files or databases, copies no content, and records no consent.
   The already approved Wave 2 scope accepts these conservative minimum rules;
   no additional setup approval is required to inspect a plan.

2. **Inventory is metadata-only and bounded.**
   Walk with directory descriptors, `lstat` semantics, and `O_NOFOLLOW`. Never
   open runtime file contents; report paths, kinds, apparent/allocated bytes,
   observed device/inode/mode/size/timestamps, decisions, and reasons. NUL-safe,
   bounded Git `check-ignore` batches distinguish ignored runtime from tracked
   and ordinary untracked source. Ignore-rule contents, matching patterns,
   raw runtime digests, and symlink targets do not appear in output. No Git
   hooks, filters, prompts, network access, or optional index writes run.

   Defaults bound the complete checkout inventory to 20,000 entries, 4 MiB of
   charged path/metadata storage, depth 64, and 10 seconds, including Git pipe
   output and runtime. Enumeration stops with an incomplete plan when a limit
   is reached; no arbitrary enumerated prefix is presented as complete. The
   API has explicit ceilings. Path ordering and budget selection are lexical.
   Case/Unicode aliases require attention on every filesystem.

3. **Every candidate has its own policy decision.**

   | Actual entry | Preview decision |
   | --- | --- |
   | Ordinary ignored regular file | Prefer a verified COW clone; otherwise a private byte copy within all budgets. |
   | Ordinary ignored directory | A private container only; inventory every child independently, except explicit deferred runtime trees below. |
   | Git-ignored `node_modules` directory | Require a rebuild or trusted directory-level adapter without enumerating descendants. Child counts and bytes are unknown; none of its contents are copy candidates. A nonignored directory with this name is inventoried normally. |
   | Likely secret by path | Require exact-file consent; an approved file plans a private copy with mode at most `0600`, under `0700` containers. |
   | `.venv` or `venv` directory | Require a rebuild or trusted portability adapter; absolute shebangs and embedded primary paths cannot be generically rewritten. Do not read files to guess portability. |
   | Any symlink | Refuse in this minimum version; explain whether it is absolute, escaping, or contained without following it. Contained-link recreation is deferred. |
   | FIFO, socket, device, or other special file | Refuse and report its kind. |
   | Nested repository/worktree or recognizable bare repository | Refuse and mark descendants unexamined; no implicit submodule fetch or adapter. |
   | Cross-device directory | Require a dedicated adapter; do not recurse across mounts. |
   | Git or coordinator administration | Exclude `.git`, `.loupe`, `.llm-coord`, `.codex`, and `.agents` wherever encountered, without descending. |

   A refused directory explicitly reports `descendants_inventoried: false`.
   Its `directory_entries` is unknown (`null`), rather than an invented zero.
   File byte fields describe observed regular files only, never recursive
   directory sizes. Deferred descendants are excluded from planned copy totals;
   a complete policy plan may still contain explicit unexamined directories and
   require decisions. Deferring an ignored dependency tree does not consume the
   global budget or disable independent candidates. Mount, repository, symlink,
   and metadata-race checks still apply before a dependency directory is deferred.
   No directory-level secret approval authorizes its descendants. Repeated
   `--approve-secret PATH` flags only preview consent for the exact file in
   this invocation. Name classification is conservative, not proof that an
   unknown file contains no secrets; all ignored runtime remains runtime-only.

4. **Copy policy preserves the isolation promise.**
   The future materializer must probe `clonefile`/`FICLONE` for the actual
   source/destination devices and verify that destination writes cannot change
   the source. C1 deliberately reports COW as `unprobed` and the destination
   device as unknown: probing would require writes. Use no hardlinks, shared
   directory symlinks, silent omissions, automatic setup commands, or sync-back.

   Default per-file apparent size is at most 256 MiB; aggregate apparent and
   estimated copy allocation budgets are each 2 GiB. The allocation estimate
   conservatively uses the greater of apparent size and observed source blocks;
   it is a preview, not a filesystem allocation guarantee. COW does not waive
   copy-fallback budgets. Exceeded entries require a decision, while smaller
   independently eligible files retain explicit candidate actions.

5. **A preview is not an executable source snapshot.**
   Compare descriptor/path metadata around observations, refuse directory-swap
   races, check observed `.gitignore` metadata, and repeat ignore classification.
   Changes, inaccessible entries, path aliases, timeouts, and truncation mark
   the plan incomplete and disable every candidate copy action. This does not
   claim a globally atomic snapshot or content equality from timestamps.

   C2 must first cross the existing publication/recovery barrier, pin the exact
   source revision plus complete ignore/attribute/config/exclude identities,
   rescan, and verify each no-follow copy/clone against stable observations.
   Replacing an external exclude file with equivalent rules can evade this
   metadata preview's semantic recheck; it cannot authorize a later copy. C2
   also owns destination collisions, actual allocation/free-space checks,
   protected-source handling, journals, and creation/recovery/cleanup.

   A directory-level rebuild decision is not permission to copy its opaque
   contents, follow links, or run an install command. C2 must either leave the
   runtime directory absent with an explicit user-visible decision, or obtain
   an approved trusted adapter/rebuild policy under the normal command and
   secret boundaries. Copying instead requires a fresh complete descendant
   inventory with per-entry secret/special-file/symlink decisions and all byte,
   allocation, time, and metadata limits. A larger tree cannot silently inherit
   blanket secret consent or bypass those limits. Tracked source under the same
   path remains part of ordinary source materialization and collision checks.

6. **Execution and publication retain existing boundaries.**
   Future hydration bytes are runtime-only: exclude them from model tools,
   indexes, prompts, artifacts, change batches, and ordinary publication. Shell
   commands remain a cooperative same-user boundary and require the protected
   input/output policy in section 19. Environment rewrites require exact hydrated
   equivalents; do not rewrite arbitrary embedded paths. Publish ordinary source
   edits through the existing validated batch protocol, with required checks,
   base comparison, and recovery. C1 leaves isolated session creation refused;
   C2 and C3 implement creation and publication respectively.

## Consequences

- Operators can inspect the real runtime costs and unsupported entries before
  creating anything; no project manifest is required or generated.
- Large dependency trees may need a larger explicit inventory budget. A bounded
  failure is visible and never silently becomes a shared runtime link.
- Virtual environments and even contained symlinks are deliberately conservative
  in this minimum. Framework-specific portability and link reconstruction need
  separate adapter tests before isolated sessions use them.
- Exact source materialization, COW capability probing, content stability,
  protected command output, durable consent, runtime refresh, and crash recovery
  remain work for C2/C3; a complete preview alone enables none of them.

## C2 implementation (Wave 3)

The session materializer now implements exact ordinary-source snapshots,
private ignored-file hydration, exact-file secret consent, pinned source-policy
identities, descriptor-based stable copies, a disposable same-device COW probe,
allocation/free-space checks, and durable creation/recovery intents. Closing a
session retires its worktree and registration once running tasks and held
proposals release its runtime. Recovery archives retain ordinary source evidence,
including late replacements and open-descriptor writes, but copied runtime,
dependency roots, and screened source are disposed through no-follow descriptors.
Ambiguous source paths are preserved and quarantined with the archive location.
Ready workspaces restore frozen runtime exclusions
on restart. Source symlinks use the C5 contained-link policy; runtime symlinks
remain deferred.

Checks and command snapshots copy hydrated runtime privately and withhold their
output. Runtime check evidence is process-keyed, so restarting invalidates that
evidence without storing raw runtime digests. Opaque dependency trees remain
absent with visible notices; no rebuild is run automatically. Per-tab environment
forwarding, protected-source adapters, runtime refresh, and managed background
processes remain outside this package. Publication is the C3 dependency.

See [Isolated workspaces](../isolated-workspaces.md) for usage and limits.
