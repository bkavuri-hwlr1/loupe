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
   | Ordinary ignored directory | A private container; inventory children independently except dependency trees handled by their directory-level adapter below. |
   | Git-ignored `node_modules` directory | Select the private directory-level adapter without previewing descendants. Counts and bytes remain unknown until bounded materialization; no installer runs. Nonignored source is inventoried normally. |
   | Likely secret by path | Opening may omit the file with an exact-path `--approve-secret PATH` notice; an approved exact file plans a private copy with mode at most `0600`, under `0700` containers. |
   | Git-ignored `.venv` or `venv` directory | Select the bounded private portability adapter. Materialization verifies base-interpreter metadata and relocates supported launchers/configuration; the preview does not read file contents. |
   | Runtime symlink outside a supported dependency adapter | Refuse and describe its shape without following it. The dependency adapter separately validates contained links and narrowly pinned base-Python executable links. |
   | FIFO, socket, device, or other special file | Refuse and report its kind. |
   | Nested repository/worktree or recognizable bare repository | Refuse and mark descendants unexamined; no implicit submodule fetch or adapter. |
   | Cross-device directory | Require a dedicated adapter; do not recurse across mounts. |
   | Git or coordinator administration | Exclude `.git`, `.loupe`, `.llm-coord`, `.codex`, and `.agents` wherever encountered, without descending. |

   A refused directory explicitly reports `descendants_inventoried: false`.
   Its `directory_entries` is unknown (`null`), rather than an invented zero.
   File byte fields describe observed regular files only, never recursive
   directory sizes. Deferred descendants are excluded from planned copy totals;
   a complete policy plan may still contain explicit unexamined directories and
   require decisions. Selecting an ignored dependency tree's adapter does not
   consume its descendants from the global preview inventory budget or disable
   independent candidates. Mount, repository, symlink,
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

   An opaque-directory decision does not grant blanket secret consent or
   permission to run an install command. C2 materializes supported dependency
   trees through a fresh bounded, no-follow walk, with per-entry secret, special
   node, link, allocation, time, and stability checks. Likely credential files
   require exact-file consent; an approved descendant does not authorize its
   siblings. Unsupported layouts fail with a path-specific adapter message.
   Tracked source under the same path remains ordinary source and is never
   silently treated as ignored runtime.

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

Checks and command snapshots copy hydrated runtime privately. Supported
virtualenvs and Node dependency trees use bounded private adapters; launcher and
configuration relocation never rewrites arbitrary data. Legitimate ordinary
source fixtures that trigger secret screening copy privately without ordinary
artifacts or model access. Output filters actual copied credential values before
streaming, durable records, and hook file returns, preserving other diagnostics.
Runtime check evidence is process-keyed, so restarting invalidates that evidence
without storing raw runtime digests. No installer runs automatically. Per-tab
environment forwarding, additional protected-source adapters, runtime refresh,
and managed background processes remain outside this package. Publication is the
C3 dependency.

See [Isolated workspaces](../isolated-workspaces.md) for usage and limits.


## C7 follow-up decision (Wave 4)

Unapproved likely-secret runtime files are optional inputs. Opening an isolated
conversation succeeds while omitting each unapproved file and naming its exact
`--approve-secret PATH` in the notice. Omitted paths remain frozen runtime
exclusions; omission never falls back to primary checkout bytes. Exact consent
still applies to one new conversation, never a directory, resumed session, or
later conversation. No setup command runs implicitly. A check requiring an
omitted file can fail and the user can reopen with that exact approval.

Screened ordinary source fixtures receive identity-checked incoming updates
privately. If local bytes diverge, retain both versions and emit one path-only
conflict notice. Durable synchronization metadata contains identities and random
tokens, while incoming raw bodies are staged only inside the pinned private
worktree Git directory, outside ordinary artifacts and model file tools.
Recovery validates the private staged identity before any write, accepts only
the recorded before/after states, and refuses later local edits. The existing
8 MiB changed-path boundary remains; larger fixture transitions are deferred
with an exact-path notice while other source changes proceed. Closing the
session prunes that private Git directory and its staged incoming material.

