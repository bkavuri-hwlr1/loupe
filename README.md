# Magnifio

Magnifio is a coding agent that lives in your terminal. It shows concise progress
such as “Reviewing project files” and “Running checks,” followed by readable
assistant messages and results. Markdown and syntax-colored code appear as
complete blocks arrive. Routine file reads, tool payloads, and reasoning text
stay out of the conversation. Answers remain in your terminal scrollback, with
labeled dividers between messages and a single “Done” after a task completes.

## Install on Mac

On macOS 15 or newer, with [Homebrew](https://brew.sh) installed:

```sh
brew install magnifiosearchengine/tap/magnifio
magnifio
```

Apple Silicon and Intel Macs are supported. Python and both provider SDKs are
included automatically; no source checkout or Python setup is needed.

For upgrades, removal, and migration from a previous `uv` installation, see the
[installation guide](packaging/README.md). Maintainers can follow the
[release guide](docs/macos-releases.md).

## Start here

```shell
magnifio
```

Magnifio opens immediately. Type `/login` whenever you're ready and choose:

- **Codex:** sign in through your browser with a ChatGPT subscription.
- **Anthropic:** enter a Claude API key in a hidden prompt.
- **OpenAI:** enter an OpenAI API key in a hidden prompt.

The provider picker also offers **Connect later**. Your account choice, model,
and effort level are remembered for the current profile. `/model` opens a
searchable list of models for the connected account, followed by the effort
levels that the selected model supports. `/effort` changes that setting later;
**Provider default** lets the service choose. Model and effort appear in the
header and `/status`. Opening `/model` automatically checks for newly available
models when its five-minute account cache expires. Use `/model --refresh` to
fetch the list immediately.

The picker opens at the recommended or newest models: Codex uses the provider's
priority, and API connections use model creation dates, with model versions as a
fallback when ordering metadata is missing. Older available models remain
selectable. Refreshing the list keeps your saved model and effort unchanged;
select a model and its effort setting, when offered, to switch.

Codex also gates visibility by client compatibility version. Magnifio requests
version `0.155.0`. If a future release requires a newer compatible catalog
version, set `LLM_COORD_CODEX_MODELS_CLIENT_VERSION` to that `major.minor.patch`
version before launching Magnifio, then run `/model --refresh`. This updates
discovery without changing the selected model or its supported effort controls.

The picker distinguishes models returned by the account from cached or reference
lists when discovery is unavailable. OpenAI API, Codex subscription, and Anthropic
connections use their own model lists and capability information. Models without
advertised effort controls do not get an invented effort menu.
Effort choices are also checked against what the connection accepts, including
cached lists. If a saved setting is no longer supported, use `/effort` to choose
another level. Provider failures show one error with a suggested next step.
Codex subscription requests retry connection failures up to twice before a
response begins. Timeouts, service rejections, and interrupted response streams
are not retried automatically.

You can switch accounts with `/provider`, enter a model ID directly with
`/model MODEL_ID`, inspect accounts with `/accounts`, or remove a saved login
with `/logout`. Changing provider, model, or effort starts a fresh conversation
after the current task is finished. Cancelling either picker preserves your
previous selection and conversation.

Start in your Git project folder, or choose one inside Magnifio with `/cd PATH`.
The repository is registered automatically when you first use it. You can
explore the interface and connect accounts outside a Git repository too.

For development, install from this source checkout once:

```shell
uv tool install --editable '.[openai,anthropic]'
uv tool update-shell
```

Open a new terminal after installing. For checkout-only development,
`uv sync --all-extras` followed by `uv run magnifio` works too. `magnifio demo`
previews the conversation display without a model connection.

After upgrading a checkout that already has a daemon running, let its active
tasks finish and run `magnifio daemon restart` to load the new code. Subsequent
in-app logins are loaded on each new task without restarting the daemon.
Earlier events that stored only character counts cannot recover old text.

Existing `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` environment settings are also
recognized. Explicit provider/model flags remain available for scripts.
API keys use separate provider billing; a ChatGPT subscription connects through
the Codex option. `magnifio --help` lists all commands.

Chat uses the whole repository as its default scope and displays it in the
header. Narrow it with `--scope src/ --scope tests/` or `/scope` during chat.
New chats start in **normal** mode: edits stay private until you review and
apply them. Choose a mode with `magnifio --mode plan`, `magnifio chat --mode auto`,
or `/mode plan`, `/mode normal`, and `/mode auto` inside the conversation.

| Mode | What the model can do |
| --- | --- |
| `plan` | Read and search the checkout, ask necessary questions, and propose a plan. Write tools and check commands are disabled. |
| `normal` | Prepare private edits and run configured checks. Inspect `/diff`, then use `/apply` to publish. |
| `auto` | Prepare edits, run required checks, and publish completed work automatically. Essential clarification questions remain available. |

Use `/mode` to see the current mode and choices. Switch between tasks; the
current task keeps its launch mode through disconnects and daemon restarts.
In the interactive editor, **Shift+Tab** cycles plan → normal → auto while
preserving your draft and cursor. Model, effort, and mode stay below the input,
separated by a violet divider. The editor follows the conversation without
padding to the bottom of the terminal; switching modes updates the status in
place without adding transcript lines.
The same status remains visible during task output and clarification questions.
Shift+Tab is disabled while answering a task's
question, and mode changes are refused while a task is running. Use `/mode`
when using plain input or a terminal that does not send Shift+Tab.
Plans remain in the conversation when you switch to normal or auto and ask the
model to implement them. Switching to auto does not apply older held proposals.
Existing sessions retain their prior behavior when upgraded or resumed.
`--publish review` and `--publish auto` remain aliases for normal and auto;
conflicting mode/publication flags are refused.

| In the conversation | What it does |
| --- | --- |
| Enter / Alt+Enter | Send the prompt / insert a new line |
| Shift+Tab | Cycle plan → normal → auto without submitting or clearing the draft |
| Up, Down / Ctrl+R | Navigate / search input history |
| `/` then type | Search command names and descriptions |
| Up, Down / Tab / Enter / Esc in the command menu | Browse without changing your query / fill in a command / run the selected command / dismiss the menu |
| Page Up, Page Down in the command menu | Browse six commands at a time |
| `/login [PROVIDER]` | Connect an account now or skip until later |
| `/provider`, `/accounts`, `/logout [PROVIDER]` | Choose an AI, inspect accounts, or remove a saved login |
| `/model`, `/models` | Search account models and select supported effort |
| `/model MODEL_ID`, `/model --refresh` | Set a model directly / refresh the account model list |
| `/effort [LEVEL]` | Choose thinking effort; `default` restores the provider default |
| `/mode [plan\|normal\|auto]` | Show or change the mode for future tasks |
| `/diff`, `/apply` | Inspect and publish the last task's prepared changes |
| `/cd PATH` | Choose a project folder |
| `/help`, `/status` | Show commands or workspace details |
| `/scope PATH...` | View or change editable paths; quote paths containing spaces |
| `/changes` | Show and acknowledge other sessions' published changes |
| `/tasks`, `/attach [TASK_ID]` | List this session's tasks / replay and follow a task |
| `/history`, `/clear` | Show prompts entered during this visit / clear the display |
| `/detach` | Keep the conversation available to resume; print the resume command |
| `/exit`, Ctrl+D | Close an idle session; preserve it if its task is still active |
| Ctrl+C during a task | Detach the viewer while the task keeps running |
| Ctrl+C twice within 2 seconds | Exit the CLI; preserve the session if a task is active |

The input editor supports multiline paste. Prompt history is in memory for the
current visit. Enter runs the selected command from the search menu immediately.
To add arguments first, press Tab to fill in the command, type the arguments,
then press Enter. `/clear` clears the display without resetting model context.
When essential information is missing, the model can pause and ask a question
in the same editor. Enter a free-text answer or the number of a suggested
choice; the task continues with your answer. Models are instructed to inspect
available context first and make routine implementation decisions themselves.
At a question, `/stop` cancels the task and retains pending edits. Ctrl+C leaves
the question pending; `/attach TASK_ID` returns to it. Replaying an answered
question does not ask it again. Questions wait up to ten minutes; after that,
the model is instructed to explain any missing information instead of guessing.
To resume a detached conversation, use the command printed by `/detach`,
including its profile, repository and scope.

`magnifio run "your task" --scope src/ --follow` follows a background task.
Add `--interactive` instead of `--follow` to let a one-shot run ask questions
and collect answers in the terminal. It implies following the task and cannot
be combined with `--json` or `--claim-only`. Background runs cannot ask questions.
To use modes for a one-shot task against the shared checkout, specify `--mode`:

```sh
magnifio run "Plan the API change" --scope src/ --mode plan --interactive
magnifio run "Implement the API change" --scope src/ --mode normal --follow
magnifio run "Fix the typo" --scope docs/ --mode auto --follow
```

An explicit `run --mode` creates a durable shared conversation and prints its
resume command. Normal-mode proposals can be published with
`magnifio task apply TASK_ID`. Without `--mode`, `run` retains its existing
isolated-worktree behavior and publishes a task result ref, not checkout edits.
Mode runs cannot be combined with `--claim-only` or `--fixture-write`.

`magnifio task watch TASK_ID` replays its conversation and follows live work.
`magnifio --json task watch TASK_ID` produces the full newline-delimited event
trace, including tool calls and results, for explicit inspection.
`--plain`, piped output, and `TERM=dumb` provide a plain-text display; `NO_COLOR`
disables colors. JSON remains available for noninteractive commands.

The normal display hides reasoning text, routine tool arguments and results,
check logs, and token counters. One activity line describes the current action
without quoting the model's reasoning. Assistant answers and questions remain
complete; code blocks are syntax-colored, and failures show a short diagnostic.
Use `/diff` for changes, `/checks` for check logs, or the JSON event trace for
details. Hidden/internal reasoning, encrypted reasoning blocks, and signatures
are never displayed. The full provider-exposed transcript, including tool
arguments and results, is saved in owner-local task events; other sessions of
the same OS user can inspect it. Peer models receive coordination metadata,
not these transcripts. Operational daemon logs do not receive transcript text.
Saved API keys live in owner-only profile files outside the project. They are
entered separately from the prompt editor and never enter input history,
task events, or daemon RPC. Logging out removes the saved login; environment
credentials, if present, remain configured until removed from the environment.

The `magnifio` and `magnifiod` commands are the public names. The `llm-coord`
and `llm-coordd` aliases, Python distribution name and existing storage/config
paths remain compatible, so older commands in the reference below still work.

See [the terminal architecture decision](docs/adr/0008-magnifio-terminal.md).

## Verified coding workflow

Configure named test, lint, and build commands with `magnifio checks configure
--file checks.toml`. The agent runs required checks against private snapshots
containing its pending edits. Failed or stale verification retains the proposal
for review. `chat --publish review` also retains successful proposals until applied.

Use `/diff`, `/checks`, `/apply TASK_ID`, `/undo TASK_ID`, and `/stop` to inspect,
apply, revert, or stop individual tasks. Shared file tools now support directory
creation, deletion of regular text files, and renames. Ctrl+C still detaches.

See [the verified workflow guide](docs/verified-workflow.md) for configuration,
CLI equivalents, dependency setup, check gating, and recovery behavior.

## Coordination architecture

This repository is the early foundation for a provider-neutral terminal coding
agent with coordination built into its harness. Think of a small, extensible
coding-agent CLI in the spirit of Pi, except every instance working on the same
local repository discovers the others through a per-user daemon and exchanges
durable coordination state before publishing changes.

The product is a coding agent with Anthropic and OpenAI provider adapters.
The harness, tool loop, durable execution state, and coordination lifecycle are
independent of the selected provider.

> **Project status:** an early, developer-runnable safety foundation. The local
> coordinator, daemon, storage, Git broker, supervised agent harness, durable
> multi-prompt sessions, interactive task stream, execution path, and crash
> recovery work end to end. Shared chat sessions prepare private edits
> concurrently, including overlapping scopes, and publish bounded multi-file
> batches into the checkout. Hydrated isolated workspaces, retrieval, and graph
> features remain ahead; live-model collaboration benchmarks have not been
> completed.

The Python distribution remains `llm-coord` for compatibility; the user-facing
product and terminal command are Magnifio and `magnifio`.

## What the project is intended to provide

- Durable, fenced claims over repository path scopes. Eligible shared-session
  tasks prepare concurrently; overlapping work involving an exclusive claim
  retains FIFO scheduling.
- A per-user daemon that is the sole writer to coordination authority.
- Shared chat sessions with private candidate edits and exact-base publication;
  linked Git worktrees for background tasks and the fixture driver.
- Independent scope and base validation before shared publication, and trusted
  Git change-set validation for linked-worktree results.
- Shared results applied to the checkout without changing HEAD, the index, or
  refs; background results retained on internal refs for explicit integration.
- Local lexical retrieval, exact-vector retrieval, and a bounded relational
  knowledge graph, all able to degrade safely when optional components are
  unavailable.
- A provider-neutral coding-agent harness with replaceable model adapters, so
  coordination correctness does not depend on a hosted model or SDK.
- Local recovery, audit events, handoff context, and bounded retention.

The coordinator is designed for cooperative processes owned by the same user.
It is not a hostile-code sandbox and does not turn a local agent process into a
security boundary.

## Architecture at a glance

The planned architecture has four deliberately separated responsibilities:

1. `llm-coord` is a thin CLI and never mutates Git or SQLite authority directly.
2. `llm-coordd` owns coordination state, scheduling, reconciliation, and trusted
   Git operations through a versioned local RPC protocol.
3. Each editing task holds a durable, fenced scope claim. Shared chat tools
   prepare a private candidate overlay; background tasks use managed linked
   Git worktrees.
4. Repository knowledge is stored separately from coordination authority so
   indexing work cannot delay claim renewal or corrupt scheduling state.

Local persistence is split into three SQLite databases:

| Database | Purpose | Authority |
| --- | --- | --- |
| `control.sqlite3` | tasks, claims, fences, sessions, workspaces, events, handoffs, and publication journals | authoritative |
| `knowledge.sqlite3` | sources, documents, chunks, FTS, user memory, and graph facts | mixed; user memory authoritative, repository projections rebuildable |
| `vectors.sqlite3` | embedding metadata and vector projection | disposable and rebuildable |

The daemon protocol uses versioned, four-byte length-prefixed JSON messages over
an owner-only Unix domain socket. The first supported platforms are macOS and
Linux. Windows transport and worktree behavior are deferred.

## Current technology decisions

- Python `>=3.12,<3.15`, managed and locked with `uv` once project packaging is
  bootstrapped.
- Standard-library `argparse` for the CLI and `asyncio` for local orchestration.
- Direct standard-library `sqlite3` access rather than an ORM.
- Pydantic 2 only where boundary DTO validation justifies the dependency; core
  domain logic should remain plain Python where practical.
- SQLite FTS5 as the always-available lexical retrieval arm.
- Built-in `none` and exact-vector modes; `sqlite-vec` remains opt-in until its
  packaging and corruption/recovery behavior pass the supported-platform test
  matrix.
- A small provider-neutral coding-agent harness with a stable provider/session
  protocol. It is intentionally extensible without adopting a general agent
  framework as a runtime dependency.

The rationale and consequences are recorded in the architecture decision
records, beginning with [ADR 0001](docs/adr/0001-runtime-and-packaging.md).

## Product defaults under development

The initial safety-oriented defaults are:

- plain-text parsing is always available as an ingestion fallback;
- background task results remain on internal refs until explicitly integrated;
  shared chat results publish into the checkout;
- clones sharing a normalized remote identity coordinate by default, with an
  explicit local-only identity override;
- terminal task records are retained for 30 days and bounded handoff records
  for 90 days;
- indexing task memory is opt-in;
- shell execution uses a restrictive approval policy;
- model planning, vectors, graphs, plugins, and automatic integration are not
  on the first safety-critical vertical slice.

## Repository documentation

- [Detailed implementation plan](docs/llm-cli-implementation-plan.md)
- [Implemented and deferred boundaries](docs/implementation-status.md)
- [Runtime and packaging ADR](docs/adr/0001-runtime-and-packaging.md)
- [Daemon RPC ADR](docs/adr/0002-daemon-rpc.md)
- [Storage split ADR](docs/adr/0003-storage-split.md)
- [Linked worktrees ADR](docs/adr/0004-linked-worktrees.md)
- [Vector index ADR](docs/adr/0005-vector-index.md)
- [Product defaults ADR](docs/adr/0006-product-defaults.md)
- [Coding-agent product boundary ADR](docs/adr/0007-coding-agent-harness.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy and threat model](SECURITY.md)

## Current runnable vertical slice

For development, use `uv run` after registering a Git repository. There are two
execution paths: shared chat sessions publish into the working checkout, while
a background `run` without a session uses a detached linked worktree and
publishes only to `refs/llm-coord/tasks/<task-id>`.

```shell
uv run llm-coord init
uv run llm-coord repo add /path/to/repository
uv run llm-coord run "document the retry flow" --repo /path/to/repository --scope docs/
uv run llm-coord task events TASK_ID
```

The coding-agent harness supports three optional providers:

| Provider | Install | Authentication | Default model |
| --- | --- | --- | --- |
| `anthropic` | `uv sync --extra anthropic` | `ANTHROPIC_API_KEY` or `ant auth login` | `claude-opus-5` |
| `openai` | `uv sync --extra openai` | `OPENAI_API_KEY` | `gpt-5.3-codex` |
| `codex` | `uv sync --extra openai` | ChatGPT subscription OAuth | `gpt-5.6-terra` |

To use your ChatGPT subscription with this CLI's own harness and coordination:

```shell
uv sync --extra openai
uv run --extra openai llm-coord auth login codex
uv run --extra openai llm-coord auth status codex
uv run --extra openai llm-coord repo add /path/to/repository
uv run --extra openai llm-coord chat --repo /path/to/repository --scope docs/ --provider codex
```

Login opens a browser. For a device-code login, add `--device` and follow the
printed instructions (device login must be enabled in your ChatGPT settings).
Use the same `--profile` for login, repository registration, and chat. Restart a
daemon running an older version once after installing this provider; subsequent
logins, logout, and token refresh are picked up on the next model request without
a restart.

For a quick test with an existing file-backed Codex login, use
`llm-coord auth login codex --from-codex`. This explicitly copies only the
current access token from `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`).
It never copies or rotates Codex's refresh token or changes Codex's login.
This temporary login expires; use the normal browser/device login for a
refreshable session. Keychain-only Codex logins require normal login here.

