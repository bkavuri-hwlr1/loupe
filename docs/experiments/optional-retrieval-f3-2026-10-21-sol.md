# F3 optional retrieval: completed Sol/high comparison

**All 30 cells and 15 matched pairs are recorded. Keep indexing off by default and leave F4 gated.** Both arms passed 12 of 15 task contracts. Retrieval enabled found 90 of 102 expected facts, versus 92 with it off, and added a median 6.5 seconds and six tool calls per matched pair. No pair gained expected facts. This small fixture does not support promoting retrieval or adding embeddings.

## Protocol and result

The five-task corpus is pinned to `6e5d65d148d6b4ac92fc849cb4fd861c63f8b5c3` (suite 5). Codex `gpt-6-sol` used `high` effort, exploration helpers used `low`, and review was off. Each task ran three times with retrieval off/on; arm order alternated by repetition. The user explicitly removed the previous token ceiling. The disposable runner and observer imposed no token quota, admission queue or artificial request delay. The original task timeout and provider retries remained.

All tasks completed. The six contract failures are the three `trace-shared-edit` pairs: off found 7, 8 and 8 of 11 facts, while on found 7, 7 and 7; the threshold is nine. Every other task met all listed facts in every repetition. These are fixed keyword checks, rather than an independent semantic assessment of the answers.

| Task | Passed off / on | Facts off / on | Median paired seconds, on − off | Median paired tool calls, on − off |
|---|---:|---:|---:|---:|
| `trace-shared-edit` | 0/3 / 0/3 | 23/33 / 21/33 | +51.7 | +21 |
| `repo-summary` | 3/3 / 3/3 | 15/15 / 15/15 | +22.4 | +6 |
| `where-source-privacy` | 3/3 / 3/3 | 18/18 / 18/18 | -1.2 | +10 |
| `where-index-updates` | 3/3 / 3/3 | 18/18 / 18/18 | -16.7 | +3 |
| `where-final-answer` | 3/3 / 3/3 | 18/18 / 18/18 | -4.2 | -7 |

The overall +6.5-second result is the median of the 15 matched time differences, not the difference between pooled arm medians. All 15 pairs had no detected suspension. Connection retries occurred, so the wall times include transport variation as well as agent work; they do not isolate retrieval latency. Index builds were outside task timing: median 1.061 seconds, 16.115 seconds across all 15 enabled cells.

Retrieval remained optional. It was called seven times across six of the 15 enabled cells, entirely by helpers; main agents made no retrieval calls. The off arm made none. This measures the effect of availability under ordinary tool choice, not forced retrieval. Three repetitions in one repository provide limited evidence and no statistical-significance claim.

## Framing repair and retained results

The initial uncapped attempt exposed a harness defect: plain chat treats each physical input line as a new task. The multiline privacy prompt created four tasks, while the runner scored only the first. The three discovery prompts contain four or five lines. Those malformed attempts are excluded from paired scores because they violate the one-prompt/one-task contract; all their usage remains accounted for.

Four already-valid single-line cells were retained unchanged: repetition 1 off/on for `trace-shared-edit` and `repo-summary`. Both failed trace results remain. Their wire bytes are identical under the repair. The other 26 keys were selected before resuming. The disposable input adapter replaces internal CR/LF with spaces and sends one final newline; it preserves every word and produces exactly one task. No valid cell was retried to improve a score.

The final record explicitly combines those four retained cells with 26 fresh cells. Raw outcomes are preserved: the interrupted component has 5/30 recorded rows and exit 130; the fresh component has 26/30 and exit 1 because its frozen runner still expects 30. Independently checking the disjoint keys establishes complete aggregate coverage, without rewriting either raw completion flag or exit code. Both usage ledgers are closed.

After measurement closed, the equivalent three-line framing fix was applied to the shipped harness. Its tests drive the real benchmark input path into the real plain-chat reader. All 30 corpus submissions byte-match the measured adapter. This follow-up passed 42 focused tests, Ruff and formatting checks; it did not generate additional paid cells or alter the frozen measurement.

## Token accounting

| Bucket | Known observed tokens | Attempts with unknown usage |
|---|---:|---:|
| Eligible 30-cell comparison | 17,904,476 | 76 |
| Excluded malformed framing attempts | 2,335,824 | 3 |
| All current uncapped experiment | 20,240,300 | 79 |
| Earlier Astra pilot and capped Sol attempts | 2,437,499 | 1 |
| **Cumulative** | **22,677,799** | **80** |

