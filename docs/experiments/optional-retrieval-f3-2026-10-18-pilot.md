# F3 retrieval pilot — incomplete comparison

One of the planned 30 live cells completed. This is a baseline observation,
not evidence for or against retrieval. The first `trace-shared-edit` repetition
ran with retrieval off, on `codex/gpt-6-astra`, effort `xhigh`. It passed the
existing task contract and found 10 of 11 expected answer facts in 419.0 seconds.
The model made 38 main-agent tool calls and 165 total main/helper calls.

Observed usage was 1,145,369 prompt tokens and 18,694 output tokens, totaling
1,164,063. Cached input accounts for 920,192 prompt tokens. Usage includes the
four exploration helpers; it does not imply a dollar charge.

The runner stopped before the second cell: its conservative next-cell reserve
was 2,328,126 tokens, exceeding the 1,835,937 tokens remaining under the
3,000,000-token cohort ceiling. That ceiling was not exhausted. The completed
attempt and its full usage remain in the [machine-readable pilot record](optional-retrieval-f3-2026-10-18-pilot.json).
No paid retries or additional cells were run. Further measurement awaits the
user's budget/model choice. A comparison using a different model or effort
must be a separate matched cohort and retain this pilot in cumulative usage.

The repository fixture is pinned to merged main `6e5d65d148d6b4ac92fc849cb4fd861c63f8b5c3`;
implementation source was `1475acc78795730645f08cf22285712098b8ab19`.
The installed, non-editable wheel's 123 source modules matched that revision
byte for byte. Its SHA-256 and the runner output digest are in the JSON audit.
The three-repeat protocol alternates off/on arm order for five tasks.

**Recommendation: no benefit conclusion yet.** Keep indexing off by default
and F4 gated until the matched live comparison is complete. No answer-quality,
token-cost or latency improvement is established by this single baseline.
