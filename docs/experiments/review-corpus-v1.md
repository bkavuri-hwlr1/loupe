# Independent review corpus, version 1

B6 adds twelve edit tasks to live benchmark suite version 4: eight seeded
correctness defects and four correct-change controls. Defects cover a boundary,
an inverted condition, a missing error path, state reset, a wrong default,
resource cleanup, path containment, and ordering. Each fixture starts correct.
The prompt prescribes one exact replacement; the public smoke check passes the
replacement. Hidden acceptance rejects each defect and accepts its original
reference and every clean replacement. Hidden tests never enter the model's
checkout.

Run the paired corpus with a signed-in non-default profile:

```console
uv run --all-extras python scripts/live_benchmark.py --profile explore-eval --review-paired --repeat 3 --provider codex --model gpt-6-sol --effort xhigh --set explore=false --set review_effort=low --budget-tokens 3000000 --out review-results.json
```

Each case alternates review off/on, reversing the first arm every repetition.
Runs use fresh fixtures, the same main model/effort, and separate daemon
configurations. Progress is written atomically after every task. Token admission
reserves at least 100,000 tokens or twice the largest completed task before
starting another paired task; usage is known after provider requests, so this
is not a provider-side hard billing limit. The result records actual spending,
budget adherence, and incomplete runs explicitly.

Only a successful prescribed edit still present when review starts supplies seed
evidence. A successful task with that evidence and completed hidden acceptance
counts as a measured run. Findings followed by repair edits and passing hidden
tests count as observed review catches/repairs; clean controls with review-driven
edits and failing hidden tests count as harmful repairs. Provider/task failures
remain visible as invalid or incomplete runs. Finding counts alone are not
semantic precision or recall.

Reports include hidden pass rates, harmful repairs, incomplete reviews/timeouts,
review duration, total and review token counts, cache read/write subsets, and
per-case repetition medians. Review/cache tokens are subsets of task usage, not
additional tokens to add again. Subscription usage has no implied API dollar
price, so dollar cost remains unknown. `--compare OLD NEW` includes both arm
summaries. Review stays disabled by default; a recommendation follows the live
measurements, and changing that default remains the user's decision.
