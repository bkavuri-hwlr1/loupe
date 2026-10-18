# Implementation status

Loupe is a provider-neutral terminal coding agent whose independently
started local instances coordinate when they work on the same repository.
The durable execution foundation now includes an interactive terminal UI;
the remaining planned capabilities are listed below.

## Implemented

- A metadata-only hydration preview for isolated workspaces:
  `loupe workspace hydrate [PATH] --plan` (or global `--json`) walks actual
  ignored runtime entries with bounded no-follow traversal and passive Git
  ignore classification. It reports sizes, copy/COW candidates, budgets and
  exact-file secret consent, bounded private dependency-tree adapters, and
  unsupported symlink, special-file and nested-repository decisions. Exact `--approve-secret PATH` flags preview
  per-file consent without saving it. Truncated or changing inventories
  disable candidate copies. This command needs no daemon and copies nothing.
  See [ADR 0009](adr/0009-isolated-runtime-hydration.md).

- Isolated session creation and runtime execution (Wave 3 C2): `loupe chat` and
  `loupe session open` accept `--workspace isolated` and repeatable exact-file
  `--approve-secret PATH` consent. Each session gets a private linked worktree
  containing current source working bytes. Ordinary source files that trigger
  secret screening can be copied privately for local tests without exposing their
  bytes to model tools or ordinary artifacts. Stable no-follow copies and bounded
  retries handle source saves during creation. Ignored files and supported
  `.venv`, `venv`, and `node_modules` trees use private copies or verified COW,
  with exact-file secret consent and bounded directory-level adapters.
  Runtime exclusions survive ignore-rule changes and daemon restart, including
  case and Unicode-normalization aliases. Commands and checks keep useful output
  while filtering actual copied credential values before model results, events,
  check records, and hook returns. Closing removes the private worktree and Git
  registration once running work and held proposals release it. Recovery preserves
  ordinary source evidence and late user changes while scrubbing owned runtime
  and screened-source copies from archives. See
  [isolated workspaces](isolated-workspaces.md) for supported
  layouts, bounded adapters, and recovery behavior.

- Isolated source publication and incoming synchronization (Wave 3 C3): shared
  batches and current primary source reach the private worktree at safe points.
  Metadata-assisted scans skip unchanged bodies outside the publication lock;
  a racing publication invalidates the scan before it can apply. Source saves or
  unavailable paths defer synchronization with a bounded path-only notice while
  preserving the last accepted version and allowing the task to continue.
  Three-way text merges retain local edits; overlapping, binary, symlink, and
  nonempty-directory conflicts remain durable without blocking unrelated incoming
  paths. Checked source deltas publish through the shared batch journal, while
  runtime stays excluded. Normal-mode proposals freeze independently of later
  private edits; apply and undo support restart and private runtime checks.
  A closed session retains its runtime through held apply, then retires it;
  exact-base undo remains available after that private cleanup.
  Incoming journals recognize exact before/after states, and a private catch-up
  failure cannot turn confirmed primary publication into a failed task.

- Faster isolated checks and incoming private fixtures (Wave 4 C7): fresh
  writable runtime copies use bounded verified COW and descriptor identity scans.
  Repeated full checks on this checkout's 6,576-target `.venv` took
  1.428/1.452/1.471 seconds, down from 3.032/3.033/3.037 seconds. Screened fixture
  changes reach an unchanged private copy through identity-only journals and
  private Git staging; divergence preserves private bytes and reports the path
  once. Incoming changes larger than 8 MiB defer that path. Unapproved optional
  secrets are omitted with an exact consent notice. See
  [measurements](experiments/isolated-runtime-c7-2026-10-17.md) and
  [ADR 0009](adr/0009-isolated-runtime-hydration.md).

- Verified shared-task workflow: named checks in disposable worktrees, explicit
  dependency setup, bounded replayable subprocess output, required-check gating,
  retained review proposals, task diffs, apply, undo, and cancellation. Shared
  overlays and batch journals support directory creation, regular text-file
  deletion and renames, bounded binary replacement from base64, and contained
  relative symlink creation, retargeting and deletion. Binary and symlink edits
  retain exact bases, survive checkpoints, run in typed check/command snapshots,
  and use the journal for publication, undo and crash recovery. Configuration
  is owner-local and frozen per attempt.
  See [the workflow guide](verified-workflow.md) for limits and commands.

- A Python 3.12–3.14 package with `loupe` and `louped` entry points
  (`llm-coord` and `llm-coordd` remain compatible aliases),
  locked development dependencies, and macOS/Linux CI.
- Bare `loupe` opens a local conversation interface before authentication,
  repository registration, or daemon startup. `/login` connects Codex through
  ChatGPT OAuth, or Anthropic/OpenAI through a hidden API-key prompt; connecting
  later remains available. Profile-local preferences remember the provider and
  model. `/provider`, `/model`, `/accounts`, `/logout`, and `/cd` manage the
  current connection and project. Provider/project changes wait for active work
  and start a fresh conversation; first use registers an existing Git project.
  Saved API keys stay outside the project and are loaded per task without RPC
  transport or inclusion in prompt history.
- Account-aware model discovery for Codex subscription, OpenAI API, and Anthropic,
  with a searchable `/model` picker and model-specific `/effort` choices.
  Offline/reference lists are labeled separately from account-confirmed results.
  Effort is saved with the profile choice, durable session, and execution launch
  recipe, so concurrent sessions, queued tasks, and recovered work retain their
  own setting. Provider adapters send only supported reasoning/thinking options.
- A Rich conversation view with complete streamed assistant text,
  provider-exposed reasoning, tool arguments/results, final summaries and usage.
  Large transcript fields are chunked, replay is ordered, terminal controls are
  removed, and piped/`--plain` output works without the terminal editor.
- A Prompt Toolkit composer with multiline input/paste, history, a searchable
  slash-command menu that opens while typing and displays command descriptions,
  session commands, safe task reattachment and an offline `loupe demo` tour.
  Pending questions have stable identities so replay cannot answer an old
  question on behalf of a new one. Active-task and interrupted-launch sessions
  preserve their resume credentials on exit.
- Private, profile-scoped XDG state with owner-only directories and files.
- A singleton Unix-domain-socket daemon using a bounded, versioned,
  four-byte-length-prefixed JSON protocol.
- Three independently migrated SQLite stores: required control and optional
  knowledge/vectors. With indexing off, optional stores are not created; existing
  files are preserved. The control database uses WAL, `synchronous=FULL`,
  transactional checksummed migrations, integrity checking, and an atomic
  online-backup API. `loupe db backup [--out DIR]` exposes verified snapshots of
  control and optional stores that already exist, with a fresh timestamped
  `0700` directory, `0600` files and printed integrity/hash/schema metadata.
  Absent optional stores appear under `omitted`; backup, status and integrity
  checks do not create them. A running-daemon integration test confirms the
  latest committed task survives the snapshot.