`llm-coord auth logout codex` removes this profile's local credentials; it
does not revoke other apps' logins or cancel an already running model request.
Credentials live in an owner-only file under the profile's state `auth/`
directory, outside the repository, task database, conversation history, and
checkpoints. Refresh token rotation is serialized across workers/processes.

The `codex` provider sends model requests directly to the ChatGPT Codex
subscription endpoint. Our harness still owns tools, scopes, private edits,
publication, and recovery; it does not launch the Codex CLI or its agent loop.
It consumes your subscription allowance and never falls back to API-key billing.
Available models and limits depend on your plan. Set `--model` explicitly when
needed; the older `gpt-5.3-codex` API default is not a subscription default.
The OAuth/transport integration follows the public Codex/OpenCode protocol;
this endpoint can evolve separately from the public Platform API.

To keep both SDKs installed, include both extras when syncing or running with `uv`.
For example, with `OPENAI_API_KEY` set in your shell:

```shell
uv sync --extra anthropic --extra openai
uv run --extra anthropic --extra openai llm-coord --profile openai-test repo add /path/to/repository
uv run --extra anthropic --extra openai llm-coord --profile openai-test chat \
  --repo /path/to/repository --scope docs/ --provider openai
```

Use a fresh profile or stop the existing daemon before starting from the shell
where you set the key: a running daemon keeps its original environment and
loaded code. The `openai` provider uses a Platform API key with API billing;
it does not read cached Codex/ChatGPT login credentials. See
[OpenAI authentication](https://developers.openai.com/codex/auth/).

Select an adapter/model per task or new session with `--provider` and `--model`,
or set `[agent] provider` and `model` in the profile configuration. An explicit
provider override uses that provider's default model unless `--model` is also
given. Existing sessions retain their provider and model; start a new session
to switch. Anthropic remains the default provider for unconfigured profiles.

The OpenAI adapter uses the Responses API with `store=false`, preserves native
tool calls and encrypted reasoning in local checkpoints, and rejects incomplete
responses before executing their tools. Both providers retain terminal tool
results so follow-up prompts and restarts have complete conversation history.
The same scope enforcement and publication checks apply to both.

Without a usable adapter or credential, the run fails with
`PROVIDER_UNAVAILABLE` before model-directed edits. Two diagnostic paths share
the lifecycle: `--fixture-write PATH=CONTENT` runs the deterministic fixture
driver, and `--claim-only` acquires or queues a claim without starting the harness.

On the background path, the agent may read eligible source anywhere in its
worktree but may only write inside the claimed scopes. Both reads and file
mutations honor the [source exclusions](SECURITY.md#secrets-and-privacy).
A write outside the claimed scopes returns a
correctable tool error and never reaches the filesystem. Because a driver that
writes around the tool surface is still possible, publication independently
revalidates the real Git object graph, so
the tool boundary is a guardrail and trusted validation is the guarantee.

A task attempt is finished once its claim reaches a terminal state. Requesting
another claim for it is refused rather than silently returning the dead one;
`llm-coord task retry TASK_ID` opens a new attempt, and re-running then
compare-and-swaps the task ref forward from its previous result.

`run` returns once the daemon has durably scheduled the work; it does not hold
the request open while the worker runs. Any local session can poll `task events`
with its last sequence using `--after SEQUENCE` to see durable claim,
worktree, validation, publication, and failure transitions.

The target branch is never changed by that command. Its confirmed result keeps
the claim reserved until an explicit discard or future verified integration.

If the daemon is killed during a linked-worktree execution, the next boot
resolves what it left behind before serving anything, deciding each abandoned
attempt against the repository rather than against elapsed time. A reference
that still names exactly what the publication meant to replace proves the
swap never happened, so the reservation is released and its waiters wake;
a reference already
holding the result means the swap did happen, so the publication is confirmed.
Anything else is unknown, and an unknown outcome stays blocking: the claim
holds no lease, so no amount of elapsed time retires it, its managed worktree
is kept as evidence, and `llm-coord task recover` re-decides it once an
operator has established what the repository actually contains.

Agent runs are bounded: a two-hour wall clock, a 500 tool-call cap, and 64 KiB
per tool result. The work lease is renewed on a timer for as long as the driver
runs, and a lease lost mid-turn is raised the moment the driver returns, so it
can never reach publication.

`llm-coord chat --scope docs/` opens a durable session. The model conversation
spans prompts, but each mutation is still its own fenced task: authority is
granted per task attempt, and a task owned by a session releases its
reservation the moment its result is published rather than holding the path
while you think about the next prompt. Releasing is not discarding -- the
published result stands and the task is recorded as completed. Each session
still accepts one live task at a time, while different shared sessions can
prepare edits to the same files concurrently.

New shared-session attempts use optimistic scheduling when their registered
driver supports recoverable shared edits and enforces scopes. Each claim binds
to its exact shared workspace. Pre-migration attempts, sessionless runs, fixture
tasks, and claim-only requests keep exclusive scheduling. An overlapping
exclusive claim, including an older queued claim, remains a FIFO barrier. The
daemon starts queued work when that barrier clears; migration never changes
the scheduling mode of an existing attempt.

With the built-in `coding_agent` harness, a shared session reads current source
from the actual checkout and overlays its own unpublished edits. Full-file
replacement requires a prior read; `apply_patch` obtains a validated read when
needed. Tool writes remain private in the execution checkpoint until the task
finishes. The daemon then independently checks the claim and every edited
file's observed base before publishing the batch. A subsequent prompt keeps
the saved conversation and reads the updated shared checkout.

Before each model request, a shared task refreshes its checkout revision,
other sessions' active intentions, and committed file paths since the last
update the model received. Overlaps are marked against the task's scopes.
These facts also arrive with tool results during a running task, so the model
can learn about another session's publication before deciding its next action.
The terminal shows `refreshed checkout context` when new context is delivered.

The model's event cursor survives checkpoints, restarts, and subsequent prompts
independently of the terminal's `/changes` acknowledgments. Updates are advisory
snapshots; file reads and final publication still recheck current state. Context
contains metadata rather than source bodies or other sessions' prompts. A
pending update above 1,000 events or 16 KiB is refused explicitly instead of
silently dropping changes. External-editor watching and manual Git
reconciliation remain planned.

Try this with the configured provider, then inspect the task IDs printed by chat:

```shell
uv run llm-coord chat --repo /path/to/repository --scope docs/
uv run llm-coord workspace status /path/to/repository
uv run llm-coord task events TASK_ID
uv run llm-coord task watch TASK_ID --after SEQUENCE
```

A batch supports at most 50 regular UTF-8 files, 256 KiB per file, and 4 MiB of
retained original and candidate bytes per task. Missing parent directories can
be staged within scope. Ignored runtime files
and nested repositories are excluded from shared source tools; symlink edits
and arbitrary shell commands remain unavailable. Named checks run in private
snapshots with explicitly configured dependency setup. `validate_changes` checks scope and stale
bases; it does not run program tests.

A successful batch advances the workspace by one revision and one checkout
event, without changing HEAD, the index, or refs. A preflight conflict retains
the entire candidate batch and publishes none of its files. No-op and read-only
tasks do not allocate a publication revision. Optimistic scheduling changes
when private work can begin; publication still runs through the daemon's short
barrier and rechecks every candidate base. A stale batch fails with its complete
proposal preserved for inspection and retry. These shared guarantees currently
apply within one daemon profile.

`llm-coord task watch TASK_ID --after SEQUENCE` follows any task from any
session. The stream is a replay of the durable event table rather than a live
subscription, so attaching late misses nothing and reconnecting resumes exactly
where it stopped. Detaching never touches the work.

The built-in coding-agent harness checkpoints its provider-native session plus
neutral loop state after each model response and tool result. The checkpoint is
bound to the selected adapter and model. If the daemon stops during a turn, its
next boot resumes the same execution: a shared session restores its private
candidate bytes and observed bases, while a background execution resumes its
locked managed worktree. An interrupted tool is never replayed blindly; the
resumed model receives an uncertainty result and inspects its tool-visible
state before deciding what to do.

Shared publication has a durable batch journal. After a crash, recovery can
finish a partially applied batch when each path still matches either its
recorded base or result. Any third state blocks the checkout for operator
attention rather than overwriting that file. After resolving the discrepancy,
`llm-coord task recover` retries recovery. Ordinary readers that do not honor
the daemon's barrier may observe a multi-file apply in progress.

The sessionless background and `--fixture-write` paths retain their linked
worktree/internal-ref behavior, including starting new tasks at the target ref.
They do not apply results to the shared checkout.

`llm-coord workspace status` reports the workspace a checkout coordinates
through -- its mode, epoch, and revision -- along with what shared mode cannot
promise: only cooperative sessions honoring the publication barrier see a
multi-file batch atomically, and an editor or test run reading paths directly
may observe an apply in progress. `--workspace isolated` is refused with an
explanation rather than silently downgraded, since worktree hydration is not
built.

The initial broker commands make that boundary inspectable:

```console
llm-coord workspace read SESSION_ID docs/guide.md
llm-coord workspace stage SESSION_ID docs/guide.md --content-file /tmp/guide.md
llm-coord workspace publish SESSION_ID CANDIDATE_ID
```

`stage` performs a brokered read, retains the UTF-8 replacement in private
content-addressed storage, and returns its candidate ID. `publish` rechecks the
exact base identity, atomically replaces one regular file from Git-internal
staging, advances the workspace revision, and appends the checkout event. A
stale candidate is retained with durable divergence evidence instead of
overwriting a newer file.

The shared harness integration is exercised by scripted-provider end-to-end
tests using real Git repositories and filesystem changes, including follow-up
prompts, conflicts, and recovery. Those tests make no remote model calls and
do not establish a speed or quality advantage over one agent.

## License

No project license has been selected yet. Unless and until a license file is
added, the repository should not be assumed to grant redistribution rights.
