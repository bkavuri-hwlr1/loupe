# ADR 0008: Magnifio terminal interface and local execution transcripts

Status: accepted

## Context

The coordination foundation can execute coding tasks, but lifecycle counts alone
do not explain what the agent says, which tool it is running, or why it needs
input. Magnifio needs a usable terminal conversation with visible progress and
replay while retaining the existing daemon, recovery, and workspace guarantees.

Model text and tool output are untrusted content. Making them visible to a local
operator must not turn them into instructions for another coordinated model.
Likewise, a transcript of an old question must not answer a newer live question.

## Decision

1. **Magnifio is the product name.** The primary commands are `magnifio` and
   `magnifiod`. Keep `llm-coord` and `llm-coordd` as compatibility aliases. Keep
   the Python package, distribution identity, `LLM_COORD_*` environment variables,
   profile paths, database schemas, and protocol version compatible. Renaming the
   user interface must not orphan existing sessions, credentials, or work.

2. **The terminal is a client of the existing control plane.** Rich formats the
   conversation, tools, status, and results. Prompt Toolkit provides an editable
   input composer. These libraries belong to the terminal presentation layer;
   the daemon and execution authority continue to use the existing RPC and
   storage services. Argparse commands and JSON output remain available for
   scripts. Plain and redirected output retain readable chronological text.

3. **Separate the conversation from its execution trace.** The harness retains
   assistant text, provider-exposed reasoning, tool arguments and results,
   summaries, and lifecycle events. The human display shows complete assistant
   messages, questions, concise errors, and a single changing activity line
   derived from lifecycle and tool names. Routine reads, payloads, reasoning,
   check logs, and usage counters remain available through explicit JSON event
   inspection. Render stable Markdown paragraphs and fenced code blocks as they
   arrive, with syntax colors and a flush on completion or interruption. Plain
   streams remain immediate. Never invent a model's private reasoning or expose
   encrypted reasoning, signatures, or opaque native state as terminal content.
   Provider-native replay state remains owned by adapters.

4. **Display does not grant execution authority.** Partial tool arguments are
   informational. Tools run only after the provider returns a validated,
   completed turn, through the existing bounded broker. Terminal formatting
   treats text as data and neutralizes terminal control sequences, including
   sequences split across streamed chunks.

5. **Transcripts remain local task events.** Persist display events in
   `task_events`, replayed by sequence through `task.attach`. The owner-only
   profile and Unix socket are the access boundary: sessions of the same OS
   user can inspect task transcripts. This is local privacy, not isolation
   between that user's sessions. Do not copy transcript text into
   `checkout_events` or peer model context. Shared coordination continues to
   project only bounded, known metadata such as paths, revisions, and intents.

6. **Bound live event delivery.** Coalesce tiny provider deltas and chunk large
   transcript fields before persistence so individual JSON frames remain below
   the existing 4 MiB protocol limit. Preserve the available text across those
   chunks. Tool execution's existing output and write limits still apply.
   `task.events` also bounds its entire response page by encoded bytes; callers
   advance after the last returned sequence until a page is empty, because a
   short page can still have more history behind it.
   Model turn identifiers separate resumed or retried output from incomplete
   earlier turns. Commentary persistence remains best effort if the database
   fails; it must not replace authoritative checkpoints or fail a valid edit.

7. **Resolve only the matching live question.** Assign each pending question an
   opaque `question_id` and include it in question events. The read-only
   `task.question` RPC reports the currently pending question independently of
   replay history. The terminal checks that identity before prompting and
   includes it in `task.answer`; the daemon rejects stale identities without
   consuming the live question. Identity omission remains supported for older
   clients. Record the answer event before waking a worker that may ask again.

8. **Launch locally and connect when needed.** Bare `magnifio` opens the
   conversation interface without requiring provider flags, a Git repository,
   or a running daemon. `/login` offers Codex subscription OAuth and
   Anthropic/OpenAI API keys, with a connect-later choice. The first task
   registers its existing Git project and opens a durable session. Changing
   providers, models, or projects closes an idle conversation before opening
   another; active or uncertain work retains its session and resume secret.
   Provider-native history never crosses providers.

9. **Keep account data local to the profile.** Provider/model preferences live
   separately from shared configuration. Saved API keys use owner-only files,
   locked atomic updates, bounded reads, and symlink rejection. Secrets are
   entered through a non-echoing prompt outside the composer and are never
   transported over daemon RPC or stored in prompt history. Provider adapters
   read current credentials when each task starts so a login does not require
   restarting a running daemon. Existing environment credentials remain
   supported and remain configured after removing a saved login.

10. **Discover models per connection and persist effort with the conversation.**
    `/model` reads the selected account's model list and offers only advertised
    or documented effort settings; `/effort` adjusts the current model's setting.
    Codex subscription capabilities are separate from OpenAI API capabilities.
    Discovery uses bounded requests and a private account-bound metadata cache;
    cache/reference results are visibly distinguished from fresh account results.
    API secrets never enter the cache. Unknown model capabilities do not imply
    effort support. Nullable effort is stored in the session and launch recipe,
    with omission preserving the provider default. A selected effort reaches
    the provider request, including queued/recovered tasks, without a shared
    mutable default that could affect another conversation. Changing effort
    starts a fresh idle conversation, and cancelling the picker changes neither
    the saved choice nor the active conversation.

## Consequences

- A terminal can attach late or reconnect and render the same stored task
  events, including model text and tool activity. Existing historical events
  containing only counts cannot reconstruct previously omitted text.
- A task's stream ending after an idle timeout does not itself mean the task
  completed. The client must consult task state when deciding whether to follow
  again or return to the composer.
- The conversation hides reasoning text. Explicit event inspection can show
  only what the provider exposes; it cannot offer undisclosed internal reasoning.
- Transcripts increase local control-database storage. They are untrusted
  execution records, not coordination facts, and their presence grants no
  filesystem, publication, or cross-session authority.
