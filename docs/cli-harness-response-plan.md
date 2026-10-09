# Loupe CLI harness: response and execution plan

Status: first response-and-delivery milestone implemented and locally validated;
context management (phase 7, items 1–3), sandboxed commands (item 4), faster
read tools (item 5), the task plan tool (item 6), stdio MCP tools (item 7),
user hooks (item 8), exploration helpers (item 9), read-only web fetch
(item 10), token usage display (item 11), and the live-model benchmark
(item 12) implemented, as are bounded repair (phase 5) and the live behavior
matrix (phase 6); broader harness work remains.
Date: 2026-09-20; phase 7 added 2026-10-02, items 1–3 completed 2026-10-03,
items 4–6 and 9 completed 2026-10-04, items 7–8 and 11–12 completed
2026-10-06, item 10 and bounded repair completed 2026-10-07, live behavior
matrix completed 2026-10-08
Baseline: `b334466`.

## Implementation progress

The first milestone in this plan is implemented and locally validated:

- The driver now separates the complete user-facing answer from the short task
  summary and records completed, blocked, and partial outcomes explicitly.
- A substantive no-tool response can complete a conversational task directly.
  `finish_task` also requires an answer and records its terminal tool result in
  provider-native history before the conversation is promoted.
- Accepted answers and their chunked `model.finished` events commit atomically,
  retain a stable message ID, and replay once across live, attach, restart, and
  fresh-viewer paths.
- Interrupted completion chunks are labeled as partial, including when an
  earlier text event has already displayed part of the response. Version-one
  finished and post-tool checkpoints recover as partial work when they contain
  only a legacy summary; recovery never invents a missing answer.
- The default terminal projection hides successful read payloads, raw tool
  activity, private reasoning, intent/task identifiers, and routine settlement
  bookkeeping. It keeps useful activity, questions, complete answers, and
  actionable failures.
- Read, search, and file-list tools support bounded ranges, pagination,
  continuation metadata, and source snapshots. Partial reads cannot authorize a
  whole-file replacement.
- Verification now reports actual not-applicable, not-run, passed, failed,
  stale, and interrupted states.
- PTY coverage includes 40/80/120-column output, attach and fresh-viewer replay,
  split answer chunks, and a live terminal resize with persistent footer
  geometry.

Post-settlement finalization is enabled for shared edit tasks. The harness
holds an edit task's answer as a draft. After the runner has published or held
the files, it binds trusted settlement facts (publication, verification,
completion, changed paths) to that draft. The draft stands when settlement is
what the agent expected: held for review in normal mode, published in auto mode.
Otherwise one tools-disabled model turn rewrites it from the facts. That covers
auto mode held by stale or failing checks, a diverged checkout, or a
publication that needs recovery. If that turn fails, the answer states only the
facts. The runner finishes the response outside the publication lock but
before the settle transaction, so the answer, the promoted conversation, and
the terminal task state still commit together, and a follower never sees a
settled task without its answer. Unlike the original design, file claims stay
held through this step. It runs only on a surprise and takes seconds, which
keeps claim release in its existing single transaction. A task relaunched after
its facts were bound finishes from those facts without resuming tool work. A
crash after publication but before the answer settles from the publication
journal and leaves the answer missing rather than inventing one. Response-only
retry is the remaining piece for that case.

