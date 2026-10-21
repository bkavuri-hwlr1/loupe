# Optional agent retrieval

Set `[knowledge] index = "lexical"` in the shared configuration, restart the
selected daemon with `loupe daemon restart`, register the checkout, and run
`loupe index build --repo PATH`. Already-running daemons keep their loaded
policy until restarted. The main agent and exploration helpers then receive
`search_project(query, limit=6)`. Indexing remains off by default.
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
  --budget-tokens TOKEN_ALLOWANCE --timeout 900 --out /absolute/path/results.json
```

Choose a positive integer for `TOKEN_ALLOWANCE` large enough for the matrix.
The runner submits multiline prose as one plain-chat task. The October 21 run
used a disposable uncapped adapter after the user removed its token ceiling;
the command above retains the shipped runner’s cell-level admission limit.

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

## Measurement status: complete; keep indexing off

The [Sol/high comparison](experiments/optional-retrieval-f3-2026-10-21-sol.md)
records all 30 cells and 15 matched pairs. Both arms passed 12 of 15 task
contracts. Retrieval enabled covered 90/102 expected facts, versus 92/102 off;
no pair gained facts. Median paired time increased 6.5 seconds and tool use
increased six calls. Helpers made all seven retrieval calls, across six enabled
cells. Keep indexing off by default and leave F4 gated.

The record combines four unchanged valid cells with 26 fresh cells after fixing
multiline prompt framing. Failed valid cells remain; malformed attempts enter
cumulative accounting only. The equivalent harness fix was applied after the
frozen measurement. Eligible usage is 17,904,476 known tokens plus 76 unknown
attempts; cumulative known usage including excluded and historical attempts is
22,677,799, plus 80 unknown attempts. Only one pair has exact token accounting,
so no overall token-savings claim is supported. The small fixture’s keyword
checks and transport retries limit quality and latency conclusions.

The [Astra pilot](experiments/optional-retrieval-f3-2026-10-18-pilot.md) and
[bounded Sol attempt](experiments/optional-retrieval-f3-2026-10-20-sol.md) remain
separate historical evidence; the user superseded the earlier token ceiling.
