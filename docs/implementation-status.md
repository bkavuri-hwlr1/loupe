# Implementation status

Loupe is a provider-neutral terminal coding agent whose independently
started local instances coordinate when they work on the same repository.
The durable execution foundation now includes an interactive terminal UI;
the remaining planned capabilities are listed below.

## Implemented

- Verified shared-task workflow: named checks in disposable worktrees, explicit
  dependency setup, bounded replayable subprocess output, required-check gating,
  retained review proposals, task diffs, apply, undo, and cancellation. Shared
  overlays and batch journals support directory creation, regular text-file
  deletion, and renames. Configuration is owner-local and frozen per attempt.
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
- Three independently migrated SQLite databases: control, knowledge, and
  vectors. The control database uses WAL, `synchronous=FULL`, transactional
  checksummed migrations, integrity checking, and an atomic online-backup API.
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
  tasks, FIFO overlap fairness whenever either claim is exclusive, concurrent
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
- A provider-independent harness/driver seam. The lifecycle owns the claim, worktree,
  validation, publication, and recovery; a driver only decides file contents,
  and only through a tool broker bounded by the claim. Drivers declare their
  capabilities, including whether they enforce scope themselves.
- A bounded model tool surface: `list_files`, `read_file`, `search_text`,
  `read_diff`, `write_file`, `apply_patch`, `validate_changes`, `finish_task`,
  and `ask_user` when a session is attached. Reads are confined to the
  worktree; writes are confined to the claim inside it. A refused write is
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
- Bounded shared batch journals: up to 50 regular UTF-8 files, 256 KiB per file,
  and 4 MiB of retained base and candidate bytes in the tool overlay. A batch
  changes one workspace revision and appends one checkout event, preserving
  HEAD, index, and refs. A preflight conflict retains the whole proposal
  without publishing any file; startup and explicit recovery can roll forward
  partially applied batches without overwriting third states.

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
rendered update. It does not scan for unmanaged external edits or monitor Git
operations. Exact-base checks still reject stale publication independently of
anything the model has seen.

Sessionless background runs and the `fixture_write` driver retain the original
linked-worktree lifecycle described below. This is separate from the planned
hydrated `--workspace isolated` mode, which is still unavailable.

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

The knowledge and vector databases are initialized with their own migration
planes. Graph extraction, embedding providers, semantic retrieval, and
`sqlite-vec` are deliberately deferred until the local execution and recovery
loop is complete.

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
revision, and every session on that checkout binds to it. `llm-coord workspace
status` reports it together with the limits shared mode cannot exceed, which
belong in front of an operator rather than only in the plan. `isolated` is
accepted as a mode name and refused with an explanation rather than silently
downgraded to shared, because hydration of ignored runtime files does not exist
yet.

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
parents can be staged as directories inside scope. Regular-file deletion and
renames use the batch journal; symlink edits and arbitrary shell execution
remain unsupported. Named checks use disposable snapshots. The daemon barrier
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

## Next executable slice

1. Run a repeatable two-session experiment with a live provider: complete a
   multi-file task, introduce a conflicting external edit, interrupt and resume
   execution, and independently test the combined result. Compare completion
   time, model cost, and operator intervention with one agent. The existing
   scripted-provider tests are the deterministic baseline.
2. Extend external-change reconciliation beyond live intent/overlap context, then
   validate the remaining multi-profile authority and migration guarantees.
3. Add isolated mode with explicit environment hydration and publication
   through the same batch protocol, building on the check-snapshot runner.
   Symlinks and binary file operations still need dedicated recovery semantics.
4. Add the remaining execution passes -- read-only planning, independent
   review of the exact diff, and a bounded repair loop -- at the session-aware
   harness boundary.
5. Add `run_command` behind an explicit policy, plus `db backup`, config
   inspection/editing, and retention maintenance in the CLI.
6. Build the opt-in knowledge/RAG and graph planes on top of that durable task
   lifecycle, with `none` and exact-search fallbacks before optional vector
   acceleration.

The full phased design remains in
[`llm-cli-implementation-plan.md`](llm-cli-implementation-plan.md).