- Opt-in local lexical discovery (Wave 4 F1): `loupe index
  status|build|rebuild|drop` and `loupe search lexical QUERY [--explain]` expose
  immutable source chunks with revision, identity, line and generation citations.
  `[knowledge] index = "off" | "lexical"` defaults off. See the
  [lexical search guide](lexical-search.md) for setup and source boundaries.
- Deterministic code graph (Wave 5 F2): `loupe graph neighbors NODE [--edge TYPE]
  [--depth N]` exposes Python definitions, Python/JavaScript/TypeScript imports,
  test-import relationships and accepted-batch links. Graph rows update with
  lexical generations, cite exact source identities/parser versions and remain
  outside coordination authority. Per-file parse failures are nonfatal; current
  exclusions/private content stop traversal. See the [code graph guide](code-graph.md).
- `loupe db prune --older-than 30d` defaults to a dry run; `--apply` removes
  old model previews/deltas, tool results, command/check chunks and retained
  check text, slims settled execution checkpoints, then runs SQLite `VACUUM`.
  Checkpoints drop provider conversations already saved to their session and,
  once published, the file contents now in the checkout. Task records, final
  answers, summaries, usage and check metadata remain inspectable. Held
  proposals, live claims, unfinished finalization, uncertain checks and
  unresolved recovery journals prevent pruning. Malformed checkpoint phases
  protect their task while other settled tasks can still prune. Each affected
  task receives a durable audit event; repeating cleanup is safe. On a copy of an 18.8 MB
  profile database, pruning everything settled over a day ago left 5.6 MB:
  checkpoints were 8.1 MB of it and tool output 3.4 MB.
- `loupe config show` reports the active daemon's effective settings and frozen
  source provenance, redacting secret-bearing values and distinguishing saved
  changes pending restart. `config set KEY VALUE` validates with the existing
  loader and durably replaces the private TOML file atomically. Invalid edits
  preserve its bytes and metadata, including malformed or unsupported typed
  values; TOML dotted/quoted keys support MCP names and header punctuation. Concurrent
  profile setters retain each
  other's updates. Config files are shared by profiles and edits activate after
  daemon restart. Formatting/comments are rewritten on successful edits.
- Repository registration that derives a stable local or normalized-remote
  coordination identity.
- Durable repository heads, task records, claims, scope rows, events, outbox
  records, and publication intents in the control database.
- Strict scope normalization and overlap checks. Unsafe paths, parent escapes,
  `.git` paths, drive paths, and UNC paths are rejected. Contention follows the
  working tree's real path identity: on a case-insensitive filesystem (detected
  from Git's own `core.ignorecase` probe and recorded at registration) two
  spellings that differ only by case name one directory and therefore overlap.
- Transactional coordination with optimistic overlap for eligible shared
  and isolated session tasks, FIFO overlap fairness whenever either claim is exclusive, concurrent
  disjoint work, per-repository monotonic fences, 10-minute launch leases,
  90-second work leases, compare-and-swap renewal, expiry reconciliation, and
  atomic release plus waiter activation.
- Publication reservations that validate trusted changed paths against scopes,
  persist an idempotent intent before a Git side effect, and remain reserved
  until an explicit integration discard or confirmation.
- A hardened Git broker for repository inspection, linked detached worktrees,
  porcelain-v2 change parsing, binary patch hashing, materialized index trees,
  scope validation, and ref compare-and-swap publication.
- Crash recovery that resolves executions an earlier daemon boot abandoned
  before the new boot serves any request. On the linked-worktree path, each
  attempt is decided against ground truth: its publication intent names the reference
  swap and the commit it expected to replace, and the repository says what
  actually happened -- rather than against elapsed time.
- Human and JSON CLI commands for initialization, doctor checks, daemon
  lifecycle, database inspection, repository registration, tasks, and claims.
  A finished task attempt is advanced explicitly with `llm-coord task retry`;
  a terminal claim is never handed back as though it still granted authority.
- Multi-terminal orchestration tests give the fixture's polling client 60 seconds
  per RPC. A macOS CI stall timed out `_Cluster.rpc("task.list")`; a regression
  now stops the real daemon for six seconds during that call and verifies it
  completes after resume. Product clients retain their five-second default,
  and uncertain mutations are not retried.
- A provider-independent harness/driver seam. The lifecycle owns the claim, worktree,
  validation, publication, and recovery; a driver only decides file contents,
  and only through a tool broker bounded by the claim. Drivers declare their
  capabilities, including whether they enforce scope themselves.
- A bounded model tool surface: `list_files`, `read_file`, `search_text`,
  `read_diff`, `write_file`, `apply_patch`, `validate_changes`, `finish_task`,
  and `ask_user` when a session is attached. Shared workspaces additionally
  expose `read_binary`, `write_binary`, `read_symlink`, and `write_symlink`.
  Reads are confined to the worktree; writes are confined to the claim inside it. A refused write is
  returned to the model as a correctable error, never as a crashed run.
- A built-in supervised coding-agent harness over a neutral provider/session
  protocol, with optional Anthropic and OpenAI SDK adapters. OpenAI uses
  `OPENAI_API_KEY` and the Responses API, defaulting to `gpt-5.3-codex`.
  The `codex` provider uses ChatGPT subscription OAuth and defaults to
  `gpt-5.6-terra`, with browser/device login, status, local logout, private
  profile-scoped credentials, serialized token refresh, and an explicit
  expiring access-token import from Codex. It uses the same coding harness,
  tool broker, native checkpoints, and shared publication; no external agent
  loop or API-key billing fallback is involved.
  Durable launches record harness, provider, and model separately; an explicit
  provider override selects its own default model. The loop has a two-hour
  wall clock, 500-tool-call cap, and 64 KiB tool-result ceiling.
- Exploration helpers can use the parent task's offered `run_command` and
  `web_fetch` handlers with the same sandbox, command/domain approvals,
  pre-tool hooks, output screening, and page cache. Their command writes are
  discarded, their calls use the reserved helper budget, and their tokens
  contribute to task usage. Parallel fetches coalesce the same URL, serialize
  bounded-cache updates, and allow waiting helpers to cancel or time out
  without holding the cache lock over network/approval work.
  Plan helpers still cannot run commands, and all
  helper views refuse edits, configured checks, general questions, MCP access,
  and task completion. Helpers start no new tools after the task deadline.
- MCP authority for shared tasks over stdio or Streamable HTTP (JSON and
  bounded SSE responses), including tools and explicit resource/prompt
  list/read/get calls. HTTP header configuration contains only environment
  variable names, and resolved values stay out of events and results.
  Servers marked `read_only = true` are offered in plan mode; this is a user
  assertion about external authority. Calls retain per-server approval,
  bounded text output, secret screening, and external-data labels. Task end
  closes stdio process groups and HTTP sessions. Isolated tasks and
  exploration helpers do not receive MCP access.
  Received JSON-RPC messages require object shape and at most 64 nested
  object/array containers before schema or result screening, independently
  of Python's decoder limits; malformed messages fail as protocol errors.
