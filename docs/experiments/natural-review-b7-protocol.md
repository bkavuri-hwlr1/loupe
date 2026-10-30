# Natural-task independent-review comparison (B7)

Protocol fixed before live calls on October 30, 2026. This extends the paired B6
harness without prescribing an initial edit or planting a replacement string.
The review default stays off; changing it remains a separate user decision.

## Corpus and provenance

Eight trimmed tasks come from real Loupe fixes. Every fixture starts with the
affected parent-revision code, plus minimal standalone dependency adapters.
Original fix code and provenance are kept outside the model's fixture, under
`scripts/benchmark_acceptance/natural-*/`. Each provenance record names its
parent and fix commits, source and regression-test paths, and baseline/reference
hashes. Original changed functions remain verbatim; prompt framing and the old
inline environment predicate are explicitly documented statement-block lifts.

The corpus covers review report parsing, unsupported configuration values,
documentation heading links, IPv6 CONNECT authorities, private runtime spelling
aliases, multiline prompt framing, environment credential screening and Intel
filesystem discovery. Configuration errors and prompt framing are the two
predeclared obvious-fix controls. They still start with a real defect: whether
their first proposed change is correct is measured, rather than assumed.

Each original baseline must pass its visible smoke check and fail its hidden
regression; each original fix must pass both. The eight baseline/reference pairs
also pass through the same sandboxed hidden runner used by the live measurement.
The model cannot read the hidden tests or the reference fixes.

## Matched execution

- Eight tasks, review off/on, three repeats: 48 cells and 24 matched pairs.
- A fresh fixture per cell. Starting arm alternates off/on, on/off, off/on.
- Non-default profile `explore-eval`, Codex `gpt-6-sol`, main effort `high`,
  review effort `low`, exploration disabled. Both arms use the same settings
  apart from `review`; no task is selected after seeing a model result.
- An explicit large admission budget is passed to the runner. It is not a
  spending target or an agreed stop point; the user has authorized the work
  without a fixed token ceiling. All observed attempts and costs are retained.
- An optional pause file stops admission only between completed cells. It lets
  unrelated scale measurements run quietly without timing out a live cell.
  Pauses are excluded from individual-cell wall times.

## Quality and attribution

Record final hidden-test passes over every attempted cell, task settlement,
incomplete or unavailable attempts, findings, timeouts, main and review tokens,
cache subsets and per-cell wall time. Subscription tokens are not an API dollar
price. No dollar savings claim follows from these counters.

Successful source mutations are replayed only up to the first `review.started`
event. The reconstructed candidate is checked by the hidden oracle. Missing,
ambiguous, unsupported or unfinished mutations make the witness unavailable;
they are never silently treated as an incorrect first proposal. Replay is
bounded to 256 files, 8 MiB total and 1 MiB per file. Later repair edits cannot
change the first-proposal score. Review-off uses its final proposal as its first
proposal. Final acceptance separately tests the actual settled checkout.

A failed-first/pass-final transition with findings and repair edits is a
**candidate catch**, requiring manual inspection of the finding and repair
before being reported as a real review catch. A correct-first/failed-final
transition after a review-driven repair is a harmful repair. Finding counts
alone are not semantic precision. Correct first proposals provide an observed
clean denominator in both obvious and harder tasks.

Large tool arguments use the production transcript's numbered JSON chunks.
Replay requires contiguous parts, matching call/tool/turn identity, an explicit
final part and complete JSON, with a shared bounded assembly buffer. Interrupted
mutations have unknown outcomes and invalidate attribution. A finished review
without its starting boundary also makes the first-proposal witness unavailable.

Retain quality failures and publish their denominators. Setup or provider
failures remain in an attempt/cost ledger; replacements must be identified,
never silently substituted. Completion requires all planned cells and matched
pairs with valid oracle witnesses, rather than merely a runner that exited.

## Decision

The dated result report will provide per-arm and per-task tables, manual catch
adjudications, incomplete attempts, usage and a recommendation. No product
setting changes as part of B7.
