# B6 paired independent-review measurement — 2026-10-17

The final comparison contains 72 witnessed cells: eight planted defect kinds
and four controls that satisfy their hidden contracts, three repetitions per
review arm. Each task started from a fresh fixture. Main model was
`codex/gpt-6-sol`, task effort `xhigh`, review effort `low`, exploration off.
The first arm alternated off/on, on/off, off/on. Hidden tests ran outside the
candidate checkout with trusted pytest configuration, candidate hooks/plugins
disabled and exact executed-test JUnit witnesses. All seed witnesses passed.

This is a corrected comparison, not a single untouched live cohort: 66 initial
cells are retained, six clean-control cells were replaced by new live calls,
and six retained resource cells were re-scored offline. The raw initial 72
cells, six new cells and rescore proof are preserved alongside the
[machine-readable final record](review-corpus-b6-2026-10-17.json). No attempted call was removed
from cumulative cost.

| Measurement | Review off | Review on (low) |
| --- | ---: | ---: |
| Bug proposals passing hidden tests | 0/24 | 24/24 |
| Finding → repair → hidden pass | 0/24 | 24/24 |
| Clean controls broken by review repair | 0/12 | 0/12 |
| Clean controls with repair edits | 0/12 | 0/12 |
| Findings | 0 | 27 |
| Review timeouts / incomplete reviews | 0/0 | 0/0 |
| Comparison prompt + output tokens | 534,644 | 1,313,126 |
| Direct review token subset | 0 | 70,277 |
| Review share of comparison arm tokens | 0% | 5.35% |
| Cached input token subset | 451,456 | 1,083,392 |
| Median task wall time | 17.00 s | 43.25 s |
| Median direct review time | 0.00 s | 2.31 s |

Review on used 2.46× the comparison tokens of off.
The direct reviewer subset omits the main agent’s extra repair/check work.
The off arm retains prescribed defects by construction; zero passes does not
measure a natural-work main-agent defect rate. Finding counts alone do not
establish semantic precision. Hidden passes prove only these synthetic
contracts. Other offline suites ran on the same host, so the wall times are
observations under shared load.

## Audit corrections

The initial resource oracle’s file double lacked the context-manager protocol.
It rejected all three idiomatic `with path.open()` repairs even though those
repairs close real files correctly. The corrected double supports that protocol
and checks cleanup both on successful reading and on a read exception.
All six exact final resource proposals were reconstructed from ordered
successful mutations and checked byte-for-byte against published request hashes
and immutable candidate artifacts, then tested offline with the corrected
oracle. Review-on resource results changed from 0/3 to 3/3; off stayed 0/3.
Original outcomes remain in each row and the full rescore proof. This used
zero model requests or provider tokens.

The original clean-inverted control reordered short-circuit evaluation: an
invalid age with `blocked=True` could return `False` rather than raise.
All six old cells were excluded from comparison quality, then replaced by
six fresh cells using an evaluation-order-preserving parenthesization.
Their old results and costs remain in the initial record and exclusion ledger.
The other controls use ordinary Python iterable/list/value contracts; none of
these oracle scores claims equivalence for every possible monkeypatch or input.

## Provenance and budget

Initial runner: `232221a037a6a25481d82f2762d49518a4c3c648`.
Corrected control/oracle: `cce2565eb4e4d74e8bacb05ad14eef31c6e77cf8`.
Product source is unchanged between them and matches merged Wave 3
`0a0c9f7`; each final row records its live source. This package changes no
review defaults. Source, task usage, cache subsets, per-case medians, mutations
and candidate hashes are recorded in the JSON artifacts.

The 72 comparison cells account for 1,847,770 observed tokens.
Excluded setup/provider attempts and superseded control cells account for
469,393 more: **2,317,163/3,000,000** overall.
A complete medium-effort comparison was not run: even a repeat of the measured
low-effort on arm exceeds the remaining budget. No dollar cost is implied.

**Recommendation: retain review off by default**, with explicit opt-in when
a user wants the extra check. This small seeded corpus shows useful repairs
and added cost; effectiveness on unseeded work and a wider clean distribution
remain unmeasured. Changing the default is a separate user decision.

Offline validation before the final oracle/control audit: 2,571 tests passed
with one case-sensitive-filesystem skip; Ruff, mypy (110 source files), wheel
and source builds passed. All 50 focused benchmark tests passed after the
corrections. Independent review verified exact seed attribution, collection-hook
isolation, durable usage, and published-byte resource rescoring. See
[the protocol](review-corpus-v1.md).

Raw inputs:

- [review-corpus-b6-2026-10-17-initial-matrix.json](review-corpus-b6-2026-10-17-initial-matrix.json) (SHA-256 `4c63a7b4f7f1fbb0450d0b463dcc45ce1813ab267156b31705df61443f5a5c7e`).
- [review-corpus-b6-2026-10-17-control-replacement.json](review-corpus-b6-2026-10-17-control-replacement.json) (SHA-256 `102b5f03d8dbaa6f19b44995aee4dc420425561301e443d8e4396e8ebc48abd1`).
- [review-corpus-b6-2026-10-17-resource-rescore.json](review-corpus-b6-2026-10-17-resource-rescore.json) (SHA-256 `655c97f11db934e8127d19b1bfb0e6b5066b9211026789b91cbb8b0226d11fbc`).