- Durable harness execution checkpoints containing provider-native session
  state plus neutral loop state.
  The checkpoint records message history, the current model turn, completed
  tool results, remaining budget, and any one tool call whose
  outcome was interrupted. A restarted daemon resumes completed work without
  replaying it; an interrupted tool is reported to the model as uncertain so
  it can inspect tool-visible state rather than blindly repeat a mutation.
  All adapters save terminal tool results without requesting another model
  turn. OpenAI replays native Responses items, including encrypted reasoning,
  with `store=false`, and refuses incomplete responses before running tools.
- Timer-based work-lease renewal for the whole time a driver runs. A renewal
  that fails is raised the moment the driver returns rather than at the driver,
  because there is no safe way to interrupt one mid-write, and publication is
  never reached on a lost lease.
- A streaming attach (`task.attach`) that replays the durable event table from
  a supplied sequence and then follows the task live, so a reconnecting session
  misses nothing and attaching late is the same as attaching early. It runs
  outside the RPC authority lock, because a stream lives as long as its task.
- An interactive session (`llm-coord chat`) that renders a run as it happens,
  including when a prompt is queued behind overlapping exclusive work. The
  daemon owns the durable launch recipe and begins queued work as soon as its
  claim becomes active, so a detached chat or ordinary CLI invocation cannot
  strand an activated reservation waiting for a second RPC.
- An `ask_user` round trip: the model publishes a question, the attached
  session answers it, and the worker blocks on a bounded wait. The tool is
  offered only to a session that opted in, so a background run can never park
  on a question nobody is there to answer.
  `llm-coord task recover` re-runs recovery after an operator repairs whatever
  made an outcome unreadable.
- Shared-session integration for the built-in `coding_agent`: current checkout
  reads with a private per-task candidate overlay, durable checkpointed bases
  and candidate bytes, and independently validated multi-file publication.
  Tool writes do not modify the checkout. Subsequent prompts retain their
  conversation and read the current shared source.
- Live coordination context before every shared-task model request, including
  tool-result requests, nudges, and resumed execution. A consistent read-only
  database snapshot supplies workspace epoch/revision, active intentions with
  scope overlap, and known publication/divergence metadata after a separate
  model-visible event cursor. The cursor is checkpointed with successful model
  history and promoted to later session prompts; it never acknowledges terminal
  delivery. Oversized updates and event gaps stop the request explicitly.
- Bounded shared batch journals: up to 50 typed paths, 256 KiB per regular file,
  and 4 MiB of retained base and candidate bytes in the tool overlay. A batch
  changes one workspace revision and appends one checkout event, preserving
  HEAD, index, and refs. A preflight conflict retains the whole proposal
  without publishing any file; startup and explicit recovery can roll forward
  partially applied batches without overwriting third states.
- Pending edits follow files that change underneath them. When another session
  publishes, or someone edits a file outside Loupe, a task's edits are merged
  onto the file's current bytes with a three-way merge from their recorded
  base before validation, checks, commands, and finishing. Overlapping edits
  leave diff3 conflict markers (`your pending edits` / `your starting
  version` / `current checkout`), and checks and finishing are refused until
  the agent resolves them; a merged file must be read again before
  `write_file` may replace it. An edit deleted on one side and changed on the
  other is set aside, and finishing refuses once so the agent sees it first.
  Checks rerun when the checkout changes while they run. In auto mode, a task
  whose checkout moved after it finished merges again, reruns its checks, and
  publishes under the same lock as the final check; a conflict there holds the
  agent's own edits for review. Changes that cannot be merged, such as a file
  that became binary or a symlink, still refuse the whole batch.

- Held proposals use the same three-way text merge when manually applied.
  Required checks rerun against the merged candidate outside the publication
  lock, followed by exact-base and evidence validation before journaled apply.
  Conflicting or unmergeable paths refuse the whole batch, including with
  `--allow-unverified`; undo remains exact-base and preserves the preceding
  checkout changes.