Repository-instruction discovery, context compaction (automatic, after an
oversized-prompt rejection, and through `/compact`), and provider prompt caching
are implemented (phase 7, items 1–3), as are sandboxed diagnostic commands
(item 4, which also covers phase 5's command capability), faster read tools
(item 5), the `update_plan` checklist (item 6), `explore` helpers (item 9), and
token usage display (item 11). A main-conversation model request that fails
in transit (a connection failure, timeout, reply cut off mid-stream, or 5xx
response) is sent again up to three times, after 2, 6, and 15 seconds, and a
`model.retrying` event shows each retry. A failed request leaves the
conversation unchanged and runs no tools, so this is safe; the failed
attempt's draft is dropped rather than kept as a partial response.
Finalization requests are not retried this way, because their attempts are
budgeted separately (see phase 2). Repair after a failing check is bounded
(phase 5), and the live benchmark runs the phase 6 behavior scenarios. The
remaining roadmap includes response-only retry.

Revalidation against `origin/main` on macOS/Python 3.14 on 2026-09-23 passes
1,233 tests with 83% branch coverage, Ruff, strict mypy, whitespace checks,
and source/wheel builds.
Linux, Python 3.12, and live-provider answer quality remain CI or opt-in release
gates.

## Product outcome

Loupe should deliver the result the user requested, show useful progress while
working, and describe execution outcomes accurately. A repository summary must
explain the repository. “Reviewed the files and prepared a summary” is a work
report, not that answer.

Build this on the existing daemon, tool broker, private candidates, verification
snapshots, questions, mode enforcement, and recovery. Those are valuable
foundations; this plan does not replace the coordinator or terminal libraries.

The first release should fix the answer contract, durable delivery, and default
conversation together. Better tools and longer-running workflows follow.

## What the reported interaction should look like

Illustrative response to “Give me a summary of what this repo is doing”:

```text
❯ Give me a summary of what this repo is doing

Loupe
Loupe is a terminal coding agent designed to let multiple sessions work
in the same Git repository without silently overwriting each other's edits.

• The CLI provides conversation, model selection, and plan/normal/auto modes.
• A local daemon owns tasks, questions, event history, and recovery.
• Agents prepare private edits. Publication checks that the source has not
  changed underneath them, and configured checks run against snapshots.
• Provider adapters connect the harness to Codex, OpenAI, and Anthropic.

Its strongest feature is coordinated editing and recovery. Current limits
include no general command tool and limited conversation-context management.

❯
─────────────────────────────────────────────────────────────────────────
Model: … · Effort: … · Mode: normal
```

While inspecting, one temporary activity row can say “Reading project structure”
or “Checking the execution flow.” It disappears when the answer is ready. Keep
the violet divider and persistent model/effort/mode display adjacent to the input.
Do not print routine intent records, task IDs, verification warnings, elapsed
reports, or “no file changes needed” into this read-only conversation.

## Confirmed causes in the current implementation

| Current behavior | Consequence | Primary code |
| --- | --- | --- |
| Default prompts and `finish_task` ask for a summary of changes | Encourages a report of work instead of the requested deliverable | `agent/harness.py`, `agent/tools.py` |
| `finish_task.summary` is silently limited to 2,000 characters | Cannot serve as complete answer storage | `agent/tools.py:436` |
| The loop stops immediately after `finish_task` | No requirement to produce the missing answer | `agent/harness.py:429` |
| A no-tool response is nudged once, then treated as stalled on another no-tool turn | Ordinary conversational completion is treated as a failure to use tools | `agent/harness.py:394` |
| `RunResult` carries a summary but no explicit completion outcome | Stalled or blocked work can be treated like successful execution | `agent/driver.py`, `execution/shared.py` |
| `model.finished.summary` becomes a normal assistant message | Execution bookkeeping is promoted into the answer | `cli/render.py:531` |
| Final checkpoint and completion event are separate writes | A crash can preserve completion without its user-visible response | `agent/harness.py:312`, `agent/harness.py:448` |
| Verification labels depend on configured checks rather than actual check records | A read-only task gets an irrelevant warning; configured-but-unrun checks can imply success | `execution/shared.py:201`, `execution/shared.py:273` |
| Own intent notices and task/elapsed hints print by default | Operational details crowd out the conversation | `cli/render.py:265`, `cli/session.py:370` |

These paths explain the reported behavior. The pasted output alone does not
prove whether that particular provider ever generated additional answer text.
The new regression must cover both missing-answer generation and lost delivery.

## Core contract

### Separate the deliverable from permissions

The user may ask for an explanation, repository overview, review, plan, change,
or diagnosis. Treat that requested deliverable as the objective of the turn.
Task type is guidance for the model, not a keyword router or a source of authority.

Keep the existing permission modes:

- **Plan:** inspect, explain, ask, and plan; no edits or command execution.
- **Normal:** prepare and verify private changes; publication requires `/apply`.
- **Auto:** prepare, verify, and publish eligible changes within existing authority.

A question in normal mode does not require edits. A question in plan mode should
still receive an answer, not be rewritten into an implementation-planning request.
Changing modes must not publish older proposals or alter an already running task.

### Separate answer, report, and execution facts

Introduce versioned result types shared by the harness, runner, and projection:

| Record | Contents | Authority |
| --- | --- | --- |
| Answer draft | Complete Markdown deliverable and message identity | Model-authored; provisional until accepted |
| Task report | Short operational summary and declared completed/blocked/partial intent | Internal metadata; never an answer substitute |
| Execution outcome | Completed, blocked, partial, cancelled, or failed; references to retained artifacts | Runner derives from model termination plus actual execution state |
| Publication outcome | None, held for review, applied, conflicted, or uncertain | Coordinator and publication journal |
| Verification evidence | Actual commands/checks, exit states, source/candidate/config identities | Check runner |
| Final answer | Accepted answer plus references to the settled facts it describes | Durable conversation record |

Keep these dimensions separate: code can be applied even if answer generation
fails afterward; a complete answer can require no code or tests; retained edits
can exist after cancellation. A provider failure must not erase those distinctions.

Store answer bodies separately from short summaries. Preserve complete text using
bounded chunks and explicit size limits; never silently truncate it to fit an
audit field. If an answer exceeds its limit, return an explicit size outcome or
an accessible text artifact, not a misleading successful partial answer.

### Completion and finalization

1. Accept a validated, substantive no-tool `end_turn` as an answer candidate.
   Empty output, refusals, token-limit stops, interrupted responses, and tool
   protocol errors have distinct outcomes. Do not nudge a complete answer merely
   because it did not call `finish_task`.
2. Change `finish_task` into completion intent with an explicit `answer`, an
   optional short `summary`, and a declared outcome. Match every tool call with
   a native tool result before checkpointing or making another provider request.
3. The runner validates candidates, verification, and publication according to
   the existing mode. Move completion gates out of the finish-tool-only path so
   native no-tool completion cannot bypass required checks, scope, cancellation,
   or publication policy. Failed checks can return to the bounded repair stage.
4. Reuse a complete read-only answer draft after no-change settlement. This path
   requires no additional model request.
5. For mutating tasks, or when an answer is missing, allow **at most one
   tools-disabled finalization request after settlement**. Supply the original
   request, draft, relevant evidence, and trusted outcome. It produces the actual
   answer, not another report of having prepared it. Count the attempt durably
   against the task's deadline and token budget. Do not use English phrase
   matching to decide whether an earlier draft promised publication.
6. If finalization fails, preserve completed work and any partial response. Show
   an explicit “work saved; answer interrupted” or blocked/partial outcome with
   a response-only retry action. Never rerun edits just to regenerate the answer.

Structural checks can reject an empty answer or invalid protocol, but cannot
prove arbitrary prose satisfies the request. Prompt guidance and behavior
evaluations handle semantic quality; do not pretend a phrase blacklist is a
reliable answer-quality validator.

For streaming, distinguish public commentary, provisional answer, accepted final
answer, tool activity, and private provider state. If an adapter cannot establish
a text block's role yet, buffer it until the completed turn establishes the role.
Mutation-result drafts must not be printed irreversibly as final before settlement.
Finalization after settlement can stream immediately. Already complete drafts can
render directly. Make this latency/correctness tradeoff explicit.

**Decision (hybrid streaming).** The renderer decodes the `answer` argument of a
`finish_task` call from its streamed arguments (`cli/partial_json.py`).

- *No edits in the task:* the draft streams into the conversation as it is
  written. The completion gate only applies when there are candidate edits, so a
  read-only draft is rejected only for structural reasons (empty or oversized).
  If that happens, a "revising this answer" notice follows the draft and the
  accepted answer is labeled as revised.
- *Edits in the task:* the draft appears only as a transient footer preview. It
  enters the conversation once `model.finished` accepts it.
- *Plain-text turns:* their role is unknown until the turn completes, so they are
  published only as `model.answer.preview` events for that preview. Older clients
  ignore this event type.

`model.finished` remains the single committed answer and reconciles with
whatever already streamed, so live output and replay agree.

## Implementation sequence

### 1. Answer and outcome semantics — first priority

**Scope:** `agent/harness.py`, `agent/tools.py`, `agent/driver.py`, provider
interfaces/adapters, and both shared and isolated execution runners.

- Add the result types and update task prompts around requested deliverables.
- Implement the no-tool completion path and bounded finalization path above.
  Reserve final-response context and deadline budget at task launch, rather than
  spending the entire budget on inspection or editing.
- Distinguish stalled, budget-exhausted, refused, blocked, and failed work from
  successful completion; stop unconditional promotion of `RunResult` to success.
- Derive verification labels from current check records. Represent not applicable,
  not run, passed, failed, stale, and interrupted distinctly.
- Preserve broker enforcement, exact-base checks, retained edits, and native
  provider history across every new terminal path.

**Exit criteria:** the reported prompt produces an actual overview; a direct
answer needs no redundant model turn; a blocked task cannot report success;
configured-but-unrun checks cannot report “Passed.”

### 2. Durable conversation delivery

**Scope:** control storage/migrations, harness checkpoints, daemon attach,
provider snapshots, and conversation event projection.

- Persist accepted answers, their completion event, and the terminal answer
  checkpoint transition atomically with an idempotency key such as
  task/attempt/message-role/revision. Do not attempt one transaction spanning
  provider requests or filesystem publication.
- Make finalization resumable: checkpoint the settled execution facts and answer
  state so restart can finish delivery without repeating mutation or publication.
  Persist finalization-attempt accounting before dispatch; an uncertain provider
  outcome must not reset the automatic-attempt budget. Explicit response-only
  retry is a separate recorded operation, not an automatic retry loop.
  It creates a new response revision using the original settled execution facts,
  without reacquiring publication authority or rerunning edits.
- Use stable message IDs and offsets/sequences for delta assembly and replay;
  stop relying on matching answer prefixes to identify duplicates.
- Use the same conversation projection for live output, attach, resumed sessions,
  and durable history. Distinguish continuing from a saved cursor from explicit
  full replay. Mark interrupted text incomplete until its completion arrives.
- Version events and checkpoints. Read existing checkpoints/transcripts without
  inventing missing answers. Label legacy summaries as task summaries when needed.
  New checkpoint formats must be recognized before a task is accepted by an older
  daemon; preserve the existing restart/version compatibility checks.
  Replaying old tasks never silently starts a paid model request. Preserve legacy
  finish-tool calls during recovery and expose missing answers honestly.
- Include the accepted final answer and matched terminal tool results in the
  provider-native conversation promoted to the next user turn, so follow-ups see
  the same answer the user saw.
  Keep the session's current-task reservation through finalization even after
  releasing file claims. Commit answer state and promoted history before allowing
  a follow-up, so the next prompt cannot race against an unfinished response.
- Keep private reasoning and native replay material out of public conversation
  events. Preserve provider-required blocks privately, without reinterpreting
  Anthropic thinking deltas as OpenAI-style public summary text.

**Exit criteria:** an answer longer than 2,000 characters survives restart and
reattach exactly once; a crash between settlement, checkpointing, and event
delivery cannot lose the final answer or replay tools.

### 3. Conversation and terminal presentation

**Scope:** `cli/render.py`, `cli/session.py`, `cli/status.py`, `cli/shell.py`,
`cli/streaming_markdown.py`, and PTY tests.

- Render an explicit allowlist of conversation events. Unknown internal events
  stay in diagnostics rather than becoming transcript lines.
- Default transcript: user prompt, occasional useful public progress, questions,
  complete answer, and actionable outcome when relevant.
- Keep one changing activity row. Describe observable work such as reading,
  editing, checking, or waiting. Never summarize hidden chain-of-thought.
- Hide own intent, session IDs, raw read results, tool arguments, routine success
  events, and unconditional elapsed/attach instructions. Keep operational detail
  available through `/status`, `/tasks`, `/checks`, `/changes`, and JSON events.
- Surface peer activity only when it matters: overlapping work, a relevant source
  change, a conflict, or a wait. Prefer a concise explanation over an opaque ID.
- Read-only answer: return to the composer without a verification or no-change
  banner. Edited task: show a compact, factual outcome such as “3 files ready for
  review · required checks passed · /diff · /apply.” Show “applied” only after the
  coordinator confirms publication.
- Preserve native scrollback, selection, violet divider, persistent footer,
  syntax colors, narrow-terminal support, plain/no-color output, and sanitization.
- Bound Markdown buffering so a long paragraph or unfinished code fence does not
  look frozen. Flush partial text safely on disconnect or cancellation.
- Define idle, working, waiting-for-input, stopping, detached, reviewing, and
  failed UI states. Derive composer availability and hints from those states.
  Preserve the requested single/double Ctrl+C behavior and durable questions.

**Exit criteria:** the example interaction matches the product behavior above;
errors and review actions stay visible; replay and live rendering agree; footer
and input survive resize, multiline output, picker use, and questions.

### 4. Context and inspection tools

**Scope:** tool schemas/brokers, provider conversation state, repository context.

- Add numbered line-range reads, focused search context, and pagination or
  continuation for directory/search results. Include explicit truncation metadata.
- Preserve private overlays and source identities. A partial read must not grant
  permission to overwrite unseen bytes or silently refresh an edit's exact base.
- Discover applicable repository instructions with bounded file sizes, path scope,
  precedence, and provenance. Repository content never enlarges tool authority.
- Track context usage and reserve space for the answer and finalization. Compact
  only at completed tool boundaries; preserve user intent, decisions, pending
  questions, candidate references, evidence, and valid native tool-call pairs.
- Retain the full local transcript separately from compacted model context. Mark
  summarized information and require fresh reads before acting on old file state.

**Exit criteria:** long conversations can continue without silently losing task
constraints; large files can be inspected without repeated whole-file reads;
compaction and restart together preserve the pending work and its authority.

### 5. Complete the coding and debugging loop

**Scope:** harness stages, existing check snapshots/process supervisor, workflow
outcomes, and bounded command capabilities.

- Make inspect → act → verify → repair → answer an explicit bounded workflow.
  Simple questions skip editing/check stages. Reviews report findings or clearly
  state none were found. Complex tasks retain a small task plan and update it when
  evidence changes the approach.
- Allow targeted reproduction and diagnostic commands through a governed extension
  of the snapshot runner. Record argv, cwd, environment policy, timeout, output,
  cancellation, and exit status. Reuse command results as evidence.
  *Implemented as `run_command` (phase 7, item 4): argv, cwd, timeout, sandbox,
  outcome, and screened output are recorded as durable task events; results
  reach the model as tool results. A dedicated evidence store is not built.*
- Do not introduce arbitrary model-generated shell execution in the shared
  checkout. A snapshot alone is not a sandbox: command capabilities require an
  enforceable filesystem/network/credential boundary, or an explicit supported
  trust policy that accurately reports weaker guarantees. Keep this capability
  disabled where its required boundary is unavailable.
- Bound automatic repair attempts and detect repeated no-progress failures.
  Ask only for missing decisions or information that tools cannot recover. Preserve
  partial work and explain the specific blocker when the bound is reached.
  *Implemented: after a check first fails, three more failing runs of it, or the
  same failure three times in a row (ignoring timings and run identifiers), stop
  the repair. Edits, checks, and commands are then refused, the task finishes as
  partial with its edits retained, and a `repair.exhausted` event says why. Each
  check result tells the agent how many attempts remain. The counts are
  checkpointed, so a restart keeps them.*
- Run an additional diff review for complex/risky changes when justified, rather
  than imposing another model call on every question or trivial edit.

**Exit criteria:** a scripted failing check can drive one corrective edit and a
passing recheck; exhausted repairs produce a partial/blocked result; cancelled
commands settle their process groups; unsupported execution never silently
weakens shared-workspace guarantees.

### 6. Behavior evaluation and release gate

Build the evaluation fixtures alongside each phase, beginning with phase 1.
Use deterministic providers for protocol/lifecycle contracts and a separate,
explicitly invoked live-provider suite for actual answer quality. Live runs use
disposable repositories, selected accounts, and declared usage budgets.

| Scenario | Required result |
| --- | --- |
| Repository summary | Describes purpose, architecture, workflow, and material limits; not an account of reading files |
| Explanation / direct question | Answers directly; no forced finish-tool loop or editing ceremony |
| Review with and without findings | Actionable evidence or an explicit no-findings result; no invented changes |
| Plan in any permission mode | Ordered requested plan; no unrequested implementation |
| Normal / auto edit | Correct private/applied outcome and actual check evidence |
| Necessary clarification | One durable question; answer resumes the same task without duplicate prompting |
| Long Markdown/code answer | Complete beyond 2,000 characters, syntax-colored where supported, no duplicate final |
| Missing answer after finish intent | One bounded finalization attempt; honest partial outcome if it fails |
| Check failure / stale verification | No false “Passed”; repair or retained proposal with next action |
| Cancellation / provider failure | Partial response retained; saved work and incomplete answer reported separately |
| Conflict / restart / two CLI viewers | No duplicate mutation, publication, answer, or question |

Track answer completeness, unsupported factual/verification claims, unnecessary
tool/model calls, time to meaningful progress, final-answer latency, duplication,
and user interventions. Establish a baseline first; compare provider/model
combinations on the same fixtures. Do not use a model judge as execution authority.

*Implemented as the live-provider half of this gate in `scripts/live_benchmark.py`
(task set version 2). Besides the original repository questions, it runs a
repository summary, a direct question, a review with a planted bug and one of
correct code, a plan request in auto mode, a fix that a required check
verifies, a fix whose tests contradict each other, an ambiguous request that
needs one question, a long guide, and an edit task that is cancelled, or whose
service is killed and restarted, after its first edit. Edit and review tasks run
in small fixture repositories (`scripts/benchmark_fixtures/`), so they are
cheap and repeatable. Each task is scored on behavior, from its durable events:
final task state, model outcome, whether it edited, what its checks reported,
questions asked, answer length, model turns, and that every tool result and
answer part was recorded exactly once and the run made one task. A missing
answer after a finish intent, provider failures, conflicts, and two attached
viewers remain covered by the deterministic suites, since a real model cannot
be made to produce them on demand.*

*The first full run with Codex (2026-10-08, 14 tasks, about 25 minutes and
2.5M prompt tokens) passed 12 tasks and found two problems. A direct question
in normal mode spent an extra model turn calling `validate_changes` with no
edits, because the shared-workspace prompt asked for it before every finish;
the prompt now asks for it only after editing, and that task dropped from four
model turns to three. The contradictory-tests fix asked which test should win,
a fair question that the benchmark could not yet answer; answers are now given
through the daemon only when the agent asks, and the task finishes partial
with its edit held and the failing check reported.*

CI remains Linux/macOS × Python 3.12/3.14 with subprocess coverage. Include a
restricted control-thread pool, split PTY writes, resize, 40/80/120-column screens,
NO_COLOR, redirected output, and injected crashes at finalization boundaries.
Update tests that currently require summary-as-answer or no-change banners.

### 7. Parity with established coding-agent CLIs

Compared with widely used coding-agent CLIs, the harness already has stronger
safety foundations: execution limits, private candidates, source policy, and
durable recovery. These items close the remaining gaps in what the agent can do
and how much it costs. Items are ordered by impact. Each keeps the
existing authority model: repository and tool content never grants authority.

| # | Capability | Status |
| --- | --- | --- |
| 1 | Context-window management and compaction | Implemented, including `/compact` |
| 2 | Provider prompt caching | Implemented |
| 3 | Repository instruction files (`AGENTS.md`, `LOUPE.md`) | Implemented |
| 4 | Governed shell execution with an OS sandbox | Implemented (`run_command`, shared tasks) |
| 5 | Faster read-only tools | Implemented; concurrent execution deferred |
| 6 | Visible task plan tool | Implemented (`update_plan`) |
| 7 | MCP client support | Implemented (stdio servers, tools) |
| 8 | User hooks around tool calls and completion | Implemented (`pre_tool`, `post_edit`) |
| 9 | Subagents for broad exploration | Implemented (`explore`) |
| 10 | Read-only web fetch | Implemented (`web_fetch`) |
| 11 | Token usage display (`/usage`, footer context meter) | Implemented; no cost estimates |
| 12 | Live-model task benchmark | Implemented (`scripts/live_benchmark.py`) |

**1. Context-window management (implemented).** Each provider turn reports the
tokens it occupied (`ModelTurn.context_tokens`, counting cached prompt tokens),
and providers expose an `input_token_budget`. Anthropic uses the published
window (default 200k) minus the output reservation. OpenAI and Codex use the
catalog window minus the reservation, or defaults: GPT-5-family (and, by
assumption, GPT-6) models get their separate 272k input limit, o-series models a
shared 200k window, and other models a conservative 128k window. Before each
request the harness estimates the next prompt size. At 80% of the budget it
asks the model, with tools disabled, for a structured handoff summary: requests
and constraints, decisions, files changed, check results, open questions, and
next steps. The summary then replaces the native history. Compaction happens
only at safe boundaries: before the opening prompt, before a nudge, or with a
turn's pending tool results, which the summary request answers without
recording them, so a failed summary leaves the results to be sent as usual. A
dedicated `continue` checkpoint phase makes restart after compaction resume
without resending results. At most one summary is made per model turn, and a
prompt alone never triggers a summary of a near-empty history. Summaries are
bounded, screened for secrets, framed as a record rather than instructions, and
labeled as possibly stale so the model rereads files before acting. After a
summary inside a task, Loupe restates that task's original opening prompt
(instructions, claimed scopes, base) instead of trusting the summary to keep
it. A coordination update attached to summarized results stays unconsumed and
is delivered again with the continue prompt. A failed summary is reported
(`model.context.compaction_failed`) and the task continues. The full transcript
remains in the durable event log.

Recovery after a rejection: adapters classify only precisely identified
oversized-prompt rejections as `provider_error: context_overflow`, because a
false match would replace history with a lossy summary and still fail.
Anthropic: HTTP 413, a 400 saying "prompt is too long" or "input length and
`max_tokens` exceed context limit", or a reply stopped with
`model_context_window_exceeded`. Responses and Codex: the
`context_length_exceeded` code, or a message saying the input "exceeds the
context window" or "maximum context length is". A failed request leaves native
history unchanged, and a reply cut off by the window is discarded rather than
shown as partial text. The harness then summarizes once and sends the same
prompt again, or summarizes with the rejected tool results and continues
through the `continue` phase. A second rejection in the same turn, or a failed
summary, fails the task with the original error, which the terminal shows with
a `/compact` hint. If the summary request itself is too long, it is retried
with tool arguments and results shortened to 4,000 and then 500 characters, in
that request only. Summary requests never disable Anthropic's refusal fallback
for later turns.

Manual compaction: `/compact` sends `session.compact` to the daemon, which
summarizes the idle session's saved conversation outside the authority lock and
saves the replacement only if the conversation revision is unchanged and no
task started meanwhile. Saved conversations now carry `context_tokens`,
including after the deferred final response, so the next task's first estimate
uses real usage rather than a size guess.

**2. Prompt caching (implemented).** Anthropic requests use top-level automatic
caching, which places the breakpoint at the last block so system prompt,
tools, and earlier turns are reused across the agent loop. Summary requests keep
the same tools so they also hit the cache. OpenAI and Codex requests send a
`prompt_cache_key` derived from the model and first conversation item, so a
conversation's requests route to the same cache until it is summarized. The
Codex subscription endpoint also needs that key as a `session_id` header: with
the key alone, a byte-stable six-turn conversation had 0–20% of its input
served from cache, and with the header 92–99%. The live benchmark found this,
because broad tasks reported a few percent of their prompt tokens cached.

Live check, 2026-10-03, Codex `gpt-6-sol` at low effort: the endpoint accepted
`prompt_cache_key` and tools-disabled summary requests. Summaries made no tool
calls, and after the history was replaced the model still answered from the
summary. Two of four follow-up requests reported about 1,280 cached tokens (the
system prompt and tools); the others reported none, consistent with best-effort
provider caching. Anthropic caching has not been checked live.

**3. Repository instructions (implemented).** `AGENTS.md` and `LOUPE.md` are read
from the repository root and from each directory leading to the claimed scopes,
shallowest first; deeper files take precedence. Limits: 32 KiB per file, 64 KiB
and 8 files in total. Symlinked, ignored, excluded, non-UTF-8, and
secret-bearing files are skipped. The text is placed in the system prompt with
its source path, framed as repository guidance that cannot widen tools, scopes,
or mode. The first task of a conversation emits `model.instructions.loaded`
with the paths used. File text cannot open or close its labeled frame, and
paths are escaped. User-level global instructions are a possible follow-up.

**4. Governed shell execution (implemented).** `run_command` runs a model-chosen
argv list in a disposable copy of the checkout with the task's pending edits
applied: tracked and untracked non-ignored files are copied, ignored dependency
folders (`.venv`, `venv`, `node_modules`, configured `runtime_paths`) are linked
read-only, and there is no `.git` directory. The copy is sandboxed by Seatbelt
(`sandbox-exec`, deny-by-default profile, paths passed as parameters) on macOS
or bubblewrap on Linux, and the tool is offered only when a probe command starts
successfully. Inside the sandbox: no network except Unix sockets in the copy and
its private home; writes only there; the real checkout, Loupe's directories, and
common home-directory credential stores unreadable; an environment of `PATH`
plus fixed settings. Arguments with recognized secrets are refused, output after
recognized secrets is withheld, the model receives the first 4 KiB and last
28 KiB of output, and a timeout (default 120 s, maximum 600 s) and task
cancellation stop the process group through the existing check supervisor.
Copies are deleted after each command and at daemon startup. The policy is
`agent.commands`: `ask` (default) asks the user before each command in
interactive sessions, with "allow once", "allow for this task", and "deny";
`allow` runs without asking; `off` disables commands. Plan mode and isolated
background tasks are not offered commands. Limits: other files on disk remain
readable; localhost TCP is unavailable on macOS; tools that need the network or
write into dependency folders fail; editable installs that point at the real
checkout cannot be imported from it; each command copies the whole source tree.
CI installs bubblewrap so the real Linux sandbox is tested.

**5. Faster read-only tools (implemented; concurrency deferred).** The
original plan was to run a turn's reads, searches and listings concurrently.
Profiling the shared-checkout path showed that this would not help yet: every
shared read holds the daemon-wide publication lock, so threaded reads took as
long as sequential ones, and most of each call's time went to starting Git
processes for source-exclusion checks (up to four per directory searched). Those
checks are now cheaper:

- Within one tool call, the user's `core.excludesFile` setting is read once,
  and user rules are evaluated in one reusable, owner-private empty repository
  rather than a new one per check. A changed setting applies from the next
  tool call.
- A search lists the tree first, checking the exclusions of up to 1,000
  entries per Git process, and then reads files in the same depth-first order
  as before, so its results and limits are unchanged. Listing stops once it
  has found as many files as a search may scan; the search lists any further
  directory it reaches. `read_file` no longer checks the same path twice.
- A search skips files with recognized secret material and reports how many it
  withheld (`withheld_files`), instead of failing the whole search.

On this repository, a shared `search_text` dropped from about 750 ms to about
150 ms, `read_file` from about 45 ms to 20 ms, and `list_files` from about
38 ms to 25 ms.
Concurrent execution remains worthwhile for slow tools: it now applies to
`explore` helpers (item 9) and should extend to web fetch and MCP tools
(items 10 and 7), or to shared reads if they move to a shared/exclusive lock. Isolated tasks still check exclusions per directory during a search.

**6. Task plan tool (implemented).** `update_plan` records an ordered checklist
of at most 12 steps, each pending, in progress (at most one), or completed. The
tool is offered outside plan mode, whose answer is itself a plan; the system
prompt asks the model to use it for multi-step work and skip it for quick
tasks. Each update emits a durable `plan.updated` event: the terminal prints
the checklist once per change and keeps the step in progress above the status
footer. The plan is saved with the checkpoint's tool usage, restored on resume,
and restated after a context summary as the model's own record rather than new
instructions. Step text is collapsed to one line, screened for recognized
secrets, and sanitized before display. In the shared checkout the tool does
not take the publication lock.

**7. MCP client (implemented for stdio tools).** Configured Model Context
Protocol servers (`[mcp.servers.NAME]`: `command`, `env`, `approval`,
`timeout`, `cwd`) start for each shared task outside plan mode, through a
small synchronous stdio JSON-RPC client in `llm_cli/mcp/`. The client performs
the initialize handshake, lists tools across pages, calls them with
timeouts, answers pings, and declines server-to-client requests, since it
declares no client capabilities. Each tool reaches the model as
`mcp__SERVER__TOOL` with a bounded, validated schema and a description naming
the server. Their tools are external authority: `approval = "ask"` (the
default) asks the user before the first call to each server in a task, with
the same once, rest-of-task, or deny choices as `run_command`, and such a
server is not offered without someone to ask. Servers get a minimal
environment plus their configured variables and run in the user's home unless
configured, so no write scope over the checkout is implied. Results are text
only, bounded, withheld if they contain recognized secrets, and labelled as
external data. `mcp.server.started` and `mcp.server.failed` events show which
servers are available. Remaining gaps: HTTP transports, MCP resources and
prompts, offering read-only MCP tools in plan mode, and isolated-workspace
tasks (which do not offer `run_command` either).

**8. Hooks (implemented).** User-configured commands around the agent's tool
calls in shared tasks, run with the same governance as item 4's commands: a
disposable sandboxed snapshot of the checkout with pending edits, no network,
and no access to the real checkout. A `[[hooks.pre_tool]]` hook matches tool
names (glob patterns), reads the call as JSON from `$LOUPE_HOOK_INPUT`, and
blocks it by exiting with status 2, its output becoming the reason; other
failures are reported and do not block. A `[[hooks.post_edit]]` hook matches
edited paths and runs with `{paths}` expanded; changes it makes to those
paths are read back and staged through the broker like the agent's own edits,
so they are reviewed before they apply, and the agent is told to re-read them.
Hooks run outside the shared publication lock. Completion verification
remains the configured checks (`run_check` and the finish gate), so there is
no separate completion hook. `hook.blocked`, `hook.changed`, `hook.failed`,
and `hooks.unavailable` events show hook activity without hook output.

**9. Subagents (implemented as `explore`).** The `explore` tool hands a
self-contained question to a helper: a new session with the same provider, its
own system prompt, and only `list_files`, `read_file`, `search_text`, and
`read_diff`. The helper reads through a view of the task's broker, so the
source policy and pending edits are the same, but its observations are its
own: a helper's read never authorizes the task's full-file writes. Up to 40
tool calls are reserved from the task's budget for each helper, always leaving
five for the task, and unused calls are returned. Helper reads are capped at
100 files and 384 KiB per exploration. The helper's report (at most 16,000
characters) returns as the tool result; the agent is told to rely on it for
understanding but to read a file itself before editing it. Because helpers
only read, a request that fails in transit (a connection failure, timeout,
dropped stream, or 5xx response) is sent again, up to twice per exploration,
after 2 and then 6 seconds. Provider failures, a spent budget, or a cut-off
report become an explicit partial or failed result rather than a task failure.
A helper that fails without a report lists the files it read and the searches
it ran, so the agent can continue from there, and the conversation shows why
it stopped. Helper token usage is added to the task's
total. Consecutive `explore` calls in one turn run in parallel (up to four);
their results are recorded together, and after a restart an interrupted group
runs again. The conversation shows each exploration by a short label the
model provides while it runs, and a one-line result when it finishes. Helpers
are offered in every mode, including plan mode, and `agent.explore = false`
disables them.