Commands and checks keep writable disposable private copies. Bounded COW workers,
exact-metadata digest reuse, digest transfer from verified copies, bounded pinned
source-parent descriptors, and exact-content secret-classification caching remove
repeated work without sharing writable runtime or persisting secret bodies in a
cache. Changed metadata or bytes invalidates the corresponding evidence. All copy
workers drain before cleanup or error. The measured Loupe/APFS repeated check
falls from about 3.03 seconds to 1.43–1.47 seconds, below the 1.5-second target;
actual-value output screening remains before events and durable check output.

## C8 follow-up decision (Wave 5)

Runtime refresh uses stat receipts at caller-owned safe points. Opening exact-file
secret consent applies to subsequent stable copies of that same runtime path;
no resumed session can expand consent. Private divergence retains the local
version and emits one path-only conflict notice per observed primary change.
New ignored entries retain normal hydration budgets and adapter boundaries.
Runtime bytes and content digests never enter the refresh journal or source
ledger. Prepared private root transitions recover by recorded inode/stat
receipts, recognizing only the before and after state; ambiguous later writes
are preserved. Unrecorded staged generations are disposable. When publication
authority is attached, installed receipts remain through staging cleanup until
that authority acknowledges them; later private changes block acknowledgment.

A retained proposal pins an immutable private generation and its check
configuration. Later live-runtime refresh does not replace those inputs for
`/apply`. Activity guards reserve command/check/hook snapshots across execution;
staging and large dependency copies do not hold the shared publication lock.
Generation directories are owner-only, carry the session ownership token, and
are removed with the workspace once live work and held proposals release it.


## C10 follow-up decision (Wave 6)

A profile may explicitly approve one installer argv per ignored, untracked
`.venv`, `venv` or `node_modules` root in `[isolated.rebuild]`. The quoted
`".venv"` key is required in TOML. The preview reports `rebuild` without reading
the primary dependency tree; it can rebuild missing or unsupported primary
layouts. No setting means no installer. Network access defaults off; only an
explicit `network = true` enables outbound IP for that installer. Ordinary
commands and checks retain their default network refusal.

Opening completes ordinary source/runtime stability checks before installation.
The installer runs through the real command sandbox with immutable private
source and writes limited to its dependency root and private disposable home.
It inherits only the minimal command environment, including its search path;
terminal variables and daemon-held provider credentials are not forwarded. Existing source-policy and actual-private-value
output screening apply before an opening notice can persist diagnostics.
Timeouts (60 seconds by default, at most 300) and bounded entry, byte, allocation,
depth, per-file and metadata-observation limits apply. Observed size enforcement
is not a filesystem quota. Failure removes the partial private root and still
opens the conversation with a notice.

A token-bound creation journal records frozen argv/network/timeout policy and
compact stat-only completion receipts. C8 records opening exact-file consent
and copied-runtime receipts before that journal. It keeps
runtime bodies and installer output out of durable recovery metadata. Recovery
removes partial roots and its deterministic disposable home, then retries an
uncompleted installer. Later unexpected writes to a recorded completed tree
require attention. Rebuilds reuse existing creation phases and G2's physical
checkout journal; confirming readiness acknowledges that authority only after
source and runtime are consistent.

C8 freezes rebuilt roots as private inputs: later primary changes and ignore-rule
edits cannot replace them, and safe-point refresh does not run installers. Other
runtime paths retain their opening consent and normal refresh behavior. Held
proposals keep immutable private dependency generations for apply. Worktree and
Git-registration cleanup also removes journal-owned installer homes and rebuilt
runtime. The existing creation publication barrier remains held during rebuilds;
this bounded delay is an explicit tradeoff, rather than a change to authority
or publication ordering.