- Public `web_fetch` pages support configured HTTP CONNECT proxies through
  `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (or lowercase forms), and an explicit
  `agent.web_proxy`. CONNECT uses the target's checked IP, with bracketed IPv6
  authorities in both the request and Host header across supported Python
  versions. TLS still verifies the original hostname; redirects retain domain
  approvals and public-address checks. Local proxy tests cover successful
  fetches, bypass, refused private targets and redirects, and configuration
  errors without secrets. See harness plan item 10 for configuration.

- Regex matching reuses a fixed pool of at most four isolated Python workers
  across brokers and batched reads. Waiting, pipe transfer, compilation and
  matching share the search deadline. Timeout or cancellation kills and reaps
  that child before its slot is reused; the next request starts a replacement.
  Idle workers drop the previous JSON source payload and purge compiled regex
  caches after each request. The sequential/batched read differential suite
  once again exercises the real matching workers.
- Wave 1 regex measurements on the existing read microbenchmark used the same
  unchanged checkout, 15 measured runs after two warmups. The shared two-listing,
  two-search batch median fell from 195.0 to 136.5 ms (sequential: 378.2 to
  283.3 ms). Isolated full-source scans remained dominated by source acquisition:
  348.7 to 374.4 ms batched and 636.5 to 639.5 ms sequential in these runs, while
  other validation jobs were active. A separate alternating before/after matcher
  measurement (30 samples after five warmups, identical 2,000-line input and
  paginated results) fell from 13.33 to 0.43 ms. Reuse removes process-launch cost;
  it does not remove checkout discovery and privacy checks.

## Current execution boundary

`llm-coord chat --repo /path/to/repository --scope docs/` uses shared checkout
execution when its durable session selects the shared-capable `coding_agent`
harness. Its tools enforce the write claim and retain their edits privately.
Reading an existing file establishes its exact base and bytes together;
subsequent full reads can refresh that base before editing, and staging pins
the base until publication. The runner validates the entire batch against the
live checkout before changing any file. Completed shared tasks release their
claims. Read-only and no-op tasks publish no batch and allocate no revision.

Use `llm-coord workspace status /path/to/repository` to inspect the mode, epoch,
and revision, and `llm-coord task events TASK_ID` or `task watch TASK_ID --after
SEQUENCE` to inspect the durable execution stream. Follow-up prompts use the
saved provider conversation and current shared source, including prior
successful task edits; the model is instructed to reread files before relying
on earlier context.

Coordination updates are included in the opening/nudge message or appended to
the last actual tool result in a completed batch. Tool-call IDs and error flags
remain intact. Updates share the existing 64 KiB tool-result bound; when needed,
the tool output is explicitly truncated to make room for the complete metadata.
Pending checkpointed tool outcomes remain unchanged, so a retried model request
receives one freshly compiled update. A successful model response and its
coordination cursor enter the same execution checkpoint.

The current context slice is advisory: it does not implement the plan's future
verified context-packet acknowledgments or mutation barrier. It covers durable
coordinator events and intentions, with up to 1,000 pending events and a 16 KiB
rendered update. A bounded scan now detects net external changes to paths a
shared session has actually observed, at each model boundary and on rotating
daemon ticks. Metadata invalidates cached identities; stable eligible source
is screened before hashing. Excluded, secret, oversized and nonregular paths
remain opaque. Durable path-only events reach only the session that observed
the path; scanners never change publication bases or terminal cursors. Quiet
cursor advances require validated irrelevant events and a prior model snapshot.
Only fresh source captures register observations; private/pinned/diff replays
cannot reset deduplication. Own confirmed publications are not reported again
as outside edits. A scan covers the session's 1,000 most recently read paths
and hashes at most 16 MiB of source (256 KiB per file) in five seconds. A path
that changes while it is read, or does not fit this scan's budget, keeps its
previous state for a later scan, so a branch switch catches up over several
boundaries. Notices are advisory: a scan that cannot complete never fails the
model turn or the daemon loop; the next update says once that outside edits
could not be checked, and later boundaries scan again. It does not monitor Git
operations or reconstruct intermediate edits between scans.
Exact-base checks still reject stale publication independently of model context.

Sessionless background runs and the `fixture_write` driver retain the original
linked-worktree lifecycle described below. Isolated conversations use the separate
hydrated `--workspace isolated` lifecycle described in the
[isolated workspace guide](isolated-workspaces.md).

`llm-coord run --fixture-write PATH=CONTENT` returns after durably scheduling a
deterministic execution rather than holding a CLI/RPC session open. Every
session can poll `llm-coord task events TASK_ID --after SEQUENCE` for the shared
durable lifecycle stream. The worker obtains an active claim, renews before
worktree setup, persists an execution record, creates a detached linked
worktree at an exact base commit, applies only explicitly supplied writes,
validates the trusted Git tree, persists a publication intent, CAS-publishes an
internal result ref, confirms the intent, and then removes the worktree. The
publication swap is made against the ref's recorded predecessor, so a later
attempt of the same task advances its ref while a ref that moved underneath the
intent is still refused.

If validation or the fixture driver fails before publication preparation, the
claim is released atomically with compatible waiter activation, and the task is
recorded as failed with its error code rather than as an operator cancellation. Once a
publication intent exists, it remains blocking; a failure after that point is
recorded as operator attention rather than being released speculatively.

## Recovery after a daemon stops mid-execution

Shared executions first recover durable batch journals. If every affected
path matches its recorded base or result, recovery can finish the remaining
replacements and confirm the batch once. A third state preserves the journal
and blocks further shared reads and publications for operator attention;
`llm-coord task recover` retries after the discrepancy is resolved. Before a
batch exists, a valid resumable harness checkpoint restores private candidates
and observations. An interrupted tool is reported as uncertain, with only its
last checkpointed edits retained. The shared checkout is never a managed
worktree cleanup target.

The following ref-intent decisions describe the linked-worktree path, including
the fixture driver and non-resumable executions. A valid resumable harness may
instead retain its locked worktree and continue from its checkpoint.

An execution only ever moves forward inside the process that started it, so a
durable record stamped with an earlier boot has no owner. Startup resolves each
one before the socket accepts anything, so no session can see a task that looks
runnable while a dead worker's reservation still blocks it.

- No publication intent means no shared side effect can have happened at all,
  because publication is the only writer of shared references and never runs
  before its intent is durable. The claim is released and its waiters wake.
- An intent whose reference already holds the recorded result means the swap
  did happen, so the publication is confirmed and the reservation advances to
  integration exactly as a clean run would have left it.
- An intent whose reference still names precisely what the swap meant to
  replace is proof the swap did not happen. That -- not merely an absence of
  evidence -- is the bar for releasing a publishing reservation.
- Anything else is unknown. The intent is recorded as needing an operator, the
  reservation stays `publishing`, and no waiter is activated. A publishing
  claim holds no lease, so expiry reconciliation cannot retire it, and its
  managed worktree is kept as the remaining evidence of what the attempt
  produced. `llm-coord task recover` re-decides such an intent once the
  repository can answer the question; `operator_attention` records a question,
  not an answer, so later evidence can still settle it.

Shutdown waits for in-process workers rather than cancelling them: a worker
runs on a thread, and cancelling the task awaiting it only stops watching while
the thread keeps going, potentially mid-`git`. Whatever is still running when
the drain budget expires is recorded as abandoned and left to the next boot.

Stale managed worktrees are removed as part of each decision, including the
case where the directory was deleted by hand but Git still owns the record --
which would otherwise make every later attempt of that task fail to create its
worktree. Pruning is refused outright if the repository also holds stale
records outside the managed root, because `git worktree prune` is global and
the operator's own worktrees are theirs to retire.

The fixture driver remains, writing straight to the worktree rather than
through the tool broker and declaring that it does not enforce scope. That is
deliberate: it stands in for a cooperative external driver, so every run of it
proves that trusted validation, not the tool surface, is what actually stops an
out-of-scope change from being published.

The agent still has no arbitrary shell-command tool. The daemon persists each
non-claim-only launch recipe before requesting its claim, scans them after every
activation and on startup, and starts compatible queued work itself. A resumed
harness execution on the linked-worktree path is adopted only when its
registered driver still declares resumability and it has an `active_work`
claim, its exact locked managed
worktree, a matching durable launch, and a valid execution checkpoint. Unknown
and non-resumable drivers retain the conservative recovery path.

## Shared workspace integration and remaining boundaries

The optional knowledge and vector stores have independent migration planes.
F1 implements explicit local lexical CLI/RPC discovery, disabled by default.
Builds read regular Git blobs at an exact HEAD and identity-matching accepted
artifacts; later accepted shared publications and observed outside edits project
through an ID-only control outbox to an independent worker. Knowledge writes
happen outside the publication barrier. Search labels stale indexed versions,
reapplies current source exclusions and withholds recognized secret material.
An unavailable optional index records health without blocking sessions or source
publication. Indexing does not acknowledge changes, choose write bases or grant
publication authority. F2 adds deterministic source graph projections and explicit
CLI/RPC neighbors with parser/version/identity/generation evidence. Changed
documents replace their graph facts atomically; unchanged graph rows remain.
Agent retrieval tools and their live comparison, embedding providers, semantic
retrieval and `sqlite-vec` remain planned.

F2 validation at `0ab3fcd` passed 2,824 tests, with one case-sensitive-filesystem
skip and the existing G1 cross-profile expected failure. Ruff, mypy over 119 source
files and source-distribution/wheel builds passed. Python statement-container
traversal retained identical graph facts across all 365 tracked Python files.
The dense 10,000-file fixture produced 400,000 function nodes in 11.655 seconds
under coverage, within the original 30-second budget; the
[F2 plan](parallel-work-plan.md#f2-deterministic-code-graph) distinguishes this
workload from the earlier simpler baseline.

F1 validation at source revision `507bd41` passed 73 focused lexical, backup,
configuration, schema and scanner regressions, repository Ruff, mypy over 116
source files, and source-distribution/wheel builds.
An offline acceptance run against Loupe at baseline `fa6632a` built 338 eligible
documents and 2,560 chunks, returned cited `SharedBatchPublisher` results, and
preserved chunk IDs after drop/rebuild. The complete build/search/drop/rebuild
sequence took 7.16 seconds on this machine. This establishes local discovery;
it is not a measurement of model answer quality or agent retrieval benefit.

F1 review follow-ups batch Git blob reads, replace only changed documents' chunks
and FTS rows, select the latest accepted receipt per path in SQL, and keep the
optional worker polling after unexpected errors while reporting health. The
corrected source passed 2,669 tests with one expected filesystem skip, repository
Ruff, mypy over 116 source files, and wheel/source builds. A synthetic checkout
with 10,000 distinct forty-line Python modules built in 1.58 seconds against the
unchanged thirty-second budget. In a flat-directory fixture, median ordinary
publication processing was 34.0 ms at 1,000 files and 34.6 ms at 10,000; metadata
checks for policy changes still depend on watched directories and rule files.
Rebuild regressions also cover over 20,000 shared or outside-edit publications
to a few paths, and incremental failures preserve the active generation, FTS
rows and event cursor together.

A task owned by a durable session settles its reservation as soon as its
publication is confirmed: the claim is released, the task is recorded as
completed, and the published result stands. ADR 0007 requires this -- a
session's coordination tasks are short-lived children that must not hold write
authority while the operator is thinking -- and the workspace-modes plan
requires it from the other side. Fenced claims still limit the paths a task may
edit and publish, but new eligible shared-session attempts use a persisted
`optimistic` scheduling mode: their private work can proceed concurrently even
when scopes overlap. Each claim binds to the session's exact shared workspace,
and publication rechecks every edited base through the existing batch barrier.
Only drivers supporting shared workspaces, scope enforcement, and durable
resumption qualify; launch and recovery must retain those capabilities and
identities. One session still has at most one live task.

Pre-migration attempts, including queued work, retain `exclusive` scheduling.
Sessionless runs, fixtures, and claim-only requests also remain exclusive.
Whenever either overlapping claim is exclusive, repository-wide FIFO fairness
still applies: an older queued exclusive claim blocks younger overlapping
optimistic requests. Two optimistic claims do not block each other's private
preparation, whether they bind to the same shared workspace or separate valid
checkouts. A task with no session keeps the older internal-ref contract and
stays reserved until an operator decides. This incremental scheduling change
does not implement the plan's full cross-profile authority-regime migration.

Every coordinated path now has an exact content identity: a versioned digest
over object kind, normalized mode, and content. A Git blob ID cannot serve this
role -- ignored and untracked files have no blob, working-tree filters mean a
blob need not materialize to the bytes on disk, and a blob says nothing about
the executable bit or about a path being absent. Symlinks are identified by
their target text rather than by what they point at, so a link and a copy are
never confused.

Each checkout has one shared workspace record carrying its mode, epoch, and
revision. Shared sessions bind to it; isolated sessions bind to a private linked
worktree whose source workspace is that shared record. `llm-coord workspace
status` reports the workspace and mode limits. Ignored-runtime hydration has a
read-only preview, and C2 implements private materialization and runtime-aware
execution. Isolated tasks now publish through the shared batch journal and
receive incoming source at safe points; the private baseline and transition
journal retain unresolved local divergence across restart.

The manual shared-workspace broker remains available for one UTF-8 regular
file at a time. A brokered read records an exact versioned identity. A session
then stages a complete private content-addressed candidate and publishes it
through a short daemon barrier. The broker rechecks the candidate's base twice,
uses same-filesystem Git-internal staging plus `rename` replacement, and commits
the workspace revision and checkout event in one SQLite transaction afterwards.
Its prepared journal is reconciled at daemon startup against the actual path:
the result proves publication, the base proves it never began, and any third
state becomes a retained durable divergence. HEAD, index, and refs are never
changed by this path. `llm-coord workspace read`, `stage`, and `publish` expose
this manual contract; shared agent tasks use the separate multi-file batch
journal described above.

Shared tools exclude ignored runtime files and nested repositories. Missing candidate
parents can be staged as directories inside scope. Regular-file deletion,
renames, binary replacements, and contained relative symlink edits use the batch
journal. Binary and symlink changes never merge after a base change; escaping,
protected, ignored, and chained symlink targets are refused. Named checks use
disposable snapshots. The daemon barrier
coordinates cooperating reads and publications within one profile. It does not
make a multi-file filesystem replacement atomic for external editors or test
processes, and the complete multi-profile guarantees in the plan are not yet
implemented. `validate_changes` checks scope and exact bases; it does not run
tests or prove that disjoint changes work together.

Scripted-provider end-to-end tests exercise real Git repositories, successive
shared prompts, candidate conflicts, and restart recovery without remote model
calls. They establish the implemented lifecycle behavior, not live-model
collaboration quality, speed, cost, or automatic validation of generated code.

A live ChatGPT-subscription smoke test using `gpt-5.6-terra` also exercised the
real daemon and built-in harness in a disposable repository: two overlapping
private edit batches, winner publication with whole-batch retention of the
stale loser, a durable follow-up that read current checkout contents, and a
process crash followed by restoration of the private candidate and peer
coordination cursor without repeating its write. HEAD and index were unchanged.
This verifies provider transport and the tested coordination/recovery paths;
it is not yet a comparison of coding quality or throughput against one agent.

## Live two-session experiment

### Wave 4 isolated and mixed smoke (October 17, 2026)

The session runner supports `--workspace shared|isolated|mixed`. Its
`toolkit-runtime` fixture has an ignored generated canary, exact `.env` consent
for isolated lanes, and a required check that reads the private runtime without
printing it. Trusted hidden acceptance ignores candidate pytest configuration
and hooks. Each run checks primary HEAD/index, physical session modes, canary
exclusion from control data and logs, and removal of private worktrees and
`.env` copies.

The [sanitized measurement record](experiments/wave4-isolated-smoke-2026-10-17.json)
keeps two source cohorts separate. Before the incoming-sync fix, four pilots and
seven repeats passed; a mixed disrupted repeat lost the outside operator note,
so the original matrix stopped with four repeats unstarted. A deterministic
witness traced that failure to a false conflict on an unchanged private file
when a published batch already included an outside edit. After fix `2a3e12e`,
one new mixed disrupted pilot and three repetitions preserved the note and
passed every hidden test, own check, and runtime/privacy/cleanup audit.

Two of those three repetitions needed one follow-up each: their original
isolated tasks failed before the daemon restart when a synchronization notice
was attached to a quiet coordination update without clearing `facts_unchanged`.
The harness correctly rejected that contradictory update. The record retains
both original failures, both permitted follow-ups, their time and tokens, and
the no-provider reproduction. Fix `608dd2b` passed 56 focused and 30 independent
tests, Ruff and mypy; it is validated offline only. The live candidate
`eb2d6f5` does not contain it. A full matrix on one final candidate was
not completed.

| Repeat cohort and condition | Samples | Median wall time | Median tokens |
| --- | ---: | ---: | ---: |
| Before incoming fix: isolated clean | 3 | 90.4 s | 154,485 |
| Before incoming fix: isolated disrupted | 3 | 100.1 s | 212,088 |
| After incoming fix: mixed disrupted | 3 | 96.9 s | 166,798 |

The mixed disrupted median includes the two permitted recoveries. Historical
shared v3 pair medians were 74.4 s / 124,159 tokens clean and 84.0 s / 162,268
tokens disrupted. That older fixture had no runtime canary/check; full offline
suites also ran on the same host during some live cases. These wall times do
not establish a matched throughput comparison. All initial, failed and
follow-up attempts count against the 3M budget: **2,892,023 tokens spent,
107,977 remaining**. No further live calls were made.

The frozen C6 runner branch `4cf7819` passed 2,575 tests with one expected
filesystem skip, Ruff, mypy over 110 files, and wheel/sdist builds. The live
product candidates also include C7 and the isolated scheduling follow-up;
the record pins exact sources, fixture trees and archive hashes.

### Earlier shared-mode experiments

`scripts/live_sessions.py` runs the same multi-file change two ways with a
real model: one session given every part, and one session per part working
at once in one shared checkout. The `shop` experiment (`live_sessions.toml`)
asks for discount codes and stock limits, which both edit the cart, its tests,
and the changelog. The `toolkit` experiment instead divides text and numeric
helpers into independent modules, with separate flags in one shared registry.
A "disrupted" scenario also appends a function to the cart or registry
by hand once an agent has edited it, as someone in an editor would, and kills
and restarts the background service once every session has edited. The script
plays the operator: it answers questions and, when a session's work is not
applied, asks again in a new session, counting both as interventions. Hidden
acceptance tests then run in a sandbox against a copy of the final checkout.
It is billed and never runs in CI:

```console
uv run --extra openai python scripts/live_sessions.py --profile NAME
```

One run of each arm with `gpt-6-sol` at `xhigh` effort (2026-10-12):

| Scenario | Arm | Wall time | Tokens | Interventions | Hidden tests |
| --- | --- | --- | --- | --- | --- |
| clean | one agent | 87 s | 132k | 0 | 16/16 |
| clean | two sessions | 183 s | 413k | 1 | 16/16 |
| disrupted | one agent | 194 s | 315k | 1 | 17/17 |
| disrupted | two sessions | 164 s | 368k | 1 | 17/17 |

- **Safety held.** Every final checkout passed every hidden test, the hand
  edit survived both disrupted runs, no session overwrote another's work, and
  both runs that crashed resumed with each tool result recorded once.
- **Stale edits stop the work.** Whenever a file a session had already edited
  changed underneath it -- another session's publication or the hand edit --
  the session noticed, but could not redo its edits on the new version: a
  shared read of a path with pending edits returns those edits, so no tool
  shows the newer file or moves the edits onto it. Its required checks became
  stale, it finished `partial`, and its proposal was held for review. This
  happened in all three runs with an overlapping change, and each needed the
  operator to ask again.
- **Parallel work is faster only without that.** In the clean run, the
  session whose work went in first finished in 52 seconds, against 87 for one
  agent doing both parts. The other session spent 125 seconds before its work
  was held and 56 more redoing it, so the pair finished later than one agent
  and used about three times the tokens (413k against 132k).

These are single runs, so the timings are noisy; the held proposals are not,
since the tools could not do otherwise.

The same experiment after pending edits learned to follow changed files
(same model and effort, 2026-10-12):

| Scenario | Arm | Wall time | Tokens | Interventions | Hidden tests |
| --- | --- | --- | --- | --- | --- |
| clean | one agent | 82 s | 126k | 0 | 16/16 |
| clean | two sessions | 93 s | 281k | 0 | 16/16 |
| disrupted | one agent | 93 s | 172k | 0 | 17/17 |
| disrupted | two sessions | 108 s | 306k | 0 | 17/17 |

- **No run needed the operator.** Each session that finished second, and the
  single agent after the hand edit, had its edits merged onto the newer files
  before finishing. Where the edits overlapped -- the cart, its tests, and the
  changelog -- the model resolved the conflict markers, its checks passed,
  and it published. The hand edit survived both disrupted runs, and both
  crashed runs resumed with each tool result recorded once.
- **Two sessions were slower in these single runs.** Both parts edit the same
  three files, so the second session resolves the overlap and each session
  reads the code for itself: about twice one agent's tokens, and slightly
  more wall time. These runs motivated measuring a second fixture with less
  shared code and repeating both experiments.

Wave 1 A2 repeats both experiments three times per arm and scenario
(2026-10-13 UTC), using Codex `gpt-6-sol` at `xhigh` effort through the
non-default `explore-eval` profile. Loupe ran from the `b5b0b2a` baseline,
which includes the pending-edit merge fix; experiment version 2 and the new
fixture came from `96f0c45`. Arm order alternated between repetitions.

```console
uv run --all-extras python scripts/live_sessions.py --profile explore-eval \
  --provider codex --model gpt-6-sol --effort xhigh --repeat 3