A live check with a Codex subscription model at `xhigh` effort, on this
repository, found:

- **Choice:** the model answered a narrow question with direct reads, used
  one exploration per question for three independent questions, and explored
  before editing in a coding task.
- **Accuracy:** every sampled path and line citation in the reports was
  correct, and the reports listed what they had not checked.
- **Re-reading:** the first guidance called reports unverified, and the agent
  re-read about 35 cited ranges after receiving them. With the guidance to rely
  on reports, the three-question task fell from 39 to 5 tool calls in the main
  context, and its peak context from 36k to 10k tokens.
- **Cost:** for one broad trace question, exploring kept the main context
  smaller (46k against 78k tokens) but used more tokens in total than
  answering without helpers. At the task's `xhigh` effort, helpers often used
  the full 40 calls and took about 2.5 minutes each.
- **Helper effort:** helpers now default to `low` effort, set by
  `agent.explore_effort` and never above the task's. With the main
  conversation still at `xhigh`, helper time fell to about half (63–100s for
  five of six helpers, against 116–164s). Total input tokens fell 36% on the
  three-question task and 20% on the broad trace. Answers covered the same key
  steps and facts, and sampled citations in the low-effort reports were all
  correct. These are single runs per scenario.

Remaining gap: helpers cannot run sandboxed commands.

