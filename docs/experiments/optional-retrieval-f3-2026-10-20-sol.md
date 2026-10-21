# F3 optional retrieval: bounded Sol attempt

The comparison is **incomplete and inconclusive**. The corrected run recorded
one failed off-arm cell out of 30 planned cells, leaving 29 missing cells and
**zero valid matched pairs**. There is no evidence here for a retrieval benefit
or regression. Keep indexing off by default and leave F4 gated.

## Protocol and outcome

The authorized follow-up used Codex `gpt-6-sol` at `high`, with exploration
helpers at `low`, review off and a shared cap of **3,000,000 additional tokens**.
The planned matrix was five tasks, three repetitions and index off/on, with arm
order alternating by repetition. Suite 5 and corpus commit
`6e5d65d148d6b4ac92fc849cb4fd861c63f8b5c3` were unchanged.

The first experimental budget guard incorrectly rejected concurrent helpers
when reservations were temporarily occupied. That invalid instrumentation
attempt recorded three failed cells and consumed 832,827 observed tokens.
Those cells are excluded from retrieval/model scoring; their usage remains
charged to the same cap. The corrected guard queues temporary contention.

The corrected attempt stopped during its first off-arm cell after a transport
attempt failed without a usage receipt. Its failure is not a completed answer
or a retrieval-quality score. That request retains its full conservative
reservation. Another worst-case request cannot fit in the remaining budget,
so no further requests or paid retries were made.

## Token accounting

| Item | Tokens |
|---|---:|
| Invalid instrumentation attempt: observed | 832,827 |
| Corrected attempt: observed | 440,609 |
| **All additional observed usage** | **1,273,436** |
| One unknown request: conservative reservation | 1,178,000 |
| **Additional observed usage plus reservation** | **2,451,436** |
| Authorized additional cap | 3,000,000 |
| Headroom after retaining the unknown reservation | 548,564 |
| Retained Astra pilot: observed, separate cohort | 1,164,063 |
| Cumulative observed usage including pilot | 2,437,499 |

The unknown reservation is **not measured usage**. Including it with both
cohorts yields a conservative total of 3,615,499 tokens. Cached input and helper
usage are already included in observed prompt/output totals; subset counters
are not added again. No dollar cost is inferred from subscription usage.

The independent request ledger contains 129 transport attempts: 128 settled
and one unknown (`create_failed`). The corrected failed task's durable
checkpoint reports only 2,195 tokens, omitting 438,414 known helper/request
tokens. The request ledger accounts for those tokens and is authoritative for
the cap. The small checkpoint total must not be used as the cost of this cell.

## Budget guard and interpretation limits

The disposable runtime used request-level admission in addition to the shipped
runner's cell-level admission flag. Each outbound request reserved 1,178,000
tokens before dispatch, using the sum of the published 1,050,000-token context
window and 128,000-token maximum output as a conservative bound. The reservation
was settled only after validated usage; unknown usage retained the full bound.
See the [model reference](https://developers.openai.com/api/docs/models/gpt-6-sol)
and [context accounting guide](https://developers.openai.com/api/docs/guides/conversation-state).
This experiment did not add a product billing-limit option.

The corrected guard added queue delays: 168.931 seconds summed across requests,
with a maximum wait of 13.439 seconds. These waits overlap across helpers and
must not be added to the 105.4-second task wall time. They are instrumentation
overhead and cannot establish ordinary product/provider latency. No paired
latency, token, tool-use or answer-quality improvement can be calculated.

The Astra/xhigh pilot is retained separately and contributes only to cumulative
accounting. It is not a control for the Sol/high cohort. The invalid attempt's
cells remain visible in the [machine-readable record](optional-retrieval-f3-2026-10-20-sol.json).

## Provenance and offline checks

The runtime was freshly built from F3 source revision
`6c69ac3fa9336f4aa9259f2bf2dba86c1f438ada`. All 123 installed source modules
matched the frozen checkout, and the runner and corpus hashes were rechecked.
The wheel SHA-256 is
`9b6a427f6681d11984973ef1a70ace93adf0c37d9bf9706d150e4038e7ddccaa`.
Source/test/runner files were unchanged throughout the measurement and reporting.
The corrected guard passed 45 offline fake-provider tests and independent
review. The reporting script passed ten offline fixture cases. Actual daemon
startup proved the guard was active before tasks began. Neither check generated
provider requests. Raw logs, request identities and the original persistent
budget ledger are retained privately; none is published in this record.
