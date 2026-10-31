# Independent review on natural tasks (B7), October 30, 2026

**Recommendation: keep review off by default and available as an opt-in.** The
completed frozen comparison has 48 cells and 24 valid matched pairs. Review off
passed 21/24 frozen hidden checks and review on passed
21/24; neither arm converted a hidden failure into a pass.
Manual inspection verified useful additional repairs outside the fixed oracle,
so equal hidden scores do not mean review found nothing. The observed token and
timing costs, one supported-runtime false positive and narrow corpus do not
justify enabling it by default. No product default changes.

The [fixed protocol](natural-review-b7-protocol.md) and eight historically grounded
fixtures were frozen at `d6ad0335decc71271b69b10ccbc300c6cf22687d` before billed
cells began. Three repeats alternate review-off/on starting order. Main requests
use Codex `gpt-6-sol` at high effort, reviews use low effort, and exploration is
disabled in both arms. Each cell starts from a fresh fixture; there is no planted
replacement string or prescribed first edit. All 48 cells, quality failures and
usage counters are retained in the [unmodified raw result](natural-review-b7-2026-10-30.json).
[Run metadata](natural-review-b7-2026-10-30-run.json) contains independent
recalculation, timing exclusions and sanitized manual adjudications. No query,
fixture, oracle or model setting was changed after outcomes were observed.

## Predeclared quality

| Measure | Review off | Review on |
| --- | ---: | ---: |
| Cells / valid first-and-final oracle witnesses | 24/24 | 24/24 |
| First-proposal hidden passes | 21/24 | 21/24 |
| Final hidden passes | 21/24 (87.50%) | 21/24 (87.50%) |
| Retained final quality failures | 3 | 3 |
| Failed-first/pass-final with finding and repair | 0 | 0 |
| Passed-first/failed-final after review repair | 0 | 0 |
| Findings / reports with findings | 0/0 | 8/7 |
| Incomplete reviews / timeouts | 0/0 | 1/1 |

| Case | Off first | Off final | On first | On final | On findings |
| --- | ---: | ---: | ---: | ---: | ---: |
| Review JSON parsing | 3/3 | 3/3 | 3/3 | 3/3 | 0 |
| Configuration errors (obvious control) | 3/3 | 3/3 | 3/3 | 3/3 | 0 |
| Heading links | 3/3 | 3/3 | 3/3 | 3/3 | 4 |
| IPv6 proxy authorities | 3/3 | 3/3 | 3/3 | 3/3 | 1 |
| Runtime path aliases | 3/3 | 3/3 | 3/3 | 3/3 | 2 |
| Prompt framing (obvious control) | 3/3 | 3/3 | 3/3 | 3/3 | 0 |
| Credential variables | 0/3 | 0/3 | 0/3 | 0/3 | 1 |
| Intel filesystem lookup | 3/3 | 3/3 | 3/3 | 3/3 | 0 |

The matched outcomes are 21 both-pass,
0 on-only pass,
0 off-only pass, and
3 both-fail. Both obvious-fix controls are
included in these denominators. A hidden-oracle pass is not a declaration that
all behavior is correct, so it cannot alone define a semantic false-positive
rate. Findings alone do not establish catches or precision.

First proposals are reconstructed from successful, witnessed source mutations
only up to the first review boundary. Contiguous production argument chunks are
reassembled exactly; missing, interrupted or ambiguous traces make attribution
unavailable. Here all 48 first/final witnesses are valid. Review-off has no review boundary,
so its settled proposal is also its first proposal for this comparison. No
incomplete or unavailable cell was replaced. Two preliminary configuration rejections occurred
before any provider request; their private logs remain retained and they consumed
zero provider calls, rather than being silently omitted billed attempts.

## Manual findings outside the fixed oracle

None of the findings below changed the associated frozen hidden-test outcome. Most had
passing first and final frozen checks; the final credential-variable finding
repaired an additional defect while its first and final checks still failed on
`ENCRYPTION_KEY`. These are **unscored adjudications**, not hidden-score catches or a change to
the success denominator. Candidate hashes, sandboxed before/after checks and
runtime versions are preserved in the run metadata. No candidate code
was executed outside the sandbox for these adjudications.

| Case / repeat / finding | Classification | Manual false→true predicates | Evidence summary |
| --- | --- | ---: | --- |
| Heading links / repeat 1 / finding 1 | Clear additional defect | 3 | three external/different-page fragment markers fail before review and pass after repair; same-page marker removal remains correct |
| Runtime path aliases / repeat 1 / finding 1 | Clear additional defect | 3 | compatibility normalization conflates valid filename characters with path separators |
| Heading links / repeat 2 / finding 1 | Clear additional defect | 3 | removing marker-only links discards separating whitespace between words |
| Runtime path aliases / repeat 2 / finding 1 | Clear additional defect | 2 | registrations are lost across symlinked spellings of the same root |
| Heading links / repeat 3 / finding 1 | Clear additional defect | 2 | ordinary marker punctuation outside headings is discarded |
| Heading links / repeat 3 / finding 2 | Malformed-input robustness; scope caveat | 2 | nested anchors overwrite or prematurely attach the outer URL |
| IPv6 proxy authorities / repeat 3 / finding 1 | False positive on tested supported runtimes | 0 | alleged double-bracketing is prevented by the standard library preserving existing brackets |
| Credential variables / repeat 3 / finding 1 | Clear additional defect | 3 | URL userinfo passwords containing comma or semicolon are not screened |