**10. Web fetch (implemented).** `web_fetch(url, offset)` reads one public page
as text through the standard library's HTTP client (`llm_cli/web/`). Domain
policy: `agent.web_fetch` is `ask` (default; approve each new domain once, for
the rest of the task, or deny), `allow`, or `off`, and `agent.web_domains`
lists hosts or `*.` subdomain patterns that need no approval. Only http(s)
URLs without credentials or recognized secrets are fetched; every resolved
address must be public, the connection is pinned to the checked address, and
each of up to five redirects is checked and approved the same way. Requests
are GET with no cookies, credentials, or proxies, a 20-second timeout, and a
2 MiB download limit; only text types are read. HTML becomes readable text
with headings and links kept; long pages are read in parts through `offset`
from a per-task cache; output is labelled as external content and screened
for secrets. It is offered in plan mode, since it reads, but not to
exploration helpers. `web.fetched` events record the domain and size only.
Remaining gaps: proxy support and fetching through exploration helpers.

**11. Token usage display (implemented).** Adapters now report `prompt_tokens`
with one meaning for every provider: all prompt tokens, cached or not
(Anthropic's `input_tokens` excludes cached tokens; Responses' includes them).
Each `model.turn.completed` event carries `context_tokens` and
`context_budget`, and summaries carry the budget, so the status footer shows
how full the context window is ("Context: 26%") and updates as turns land.
`/usage` sends `session.usage` to the daemon, which totals the session's task
runs from their recorded usage, falling back to the latest checkpoint for runs
that are still running or failed, and reports the current context size and
budget. Explore helper tokens count in the totals, and `/usage` also shows the
helpers' share, recorded as `explore_prompt_tokens` and `explore_output_tokens`.
The context meter covers only the main conversation. Older runs recorded before `prompt_tokens` count their `input_tokens`,
which undercounts cached Anthropic prompts. Loupe shows tokens, not dollar
cost: prices depend on the provider and plan, and ChatGPT subscriptions are
not billed per token. The meter appears after the first turn of a visit or a
`/usage`; it is not fetched when a session opens.

