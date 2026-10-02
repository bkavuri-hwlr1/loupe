# Loupe CLI harness: response and execution plan

Status: first response-and-delivery milestone implemented and locally validated;
broader harness work remains.
Date: 2026-09-20
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

The durable post-settlement finalization state machine is scaffolded behind a
disabled capability gate. Enabling it still requires the runner/coordinator
transaction that records trusted settlement facts, releases file claims while
keeping the session task reserved, consumes the one model attempt durably, then
commits the answer, promoted conversation, and terminal task state together.

The remaining roadmap includes repository-instruction discovery and model
context compaction, governed diagnostic commands, bounded repair stages,
response-only retry, and the full deterministic/live-provider behavior matrix.

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
- Do not introduce arbitrary model-generated shell execution in the shared
  checkout. A snapshot alone is not a sandbox: command capabilities require an
  enforceable filesystem/network/credential boundary, or an explicit supported
  trust policy that accurately reports weaker guarantees. Keep this capability
  disabled where its required boundary is unavailable.
- Bound automatic repair attempts and detect repeated no-progress failures.
  Ask only for missing decisions or information that tools cannot recover. Preserve
  partial work and explain the specific blocker when the bound is reached.
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

CI remains Linux/macOS × Python 3.12/3.14 with subprocess coverage. Include a
restricted control-thread pool, split PTY writes, resize, 40/80/120-column screens,
NO_COLOR, redirected output, and injected crashes at finalization boundaries.
Update tests that currently require summary-as-answer or no-change banners.

## Delivery order and boundaries

Phases 1 and 2 define the shared contract. Phase 3 can proceed against that
contract in parallel, but the first release includes all three. Phase 4 follows;
phase 5 builds on its context/evidence support. Phase 6 is a continuous release
gate, not a final testing project.

Keep each implementation slice independently testable. Publish only after the
full CI matrix passes; run a real-model check of the exact reported prompt before
calling response quality solved. Deterministic tests alone cannot establish it.

Defer RAG/vector indexing, general plugins/MCP expansion, autonomous subagent
delegation, remote orchestration, and broad binary/symlink editing. None is needed
to make an ordinary prompt receive a complete, trustworthy answer.

The first milestone is explicit: **ask the pasted repository-summary question,
receive an actual overview once, reattach and recover that same answer, and see
no irrelevant execution bookkeeping in the default conversation.**