```

Each table value is the median of three completed runs. Tokens are reported
prompt plus output tokens, including cached input; they do not estimate cost.
The [measurement record](experiments/wave1-live-sessions-2026-10-13.json)
preserves every run's timing, usage, disruptions and acceptance counts.

| Experiment | Scenario | Arm | Median wall time | Median tokens | Interventions | Hidden tests in every run |
| --- | --- | --- | --- | --- | --- | --- |
| shop | clean | one agent | 108.3 s | 130,730 | 0 | 16/16 |
| shop | clean | two sessions | 102.4 s | 282,324 | 0 | 16/16 |
| shop | disrupted | one agent | 109.8 s | 176,916 | 0 | 17/17 |
| shop | disrupted | two sessions | 105.8 s | 301,907 | 0 | 17/17 |
| toolkit | clean | one agent | 72.4 s | 76,102 | 0 | 15/15 |
| toolkit | clean | two sessions | 78.4 s | 126,735 | 0 | 15/15 |
| toolkit | disrupted | one agent | 75.8 s | 76,594 | 0 | 16/16 |
| toolkit | disrupted | two sessions | 91.8 s | 149,761 | 0 | 16/16 |

All 24 final checkouts passed every hidden acceptance test and their own
tests. All 12 disrupted runs exercised the manual edit and daemon restart;
the operator's function survived each time. Every lane applied, with no
recorded question answers, follow-ups, rejected batches or timeouts.

The medians show no consistent speed benefit: two sessions were about 4–5%
faster on `shop` and 8–21% slower on `toolkit`, while using 1.7–2.2 times the
reported tokens. These small fixtures and three repetitions do not establish
a general throughput result. One single-agent shop run encountered a provider
retry, and other validation jobs ran on the same machine. The new experiment
separates the code edits, but its shorter tasks still did not amortize the
additional session work in these measurements.

These runs used experiment version 2, whose toolkit registry put the two
flags on adjacent lines. A three-way merge treats adjacent changed lines as
one conflict, so every two-session toolkit run had to resolve a registry
conflict, which may account for part of its extra time. Version 3 puts a
comment line between the flags so the parts merge cleanly. Its repeated
measurement follows below.


### Toolkit version 3 measured results (October 14, 2026 local)

The revised fixture separates adjacent text and numeric registry flags with a
comment, allowing their disjoint changes to merge cleanly. On
final Wave 1 commit `47d1451`, twelve live Codex runs repeated single/pair and
clean/disrupted cases three times with `gpt-6-sol`, effort `xhigh`, exploration available (unused in every run), commands allowed, web fetch off,
and a matching noneditable Python 3.14 wheel. Independent review was unavailable
in this baseline. Full test
suites were held during measurement; implementation work and short focused
checks continued. [Sanitized per-run evidence](experiments/wave1-toolkit-v3-2026-10-14.json)
retains the source fingerprint and exact settings.

| Scenario | One agent median | Two sessions median | One agent tokens | Two sessions tokens |
|---|---:|---:|---:|---:|
| Clean | 62.4 s | 74.4 s | 70,197 | 124,159 |
| Restart + manual edit | 79.8 s | 84.0 s | 84,108 | 162,268 |

All twelve runs passed their own tests and every hidden acceptance test
(15/15 clean, 16/16 disrupted). Every disrupted run restarted the daemon and
preserved the operator edit. There were no answers, follow-ups, timeouts,
rejected batches, failed lanes, or measured sleep intervals.

On this fixture, two sessions were 19.2% slower clean and 5.3% slower disrupted,
using 1.77x and 1.93x the tokens respectively. Tokens are prompt plus output,
including cached prompt tokens, and do not measure dollar cost. Three samples
per case do not establish a general performance claim. Earlier version 2 ran
on implementation `b5b0b2a`; version 3 also includes subsequent Wave 1 product
changes, so the difference does not isolate the fixture revision. The shop
fixture was unchanged and was not rerun. E2 remains deferred.


## Wave 2 B1: independent review before settlement

`agent.review = "on"` enables one fresh read-only review after candidate
reconciliation and required checks pass. The setting defaults to `off`.
`agent.review_effort` defaults to `low`; `"task"` uses the task's effort,
and any requested level is capped at that effort.

### Wave 4 B6: paired review corpus (October 17, 2026)

Suite version 4 measures eight planted defect kinds and four correct controls,
three repetitions per review arm, alternating the first arm. The final 72-cell
comparison combines 66 original live cells and six replacement control cells;
six saved resource proposals were re-scored offline after fixing the oracle.
All raw outcomes and costs remain recorded, with exact proposal/hidden-test witnesses.
`codex/gpt-6-sol` used task effort `xhigh`, review effort `low`, and no exploration.
Hidden bug tests passed for **0/24 with review off** and
**24/24 with review on**; review-driven repairs broke
**0/12 clean controls**. Finding counts alone do not establish
semantic precision or effectiveness on natural work.

Review on used 1,313,126 observed tokens versus 534,644 off
(2.46×), with median task wall time 43.25 s versus
17.00 s. Direct reviewer tokens were
70,277 of the on arm; extra repair and check work
accounts for the rest of the overhead. The final matrix and excluded earlier
attempts together consumed **2,317,163/3,000,000** tokens. A complete
medium-effort comparison did not fit the remaining measured-cost budget.

**Recommendation: keep review off by default.** This seeded corpus supports an
explicit opt-in at a measurable cost; changing the default remains the user's
decision. The [experiment](experiments/review-corpus-b6-2026-10-17.md) records
per-case outcomes, cache usage, provenance, excluded attempts and limitations.

### Original review gate and Wave 2 acceptance

The runner supplies the exact pending diff and complete original/candidate
text, plus check results tied to the candidate/source/configuration digests.
The reviewer returns explicit no-findings or changed-file/line findings.
Findings return to the original conversation once for bounded repair and the
usual required checks run again. There is no second review. A failed,
interrupted, malformed or oversized review produces a visible notice and
continues through the existing verification/publication rules. Normal-mode
proposals still require the user's `/apply`.

The reviewer has only file-read tools, a reserved budget of at most 20 calls,
the task's cancellation/deadline, separate read observations and screened
input/output. Its tokens count toward task totals and separate `review_*`
usage. Checkpoints preserve the single attempt and any findings not yet
delivered, so a restart neither bills a second review nor loses the handback.

Validation so far: 59 focused review/benchmark tests pass, including finding
repair, natural answers, check failures, reviewer failures, off/plan/no-edit
paths, secret/oversized inputs, write-authority separation, cancellation token
retention in execution records, post-review deadline enforcement, and restarts
before/after review. Disabling the review gate makes
both publication/repair regressions fail. Independent read-only review found
no additional defects and its separate run passed all 26 review tests. Ruff
and mypy pass (105 source files). Frozen product source `6653b0e` passed all
2,210 tests with every extra installed on Python 3.14.8, using a noneditable
wheel from an immutable snapshot. The later prompt, regression-test and
documentation commits leave product source unchanged; their targeted follow-up
checks pass. Independent review of the cancellation/deadline follow-up also
found no defects and reran both added regressions successfully.

The original Wave 2 live benchmark used suite version 3 (separate from the
session experiment's version). `independent-review-planted-bug` deliberately seeds an off-by-one
pagination proposal that passes its incomplete smoke checks, calls the review
gate, then repairs findings. Scoring requires exactly one observed completed
review with at least one finding. A hidden acceptance test fails on the
seeded proposal and passes on its repair. Run only with a non-default profile:

```console
uv run --all-extras python scripts/live_benchmark.py --profile explore-eval \
  --provider codex --model gpt-6-sol --effort xhigh --set review=on \
  --set review_effort=low --set explore=false \
  --only independent-review-planted-bug --out /tmp/loupe-review-benchmark.json