**12. Live-model benchmark (implemented).** `scripts/live_benchmark.py` runs
a small, versioned set of real tasks (`scripts/live_benchmark.toml`) with a real
model, to catch regressions when prompts, tools, or settings change. It is
opt-in and billed, so it never runs in CI. It runs against a signed-in profile
other than `default`, whose background service it restarts with a temporary
configuration built from `--set` options. Nobody is at the terminal, so that
configuration runs sandboxed commands without asking and fetches no web pages
unless `--set` says otherwise; a question left pending after the chat exits
cancels the task instead of stalling the run. Repository questions run in a
clean clone of this repository at the task file's pinned revision; edit, check,
and review tasks run in fresh fixture repositories with their checks
configured. Typed answers can follow the prompt, and a task can be cancelled or
have its service killed and restarted after its first edit. Each task records
the behavior checks described in phase 6, which expected facts the answer states
(each fact lists acceptable wordings), whether it used `explore` when expected
or avoided it when not, main-conversation turns and tool calls, peak context,
prompt, output, cached, and helper tokens, and each exploration's state, calls,
time, retries, and effort. Wall time is reported with the time the machine
slept during the task, so slept runs are not compared as if they were slow. A
token budget (default 3M) stops the run before the next task once spent.
Results are JSON; `--compare OLD NEW` shows per-task changes. No model judges
answers.