Within the eligible comparison, off has 8,413,707 known tokens plus 31 unknown attempts; on has 9,490,769 known tokens plus 45 unknown attempts. These are known portions, not exact arm totals. Only one of 15 pairs has complete token accounting. No exact overall token increase, savings or dollar cost is claimed. Unknown attempts do not prove provider receipt and are not treated as zero consumption. No new finite bound is inferred for them.

The historical capped-Sol request retains its separate 1,178,000-token conservative bound. That bound is not measured usage, is not added to the observed-token table and does not bound the uncapped experiment. Cached input and helper counters are subsets of input/output usage and are not added again. Eligible checkpoint totals equal the eligible known transport subtotal in this run; that agreement does not recover unknown usage.

The [Astra pilot](optional-retrieval-f3-2026-10-18-pilot.md) and [bounded Sol attempt](optional-retrieval-f3-2026-10-20-sol.md) remain unchanged historical records. They contribute to cumulative accounting, never to this matched comparison.

## Frozen provenance and validation

Product source remained `6c69ac3fa9336f4aa9259f2bf2dba86c1f438ada` throughout measurement; `b7b503c652dc2b9ff319692deb141a6ff41c5d0e` is its documentation-only checkout head. All 123 installed modules matched the freeze, and source/test/script trees were unchanged through closure. The wheel SHA-256 is `9b6a427f6681d11984973ef1a70ace93adf0c37d9bf9706d150e4038e7ddccaa`. The frozen runner hash is `0863003c70b22d111797fd8b2ceffb81d297933d36064b3dc92d183f76b9b490`; corpus hash is `d6faf771dd9fe640d74574819b87cccac4c53cbf48119aa0241defd6c32ae740`.

The accounting observer passed 27 fake-SDK tests; the uncapped runner adapter passed 14, input framing 25, reporting 14 and component merging 14 offline tests. Independent review reconciled raw rows, both closed ledgers and their hashes. Each of the 27 fresh daemon starts proved observer activation before generation. Process-scoped idle-sleep prevention was verified; no recorded cell detected suspension. The owned daemon stopped and left no active claim or task execution.

The [machine-readable record](optional-retrieval-f3-2026-10-21-sol.json) includes all 30 cells, every pair, observed/unknown counters and source/selection hashes. Raw logs, identities, ledgers and executable instrumentation remain in an owner-private audit bundle. The normal runner now includes the equivalent framing fix; its documented `--budget-tokens` option is still cell-level admission, unlike this experiment’s disposable uncapped adapter.

## Every matched pair

Deltas are on minus off. Known-token differences are exact only where explicitly marked. All failures remain in the tables.

| Task / repeat | Pass off / on | Facts Δ | Known tokens Δ | Exact token accounting | Tool calls Δ | Seconds Δ |
|---|---|---:|---:|---|---:|---:|
| `trace-shared-edit` #1 | False / False | +0 | -185,659 | no | +23 | +98.6 |
| `repo-summary` #1 | True / True | +0 | -107,491 | no | +6 | -29.7 |
| `where-source-privacy` #1 | True / True | +0 | +160,407 | no | +21 | +58.8 |
| `where-index-updates` #1 | True / True | +0 | +246,295 | no | +5 | +21.9 |
| `where-final-answer` #1 | True / True | +0 | +53,433 | no | +20 | +84.9 |
| `trace-shared-edit` #2 | False / False | -1 | +294,596 | no | +21 | +51.7 |
| `repo-summary` #2 | True / True | +0 | +315,364 | no | +26 | +22.4 |
| `where-source-privacy` #2 | True / True | +0 | +235,219 | no | +10 | -1.2 |
| `where-index-updates` #2 | True / True | +0 | -61,286 | yes | -10 | -24.8 |
| `where-final-answer` #2 | True / True | +0 | -67,497 | no | -7 | -4.2 |
| `trace-shared-edit` #3 | False / False | -1 | +197,116 | no | +20 | +6.5 |
| `repo-summary` #3 | True / True | +0 | +173,379 | no | +4 | +26.3 |
| `where-source-privacy` #3 | True / True | +0 | -261,102 | no | -21 | -37.3 |
| `where-index-updates` #3 | True / True | +0 | -143,566 | no | +3 | -16.7 |
| `where-final-answer` #3 | True / True | +0 | +227,854 | no | -10 | -39.2 |