```

The [live acceptance record](experiments/wave2-independent-review-2026-10-14.json)
reports one successful Codex `gpt-6-sol`/`xhigh` run with review effort `low`,
from benchmark source `26bb129` (product source identical to `6653b0e`). It
recorded exactly one task, one completed review, one finding, two finish
attempts, two check runs and two edits, with no questions or exploration
helpers. The repaired checkout passed its two smoke tests and the hidden
acceptance test. Total usage was 32,753 prompt and 769 output tokens; the
review's included share was 1,187 prompt and 97 output tokens.

The observed duration was 44 seconds while full test suites ran on the same
machine, so it is not a comparative performance result. An initial run was
excluded because a multiline piped prompt submitted three tasks; the prompt
is now one physical line and its scoring/prompt tests pass (31 tests). This
controlled example demonstrates the finding/repair gate. The dated Wave 4 B6 corpus
broadens this synthetic measurement; natural-work effectiveness and cost
savings remain unmeasured. Review remains off by default.

## Wave 4 G1: proposed cross-profile authority decision (October 17, 2026)

[ADR 0010](adr/0010-cross-profile-publication-authority.md) is **Proposed**, pending
user review. A checked-in integration reproduction runs two real profile daemons
and ordinary shared publication RPCs. With both renames paused after their final
base checks, both independent journals prepare and then confirm different
same-base results; the second replaces the first. Its strict expected failure
accepts only the specific final violation after all preconditions and durable
witnesses pass; startup, RPC and unrelated failures remain test failures.
`--runxfail` independently confirms that exact known violation.

The ADR selects profile-independent physical ownership and conservative dirty
journal recovery for G2, with read-only peer status, stable errors, database
instance binding and clean idle release. G3 separately adds claim-free workspace
execution and explicit, recoverable legacy migration. G2/G3 scope, file ownership
and acceptance criteria have been rewritten in the parallel plan. Global
protected handoff equality, complete offline-drift history, generation-based
restore/downgrade fencing and foreign-journal import remain explicit deferrals.
No authority product behavior changed in G1. Focused reproduction: one strict
expected failure; Ruff passes. Acceptance of the proposed ADR remains outstanding.

## Next executable slice

[`parallel-work-plan.md`](parallel-work-plan.md) splits this list into
Waves 4–7, as packages that separate sessions can run at once.

1. Validate the remaining multi-profile authority and migration guarantees.
   G1 designs these first, then G2 and G3 implement them.
2. Complete isolated-mode live validation (C6); C7 implements faster checks.
   Then extend isolated runtime support with runtime refresh (C8), a
   per-session command environment (C9) and dependency rebuild adapters (C10).
3. B6 now records paired synthetic review quality and cost. Keep review disabled
   by default; evaluate natural-work effectiveness before reconsidering it.
4. F1 lexical discovery and F2's deterministic code graph are implemented. Next
   add retrieval in the agent with a live comparison (F3). Add embeddings with
   exact scoring (F4) only if that comparison shows retrieval helps.
5. E2's live provider checks are on hold.

The full phased design remains in
[`llm-cli-implementation-plan.md`](llm-cli-implementation-plan.md).


## Wave 2 local validation (October 14, 2026)

B1 independent review, B2 helper commands/fetches, C1 hydration preview and C4
external-change notices are implemented locally. The combined frozen candidate
`51f5048` passed **2,316 tests**, with one expected case-sensitive
filesystem skip on macOS. Ruff and mypy (107 source files) passed. A matching
noneditable all-extras wheel and every original snapshot file were verified.
The [validation record](experiments/wave2-validation-2026-10-14.json) records
the tested commits, source fingerprint and required integration regressions.
The corrected B1 live check found and repaired its planted bug before
publication; the revised toolkit v3 benchmark completed all twelve runs.
Phase 2 is not merged, and E2 remains deferred.


## Wave 2 review corrections (October 15, 2026)

The corrected candidate `7c15c49` passed **2,385 tests**
with one expected macOS filesystem skip, Ruff, mypy and package builds. Its
installed wheel and frozen source were verified unchanged after testing.
The [validation record](experiments/wave2-review-fixes-2026-10-15.json)
records the candidate identity and regression coverage.

Independent review now accepts standalone fenced JSON and retains valid findings
when other entries are malformed. Explicit no-findings is valid; a nonempty
findings list with no valid entries fails. Review has a 120-second cap with 1–30 seconds
reserved for completion. The cap covers provider waits and shared source tools;
a timeout produces a visible incomplete-review notice. `/usage` itemizes the
review's share of existing token totals. Real task cancellation and the original
deadline still govern repair.

Helpers identify themselves in command/domain approvals. A parent receives a
durable reconciliation notice before its next action, including after replay or
compaction; shared fetch waiters honor the task lifecycle while awaiting human
approval. Hydration's default output is a short summary, and `--json` retains
full details. Git-ignored `node_modules` directories are explicit rebuild
decisions with unknown contents rather than enumerated copy candidates,
preserving unrelated candidates. C2 now implements bounded private copying as
noted above, and C3 adds checked source publication and incoming synchronization.
E2 remains deferred.


## Wave 3 validation follow-up (October 16, 2026)

The first combined frozen run finished with 2,484 passing tests, five failures,
and one expected case-sensitive-filesystem skip. All five failures exercised one
shared-session settlement regression: isolated catch-up handling had changed
unmergeable shared proposals from durable divergence to held review. The fix
limits that handling to isolated sessions.

The corrected revision passed 64 focused orchestration-stress, catch-up,
isolated synchronization/publication/held-workflow, OpenAI publication-refusal,
and shared-finalization tests, including all five failing cases. Ruff, mypy, and
source-distribution/wheel builds passed. Current cross-platform CI results are
recorded on [PR #55](https://github.com/bkavuri-hwlr1/loupe/pull/55).

## Wave 4 C6 scheduling follow-up (October 17, 2026)

C6 exposed exclusive preparation claims on isolated sessions, which serialized
paired work before either session could publish. Eligible isolated launches now
use the existing optimistic preparation path. Each claim binds its own active,
ready private workspace and the matching active shared source checkout. Saved
driver/attempt authority, older exclusive waiters, publication scopes, leases,
cancellation and fencing stay in force. This is a narrow scheduling fix; G3's
claim-free execution and migration remain planned.

Regressions prove same-scope isolated/isolated and shared/isolated tasks can both
stage disjoint edits before either publishes, then publish both without changing
HEAD or index. Malformed/foreign/private-not-ready bindings are refused; older
exclusive waiters retain their reservations. Full suite: 2,566 passed, one
case-sensitive-filesystem skip. Ruff and mypy passed.