**Exit criteria (items 1–3):** a conversation that exceeds the input budget
continues after one summary without losing task constraints; restart after a
summary resumes without duplicate tool results; an oversized-prompt rejection
is recovered once rather than failing the task; repeated prefixes report cache
reads; repository instructions appear in the system prompt only when the source
policy allows them. Remaining gaps: a live Anthropic cache check and a live
oversized-prompt rejection on each provider.

## Delivery order and boundaries

Phases 1 and 2 define the shared contract. Phase 3 can proceed against that
contract in parallel, but the first release includes all three. Phase 4 follows;
phase 5 builds on its context/evidence support. Phase 6 is a continuous release
gate, not a final testing project.

Keep each implementation slice independently testable. Publish only after the
full CI matrix passes; run a real-model check of the exact reported prompt before
calling response quality solved. Deterministic tests alone cannot establish it.

Defer RAG/vector indexing, remote orchestration, and broad binary/symlink
editing. None is needed to make an ordinary prompt receive a complete,
trustworthy answer. MCP, hooks, and subagent delegation are scheduled in
phase 7 after the safety-critical phases, under the authority constraints
stated there.

The first milestone is explicit: **ask the pasted repository-summary question,
receive an actual overview once, reattach and recover that same answer, and see
no irrelevant execution bookkeeping in the default conversation.**
