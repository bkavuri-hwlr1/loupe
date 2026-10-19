# Optional agent retrieval

Set `[knowledge] index = "lexical"`, register the checkout, and run
`loupe index build --repo PATH`. The main agent and exploration helpers then
receive `search_project(query, limit=6)`. Indexing remains off by default.
The existing `search_text` and file tools remain available for exact discovery.

The tool returns an `optional_retrieval` block containing up to six lexical
snippets and one hop of code-graph evidence from the first lexical match (or
query when there are no lexical matches). Graph output is capped at 20 edges
and 24 nodes; total output is capped at 24 KiB and the task's smaller allowance.
Each snippet retains its path, identity, indexed revision, line range and
active generation. Graph evidence retains its parser/version and generations.
Snippets longer than 2,500 characters carry `text_truncated`.

Retrieval rechecks current source policy and content identities in the task's
own source root, including an isolated worktree. Changed content is labeled
`stale indexed version`; pending task edits, deletions and renames mark matching
indexed hits and graph evidence stale, including in helper views. Pending bytes
are never substituted into the index. Newly ignored or secret-screened source is hidden.
Results are untrusted discovery data. Read current source before editing.
Neither retrieval nor helper retrieval creates a full-file edit observation,
acknowledges events, advances coordination cursors, or authorizes publication.
Fresh mandatory coordination facts precede optional results at model boundaries.
If both cannot fit, the optional block is omitted with a notice when space allows.

Calls consume the normal tool-call allowance and returned bytes consume the
read-byte allowance. Helpers use their reserved parent task budget. Metric events
record only lookup state, byte count and whether the call came from a helper.
Missing, dropped, locked or damaged indexes produce an advisory notice and leave
source tools usable. Retrieval opens the existing optional database read-only;
it does not initialize stores, schedule indexing or change worker health.

## Measuring usefulness

The F3 corpus is pinned to `6e5d65d148d6b4ac92fc849cb4fd861c63f8b5c3`.
It includes `trace-shared-edit`, `repo-summary`, and three discovery questions
about privacy, index updates and answer finalization. Run the same model and
effort with index off/on three times per task; arm order alternates each repeat:

```sh
python scripts/live_benchmark.py --profile NON_DEFAULT_PROFILE \
  --retrieval-paired --repeat 3 --provider PROVIDER --model MODEL --effort EFFORT \
  --budget-tokens 3000000 --timeout 900 --out /absolute/path/results.json
```

This is a billed live comparison and does not run in tests or CI. Use an already
signed-in, non-default profile; the harness restarts its service with temporary
configuration. Existing account credentials are not changed. `knowledge.index`
is selected per arm; each on-arm builds the pinned checkout's index before the
measured task starts. Results separately record build time, answer fact coverage,
failures, tokens, all main/helper tool calls and retrieval usage. All completed
attempts, including failures, contribute to spent tokens. The aggregate token
ceiling is admission control; usage becomes known after a model request, so it
is not a hard provider billing limit. No API dollar cost is inferred from
subscription tokens. Do not retry paid cells merely to improve the score.

`--compare OLD NEW` prints comparison tables and retrieval arm summaries.
A default-setting change and F4 embeddings require the recorded comparison's
recommendation; neither follows merely from implementing this tool.

## Initial pilot status

The [retained pilot](experiments/optional-retrieval-f3-2026-10-18-pilot.md)
completed one off-arm baseline using Codex `gpt-6-astra` at `xhigh`, consuming
1,164,063 observed prompt/output tokens including helpers and cached input.
Admission control stopped before the second cell under the 3,000,000-token
cohort ceiling. The full comparison awaits the user's budget/model choice.
This single baseline cannot establish retrieval benefit; indexing stays off
by default and F4 remains gated.