Six findings repair clear additional defects, with 16 manual predicates changing
from fail to pass. One separate robustness finding repairs two predicates for
malformed nested anchors; one finding is a false positive on the tested runtimes.
Predicate counts are checks of behavior, not counts of distinct bugs.

The nested-anchor case is outside an explicit malformed-input guarantee.
The IPv6 allegation did not reproduce in actual wire construction or standard
library bracket preservation on Python 3.12.13 and 3.14.8; the before and after
both passed. These classifications cover the observed findings, not a semantic
precision estimate over developer work.

The retained credential-screening failures are real quality failures. Independent
sandboxed rechecks keep the original expected behavior and identify
`ENCRYPTION_KEY` as the first failing assertion; reference fixes pass. The
reviewer returned no finding on the first repetition and timed out on the second.
On the third it repaired comma/semicolon-separated URL userinfo handling, but
still missed the credential-name failure. All six failed cells remain quality
failures, not repaired catches or excluded measurement failures.

## Observed tokens and cache subsets

| Counter | Review off | Review on |
| --- | ---: | ---: |
| All prompt tokens, including review | 1,856,091 | 2,439,183 |
| All output tokens, including review | 62,787 | 74,307 |
| All observed tokens | 1,918,878 | 2,513,490 |
| Main prompt tokens, excluding review | 1,856,091 | 2,361,455 |
| Main output tokens, excluding review | 62,787 | 71,679 |
| Direct review prompt / output | 0/0 | 77,728/2,628 |
| Direct review share of all tokens | 0% | 3.20% |
| Cached prompt-read subset | 1,690,112 | 2,171,008 |
| Prompt cache-write subset | 0 | 0 |
| Review cached prompt-read subset | 0 | 3,072 |
| Review prompt cache-write subset | 0 | 0 |

Review-on observed tokens are 30.99%
higher in aggregate. Cache counters are subsets, not extra tokens to add to the
prompt totals. Direct review tokens exclude main-model repair turns; independent
first proposals and subsequent main work also differ between arms, so the total
difference is descriptive rather than a clean causal estimate of one reviewer
call. Provider usage for an abandoned or timed-out request may be unavailable;
these are observed counters, not an invoice-complete billing claim. Subscription
tokens do not supply an API dollar price, so no dollar-saving estimate is made.

Raw tool-call counters count transcript frames, including chunks of large
arguments. They are not used as logical tool-call costs in this report.

## Timing and suspension limits

The raw all-cell wall medians are 68.0 seconds off
and 90.2 seconds on. They retain every attempted
cell and are not described as active time. Between-cell coordination pauses are
outside task wall measurements. Two cells record a wall-minus-monotonic gap over
the runner's five-second suspension notice:

| Cell | Raw wall | Recorded gap |
| --- | ---: | ---: |
| Credential variables / repeat 2 / on | 1059.8 s | 941.6 s |
| Credential variables / repeat 2 / off | 5429.6 s | 5182.6 s |

The gaps are consistent with suspension or a clock discontinuity; the precise
cause is not established. An idle-sleep inhibitor does not prove that all system
suspension was impossible. Do not subtract these gaps to recover provider latency.
Their quality results and token usage stay in the full ledger.

For comparable timing, exclude the **whole matched pair** if either arm has a
gap above five seconds: 23/24 pairs remain. Their raw wall
medians are 67.4 seconds off and
88.1 seconds on; the median paired on-minus-off
difference is 13.1 seconds.
These descriptive timings are separate from 48/48 valid quality witnesses and
24/24 matched quality pairs. They do not imply a latency guarantee.

## Boundaries of the recommendation

Eight trimmed historical tasks and three repeats cannot establish general review
quality. The frozen oracle misses several defects found by manual checks, while
one reported finding was not a defect on the tested runtimes. One incomplete
review coincided with a suspension-affected cell. These limits prevent a broad
semantic-precision, savings or population-wide effectiveness claim.

Useful manual repairs support keeping review available. Equal predeclared hidden
passes, additional observed tokens, longer comparable wall time and the verified
false positive support keeping the default off on this evidence. Any broader
recommendation needs a new fixed corpus and scoring plan; these results are not
retuned to produce one.

The original live run used the uv-based interpreter on the frozen source. After
all 48 cells finished, a separate Framework-Python portability fix permits only
ancestor metadata for explicitly readable runtimes; sibling contents and directory
listings remain denied. That fix changes no recorded task, oracle or usage row and
requires no billed rerun. Raw provider transcripts, profile databases, candidate
bodies and operational logs are not published with these metadata artifacts.
