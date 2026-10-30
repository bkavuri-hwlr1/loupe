# Parallel work plan

Date: 2026-10-13, extended 2026-10-16, 2026-10-22, 2026-10-24 and 2026-10-30. Waves 1–3 started from
`main` after #38, #39 and #41 and are merged (through #56). Waves 4–7 started
from `main` at #56 and are merged (through #72). Waves 8–11 started from `main`
at #72 and shipped in 0.1.0a3 (through #85). Wave 12 starts from `main` once
this plan merges (#86).

The remaining work, split into packages that separate sessions can run at the
same time. Each package is sized for one session, names the files it may
change, and says what "done" means. Packages in one wave touch disjoint
files, so they can run together without merge conflicts. The long-range
designs remain in
[`multi-session-workspace-modes-implementation-plan.md`](multi-session-workspace-modes-implementation-plan.md),
[`cli-harness-response-plan.md`](cli-harness-response-plan.md), and
[`llm-cli-implementation-plan.md`](llm-cli-implementation-plan.md); this
plan picks the next executable slices from them and from the live
experiments in [`implementation-status.md`](implementation-status.md).

## How to run packages in parallel

- **One worktree per package.** Never share the main checkout between
  sessions:

  ```console
  git fetch origin main
  git worktree add -b <branch> ../loupe-<id> origin/main
  ```

- **Stay inside the package's files.** A package may change the files it
  lists, add new modules and tests, and add its own lines to the docs. If it
  needs a file another running package owns, stop and coordinate rather than
  editing it.
- **Hot files.** `src/llm_cli/execution/shared.py`,
  `src/llm_cli/agent/harness.py`, `src/llm_cli/agent/shared_tools.py`,
  `src/llm_cli/daemon/service.py`, and `src/llm_cli/cli/app.py` are where
  packages most often collide. The table says which packages touch them; run
  at most one package per hot file at a time.
- **Docs.** Each package updates `docs/implementation-status.md` (and the
  relevant plan) in its last commit. These edits often overlap; resolve them
  at merge time, they are text only.
- **Definition of done for every package:** `uv run --all-extras pytest`,
  `uv run ruff check .`, and `uv run --all-extras mypy` pass; new behavior has
  tests that fail without the change; docs describe the behavior; one PR per
  package.
- **Billed work.** Packages marked *live* make real model requests through
  a signed-in profile other than `default` (for example `explore-eval`). They
  never run in CI.
- **Stacked work.** When a package depends on another that is not merged yet,
  branch from that package's branch, and retarget the PR to `main` as soon as
  the base merges, before its branch is deleted.

## At a glance: Wave 12

Wave 12 takes up everything left open after 0.1.0a3: the former Later list
and two known limits of the outside catch-up fix (#85).

| ID | Package | Lane | Size | Depends on | Hot files | Wave |
| --- | --- | --- | --- | --- | --- | --- |
| C17 | Isolated catch-up follow-ups | Workspace | M | — | — | 12 |
| C18 | Larger isolated admission | Workspace | L | — | — | 12 |
| C19 | Outside-scan coverage beyond the read window | Workspace | M | — | — | 12 |
| C20 | Installer cache for dependency rebuilds | Workspace | M | — | — | 12 |
| C21 | `loupe session switch` | Workspace | S | — | app.py | 12 |
| C22 | Patch and bundle exports of candidates and batches | Workspace | M | — | app.py, service.py (one handler) | 12 (after C21) |
| B7 | Review on natural tasks (*live*) | Harness | M | — | — | 12 |
| F5 | Offline comparison for semantic retrieval | Knowledge | M | — | — | 12 |
| F4 | Embeddings with exact scoring | Knowledge | L | F5 recommends it | app.py (`search`) | after F5 |
| E2 | Live provider checks: Anthropic cache, oversized prompts (*live*) | Reliability | S | your sign-in | — | 12 |

**Wave 12, all at once:** C17, C18, C19, C20, B7, F5, E2, and one session
running C21 → C22 in order (they share `cli/app.py`). F4 runs only if F5
recommends it.

**Shared files.** These overlaps are small; resolve them at merge time.

- C18 and C20 both add settings to `src/llm_cli/config/models.py`.
- C18 and C19 both extend `scripts/scan_scale.py`: C18 the isolated sizes,
  C19 the outside-scan read set.
- C22 adds control migration 34. Any other package that needs a migration
  takes the next free number when it merges.

**Live work.** B7 and E2 (and F5, if it uses a hosted embedding model) make
billed requests through a profile you sign in yourself before starting.
Agents never handle keys. Set each run's token cap explicitly.

**Starting a package.** Create its worktree as described above, for example
`git worktree add -b codex/wave12-c17 ../loupe-c17 origin/main`, and give the
session: "Implement package C17 from `docs/parallel-work-plan.md` (Wave 12),
following 'How to run packages in parallel'. Open one PR."

## At a glance: Waves 8–11 (shipped in 0.1.0a3)

| ID | Package | Lane | Size | Depends on | Hot files | Wave |
| --- | --- | --- | --- | --- | --- | --- |
| D4 | Release 0.1.0a3 | Operations | S | — | — | 8 |
| E3 | Long-running soak and scan-cost measurement | Reliability | M | — | — | 8 |
| C11 | Git index safety and Git-operation reconciliation | Workspace | L | — | — | 8 |
| G4 | Authority maintenance and stuck-cutover recovery | Authority | M | — | app.py, service.py (one handler) | 8 |
| C13 | Switch a conversation between shared and isolated mode | Workspace | L | — | service.py, shared.py | 9 |
| C15 | Export runtime files from an isolated session | Workspace | S | — | app.py, service.py (one handler) | 9 |
| C16 | Bound isolated synchronization-journal storage | Workspace | M | — | — | 9 |
| C14 | Managed background processes | Workspace | L | C13 | service.py, shared.py, shared_tools.py, app.py | 10 |
| C12 | Native file-change watchers | Workspace | M | C11, E3's measurement | service.py | 11 (E3: needed) |

**Wave 8, all at once:** D4, E3, C11, G4. Only G4 touches hot files.
**Wave 9 implementation complete:** C13, C15 and C16 are integrated with
migration 31 for compact synchronization evidence and migration 32 for durable
workspace switches. C15's self-contained export handler retains the physical and
runtime activity barriers used by switching and cleanup. The C16 full growing-path
hour passed with the original bounds. C13's final combined source is frozen for
full covered correctness and separate production scale gates; see the package
verification links below. **Wave 10:** C14 is implemented. **Wave 11:** C12 is
implemented; native watchers bring the 20,000-file safe point to 144 ms, under
E3's threshold.

D4 prepared 0.1.0a3 from Waves 1–7. The tag waited until Waves 8–11 and the
outside catch-up fix merged, so 0.1.0a3 shipped Waves 1–11 on October 29,
2026. The work in [Later](#later) stays out of these waves.

### What E3 changed for Waves 9–11 (October 22, 2026)

E3's [report](experiments/soak-and-scan-e3-2026-10-22.md) changes two things:

- **C12 goes ahead.** A real isolated safe point takes 1.94 seconds at 20,000
  source files, above the 250 ms gate.
- **C16 joins Wave 9.** The growing-path soak exceeded its 128 MiB database
  bound after 52 minutes, because every isolated sync keeps a whole manifest.

## At a glance: Waves 4–7 (merged)

| ID | Package | Lane | Size | Depends on | Hot files | Wave |
| --- | --- | --- | --- | --- | --- | --- |
| B6 | Review quality and cost measurement (*live*) | Harness | M | — | — | 4 |
| C6 | Live isolated-mode smoke test (*live*) | Workspace | S | — | — | 4 |
| C7 | Faster isolated checks and Wave 3 leftovers | Workspace | M | — | — | 4 |
| C8 | Runtime refresh for isolated sessions | Workspace | M | C7 | — | 5 |
| C9 | Per-session command environment | Workspace | L | C7 | service.py, app.py | 6 |
| C10 | Dependency rebuild adapters | Workspace | M | C8 | — | 6 |
| F1 | Knowledge store and lexical index | Knowledge | L | — | service.py, app.py | 4 |
| F2 | Deterministic code graph | Knowledge | M | F1 | app.py | 5 |
| F3 | Optional retrieval in the agent, with a live comparison (*live*) | Knowledge | M | F2 | shared_tools.py | 6 |
| F4 | Embeddings with exact scoring | Knowledge | M | F3's result | app.py | 7 (after G3) |
| G1 | Multi-profile authority: design and ADR | Authority | M | — | — | 4 |
| G2 | One publisher per physical checkout | Authority | L | G1 | service.py | 5 |
| G3 | Legacy claim cutover | Authority | L | G2 | service.py, app.py | 7 |

**Wave 4, all at once:** B6, C6, C7, F1, G1. Only F1 touches hot files.
**Wave 5:** C8, F2, G2. **Wave 6:** C9, C10, F3. C9 and C10 each add one
setting in `config/`, so keep those edits small and resolve them at merge.
**Wave 7:** G3, then F4 only if F3's measurement supports it (they share
`cli/app.py`).

Waves 5–7 are provisional. G1 rewrites G2 and G3 with their final scope, and
C6's findings may add to C7 or C8. Revisit the waves once those land. E2 is
on hold. The work in [Later](#later) is deliberately left out of these waves.

## At a glance: Waves 1–3 (merged)

| ID | Package | Lane | Size | Depends on | Hot files | Wave |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | `/apply` merges held proposals onto the current checkout | Multi-session | M | #41 | — | 1 |
| A2 | Parallel-friendly experiment with repetitions (*live*) | Multi-session | S | #41 | — | 1 |
| B1 | Independent review pass before publication | Harness | L | — | shared.py, harness.py | 2 |
| B2 | Exploration helpers can run commands and fetch pages | Harness | M | — | shared_tools.py | 2 |
| B3 | MCP over HTTP, resources and prompts, plan mode | Harness | M | — | — | 1 |
| B4 | Web fetch through an HTTP proxy | Harness | S | — | — | 1 |
| B5 | Reusable regex search worker | Reliability | M | — | — | 1 |
| C1 | Isolated mode: design and hydration planner | Workspace | M | — | — | 2 |
| C2 | Isolated mode: sessions in their own worktree | Workspace | L | C1 | shared.py, service.py | 3 |
| C3 | Isolated mode: publish and receive batches | Workspace | L | C2 | shared.py | 3 |
| C4 | Track edits made outside Loupe while sessions work | Workspace | L | #41 | service.py | 2 |
| C5 | Symlink and binary edits | Workspace | L | #41 | shared_tools.py | 3 |
| D1 | `loupe db backup` | Operations | S | — | app.py | 1 |
| D2 | Retention: prune old task output | Operations | M | D1 | app.py | 1 (after D1) |
| D3 | `loupe config show` and `set` | Operations | S | — | app.py | 1 (after D2) |
| E1 | Stress test survives stalled CI runners | Reliability | S | — | — | 1 |
| E2 | Live provider checks: Anthropic cache, oversized prompts (*live*) | Reliability | S | — | — | on hold |

**First wave, all at once:** A1, A2, B3, B4, B5, E1, and one session
running D1 → D2 → D3 in order (they share `cli/app.py`). **Second wave:** B1,
B2, C1, C4 (B1 and C4 must not run at the same time as anything else that
touches `shared.py` or `service.py`). **Third wave:** C2 → C3, C5.

## Lane A — multi-session follow-through

### A1. `/apply` merges held proposals onto the current checkout

**Why.** New chats default to normal mode, which holds every proposal for
review. With two sessions, applying the first proposal makes the second one
stale, and `loupe task apply` refuses it: "Conflict at PATH; no files
changed" when a file it edits changed, or, when checks are configured,
"Required checks are missing, failed, or stale" after any change at all. #41
fixed this for auto mode only.

**Scope.**

- In `Workflow.apply` (not `undo`), when a proposal's file has changed since
  its recorded base, merge it with `workspace/merge.py` from the file's
  recorded original onto the current bytes, as #41 does inside a task.
- Clean merge: rerun the required checks on the merged proposal, then apply.
  Factor the `CheckRunner` construction in `execution/shared.py` into a
  helper both paths use, or build one in the workflow; keep the change to
  `shared.py` to that helper.
- Conflict, or a change that cannot be merged: refuse as today, naming the
  conflicting paths, and suggest asking the session to make the change again.
  Never apply conflict markers.
- `--allow-unverified` keeps its meaning (skip checks, never skip conflicts).

**Files.** `src/llm_cli/workspace/workflow.py`, a small helper in
`src/llm_cli/execution/checks.py` or `execution/shared.py`, CLI messages in
`src/llm_cli/cli/` for apply, `tests/integration/test_verified_workflow.py`
or a new test file, `docs/verified-workflow.md`.

**Done when.** Two normal-mode sessions edit overlapping files in different
places: both `/apply`s succeed, the checkout holds both changes, and checks ran
on the merged result. Overlapping lines: the second apply is refused with the
paths, and the checkout is unchanged. A stale-but-unconflicted proposal whose
merged checks fail is refused with the failure.

### A2. Parallel-friendly experiment with repetitions (*live*)

**Why.** The shop experiment's two parts edit the same three files, so two
sessions were slightly slower than one agent even after #41. Whether parallel
sessions pay off on work that overlaps less is still unmeasured, and single
runs are noisy.

**Scope.** Add a second experiment to `scripts/live_sessions.toml` whose parts
touch mostly different files (for example a fixture with two independent
modules and one shared registry line), with hidden acceptance tests. Run both
experiments with `--repeat 3` and record medians in `implementation-status.md`.

**Files.** `scripts/live_sessions.toml`, `scripts/benchmark_fixtures/<new>/`,
`scripts/benchmark_acceptance/<new>/`, `tests/unit/test_live_sessions.py`
(the shipped-experiment test covers it), `docs/implementation-status.md`.

**Done when.** The new experiment validates in unit tests, its hidden tests
fail on the untouched fixture and pass on a reference solution, and the doc
reports medians for both experiments and arms.

## Lane B — harness gaps

### B1. Independent review pass before publication

**Why.** Planned since the original design (`llm-cli-implementation-plan.md`
§17.2, step 4): a fresh session inspects the exact diff and check evidence
before publication. It is the remaining built-in execution pass; planning and
bounded repair exist.

**Scope.**

- New `agent/reviewer.py`: a read-only helper session, like `explore`, given
  the task, the exact pending diff, and check results, returning findings
  with file and line references, or an explicit no-findings result.
- Run it when an edit task finishes with passing checks, before settlement.
  Findings return to the agent once as a tool result for a bounded repair;
  a second review does not loop.
- Setting `agent.review = "off" | "on"`, default `off` until measured, with
  `agent.review_effort` like `explore_effort`. Count its tokens in the task's
  usage, as helpers are.
- Show the review in the conversation (label, finding count).

**Files.** `src/llm_cli/agent/reviewer.py` (new),
`src/llm_cli/agent/harness.py`, wiring in `src/llm_cli/execution/shared.py`,
`src/llm_cli/config/`, `src/llm_cli/cli/render.py`, tests, a benchmark task
in `scripts/live_benchmark.toml` (a planted bug the review should catch).

**Done when.** Scripted-provider tests cover findings → repair → publish,
no findings → publish, reviewer failure → publish with a notice, and the
setting off → no review. A live benchmark run shows the planted-bug task
caught.

**Implementation status (2026-10-14).** The opt-in pass, one durable repair
handback, read-only helper, usage accounting and review notices are implemented.
Scripted tests cover publication, failures, cancellation and restart; the
`independent-review-planted-bug` live task requires an observed successful
review with findings rather than trusting the final answer's claims. Its live
acceptance run passed: exactly one review found the planted pagination bug,
the original agent repaired it, and the hidden test passed. See the sanitized
[measurement record](experiments/wave2-independent-review-2026-10-14.json).
The initial multiline-prompt run was excluded; the single-prompt regression
is now covered. Timing is not comparative because full suites ran concurrently.
Frozen product source passed all 2,210 all-extras tests, Ruff and mypy;
subsequent prompt/test-only follow-ups passed their targeted regressions.

Review accepts a standalone JSON code fence and retains valid, anchored findings
when other findings are malformed. Optional review has a 120-second total cap
and reserves 10% of the task budget for completion (minimum one second, maximum
30 seconds); repair still uses the original task deadline. Reviewer searches
and source-exclusion Git calls share this cap, including exclusion setup lock
waits. A timed-out review
produces a visible incomplete-review notice and permits normal checked
settlement. Synchronous transports without cancellation can leave at most one
private daemon request running per durable review attempt; its late response
cannot execute tools or alter task state, and its unavailable usage is not
counted. Streaming adapters stop at the next event after abandonment.

### B2. Exploration helpers can run commands and fetch pages

**Why.** Remaining gaps from the harness plan items 9 and 10: helpers cannot
run sandboxed commands or fetch web pages, so a question like "what does this
test print?" needs the main conversation.

**Scope.** Offer `run_command` (same sandbox, approval, and output screening;
never writes) and `web_fetch` (same domain policy and approvals, which ask the
user through the parent task) to helpers when the parent task has them.
Count their calls against the helper's reserved budget.

**Files.** `src/llm_cli/agent/explorer.py`, `read_only_view` in
`src/llm_cli/agent/shared_tools.py` and `src/llm_cli/agent/tools.py`,
`tests/integration/test_shared_explore.py`.

**Done when.** A helper can run a command and fetch an allowed page; a denied
domain or command approval is refused to the helper exactly as to the parent;
plan-mode helpers still cannot run commands.

**Implemented.** Helper views expose only source reads plus the parent's
offered command/fetch handlers, preserving its sandbox, approvals, hooks,
cache, screening, cancellation, and deadline. Calls use the helper reservation
without double charging, and helper observations never authorize parent writes.
Helper-triggered reconciliation is delivered durably to the parent before its
next action. Command/domain approval questions identify the helper; coalesced
fetch waiters follow task cancellation/deadlines while domain approval is pending.

### B3. MCP over HTTP, resources and prompts, and plan mode

**Why.** Remaining gaps from harness plan item 7: only stdio servers and tools
are supported, and read-only MCP tools are not offered in plan mode.

**Scope.** Streamable HTTP transport (with configured headers taken from
environment variables, never stored in config), MCP resources (list and read,
bounded and screened like tool results) and prompts (list and get), and a
server-level `read_only = true` flag that offers its tools in plan mode.

**Files.** `src/llm_cli/mcp/` (client, tools, a new transport module),
MCP settings in `src/llm_cli/config/`, `tests/integration/test_shared_mcp.py`,
`tests/unit/test_mcp.py`, harness plan item 7.

**Done when.** A local HTTP test server's tools, resources, and prompts work
end to end; a `read_only` server is offered in plan mode and others are not;
secrets in headers never appear in events or logs.

### B4. Web fetch through an HTTP proxy

**Why.** Remaining gap from harness plan item 10: `web_fetch` refuses proxies,
which blocks networks that require one.

**Scope.** Honor `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` (or an explicit
`agent.web_proxy`) via CONNECT tunneling, keeping every existing check: the
target's resolved addresses are still checked as public, redirects are still
approved per domain, and the proxy itself must be configured, never learned
from a page.

**Files.** `src/llm_cli/web/fetch.py`, `src/llm_cli/web/access.py`, web
settings in `src/llm_cli/config/`, `tests/unit/test_web_fetch.py`.

**Done when.** Tests with a local CONNECT proxy fetch an allowed page, refuse a
private target even through the proxy, and honor `NO_PROXY`.

### B5. Reusable regex search worker

**Why.** Each `search_text` starts a fresh `python -I -S` process so a runaway
regular expression can be killed. On stalled macOS CI runners those launches
hung for minutes (the read differential test had to match in-process). A
user's searches would hit their 5-second limit the same way.

**Scope.** Keep matching out of process, but reuse one worker per broker (or
a small pool shared by batched reads), sending each search's files and
pattern over its pipe; kill and replace the worker when a search times out.
Measure the effect with the existing read microbenchmark.

**Files.** `src/llm_cli/agent/bounded_search.py`, its callers in
`src/llm_cli/agent/tools.py` only if the interface changes,
`tests/unit/test_bounded_search.py`, `tests/integration/test_parallel_reads.py`.

**Done when.** Results match the per-search worker exactly (the differential
test can match through the real worker again), a catastrophic pattern is still
stopped within its budget and the next search works, and search latency drops.

### B6. Review quality and cost measurement (*live*)

**Why.** B1's independent review is off by default because it has been
measured on only one planted bug. How often it catches real bugs, how often
its findings send the agent into a harmful repair, and what it costs are all
unknown. Deciding the default needs those numbers.

**Scope.**

- A review corpus in `scripts/live_benchmark.toml`, seeded like
  `independent-review-planted-bug`: at least eight planted-bug tasks of
  different kinds, each passing its visible smoke checks. Kinds: boundary
  (off-by-one), inverted condition, missing error path, state not reset, wrong
  default, resource leak, unsafe path handling, ordering. Add at least four
  clean tasks whose correct change gives the review nothing real to find.
  Each task gets hidden acceptance tests.
- A paired comparison in `scripts/live_benchmark.py`. The script has
  `--set`, `--budget-tokens` and `--compare OLD NEW` today; add `--repeat N`,
  as `live_sessions.py` has. Run every corpus task with `--set review=off` and
  `--set review=on`, three times each, alternating which arm goes first.
  Extend `--compare` to summarize per arm:
  - hidden-test pass rate on the bug tasks (bugs caught and fixed);
  - clean tasks that a review-driven repair broke (harmful false positives);
  - findings per task and review timeouts;
  - tokens, the review's share of them, and wall time.
- If the token budget allows, repeat the `on` arm at review effort `low` and
  `medium`.
- Record the run in `docs/experiments/`, and recommend a default in
  `implementation-status.md` with the numbers behind it. Changing the default
  is a separate decision for the user, not part of this package.

**Files.** `scripts/live_benchmark.toml`, `scripts/live_benchmark.py`,
`scripts/benchmark_fixtures/review-*/`, `scripts/benchmark_acceptance/review-*/`,
`tests/unit/test_review_benchmark.py`, `docs/implementation-status.md`,
`docs/experiments/`.

**Done when.**
- Unit tests show each bug task's hidden tests fail on the seeded change and
  pass on a reference fix.
- Each clean task's hidden tests pass as seeded.
- The recorded run reports, for each arm, the catch rate, the harmful false
  positives and the cost.
- The run stays within its token cap. At the size of the planted-bug run
  (about 33,000 tokens per task), the base comparison of at least twelve tasks,
  two arms and three repeats takes roughly 2.5 million tokens. That is close to
  the default `--budget-tokens` of 3 million, so set the cap explicitly.

**Implemented and measured (October 17, 2026).** Twelve corpus tasks, exact seed
witnesses, trusted hidden tests, paired alternating arms and three repetitions
produced 72 final comparison cells (66 retained plus six new controls), with
six saved resource proposals re-scored against a corrected file double. Review on passed 24/24 hidden bug cases
versus 0/24 off, with 0/12 harmful clean repairs. All attempts together
used 2,317,163/3,000,000 observed tokens; medium effort did not fit the
remaining budget. The [experiment](experiments/review-corpus-b6-2026-10-17.md)
records per-case results, latency, usage and exclusions. Review remains off by
default, with a recommendation to retain that setting.

### B7. Review on natural tasks (*live*)

**Why.** B6 measured the independent review only on planted bugs: on caught
24 of 24, off caught none, and no clean task was harmed. Planted bugs are
smaller and more uniform than real ones, so review stays off by default until
it is measured on natural work.

**Scope.**

- **Corpus.** At least eight natural tasks taken from real bug fixes and
  features in this repository's history or in small public projects:
  - each task starts at the fix's parent commit, with the problem described
    as a user would describe it;
  - its hidden acceptance tests are the tests the fix added, or later
    regression tests for it;
  - keep each fixture small: a trimmed snapshot of the affected code, not a
    whole repository;
  - include tasks where an unreviewed first attempt is likely to miss an
    edge case, and at least two where the obvious change is correct.
- **Comparison.** Reuse B6's paired harness: `--set review=off` and
  `--set review=on`, three repeats each, alternating which arm goes first.
- **Measures, per arm:**
  - hidden-test pass rate;
  - real defects the review found that a repair then fixed;
  - review-driven repairs that broke a correct change;
  - findings per task and review timeouts;
  - tokens, the review's share of them, and wall time.
- **Recommendation.** Record in `implementation-status.md` whether to change
  the default, with the numbers. Changing it is the user's decision, not part
  of this package.

**Files.** `scripts/live_benchmark.toml`, `scripts/live_benchmark.py` (only if
the harness needs it), `scripts/benchmark_fixtures/natural-*/`,
`scripts/benchmark_acceptance/natural-*/`, `tests/unit/test_review_benchmark.py`,
`docs/implementation-status.md`, `docs/experiments/`.

**Done when.**
- Unit tests show each task's hidden tests fail at its starting point and pass
  with the original fix.
- The recorded run reports each arm's pass rate, real catches, harmful repairs
  and cost.
- The run stays within an explicit `--budget-tokens`. At B6's size (about
  33,000 tokens per task), eight tasks, two arms and three repeats take about
  1.6 million tokens; natural tasks will likely use more.

## Lane C — workspace modes

### C1. Isolated mode: design and hydration planner

**Why.** `--workspace isolated` is refused (`daemon/service.py`) because
ignored runtime files such as `.env` and `.venv` cannot be brought into a
private worktree. The full design (§18–§19 of the workspace plan) is large;
this package decides the minimum viable version and builds its first piece.

**Scope.** An ADR choosing the minimum: what is hydrated (copy-on-write clone
where available, otherwise copy, within budgets), what is refused (secrets the
user has not approved, special files, escaping symlinks), and how changes
publish (through the existing batch protocol). Then a pure planner,
`workspace/hydration.py`, that inventories a checkout's ignored files and
returns a plan with sizes, decisions, and reasons, plus a
`loupe workspace hydrate --plan` preview.

**Files.** `docs/adr/0009-*.md` (new), `src/llm_cli/workspace/hydration.py`
(new), a preview command in `src/llm_cli/cli/app.py` (coordinate with D1–D3),
tests.

**Done when.** The ADR is accepted, and the planner handles `.env`, `.venv`
with absolute paths, `node_modules`, nested repositories, escaping symlinks,
and FIFOs with explicit decisions; nothing is copied yet.

**Implemented (Wave 2 C1):** [ADR 0009](adr/0009-isolated-runtime-hydration.md)
accepts the conservative preview minimum. `workspace/hydration.py` inventories
actual ignored entries with descriptor-relative no-follow traversal, NUL-safe
Git classification, metadata/time/depth/entry bounds, per-file and aggregate
copy budgets, and stable lexical decisions. The daemon-free
`loupe workspace hydrate [PATH] --plan` command returns human/JSON metadata;
`--approve-secret PATH` previews exact-file consent only. `.venv` requires a
portability adapter/rebuild without inspecting its bytes; contained symlink
recreation is deferred along with escaping/absolute links. Refused directories
explicitly mark descendants unexamined. Incomplete or changed inventories
cannot propose copies. COW probing, pinned source/ignore-rule materialization,
actual isolated workspaces and publication were left to C2/C3 below.

### C2. Isolated mode: sessions in their own worktree

**Depends on C1.** Open an isolated session on a managed linked worktree
built from the checkout's exact current source plus the hydration plan; point
reads, edits, checks, and commands at it; recover or clean it up after a
crash. Publication still refuses with a clear message until C3.

**Files.** `src/llm_cli/daemon/service.py` (remove the refusal),
`src/llm_cli/execution/shared.py`, `src/llm_cli/git/worktrees.py`,
`src/llm_cli/workspace/`, tests.

**Done when.** An isolated session can run its tests with the copied `.env`
while the main checkout is untouched, and a daemon crash at each creation step
recovers or cleans up without touching user paths.

**Implemented (Wave 3 C2):** Isolated conversations create private linked
worktrees from current working bytes without changing the primary index or HEAD.
Source policy and exact manifests are pinned across creation, with bounded retries
when a save crosses a snapshot. Screened ordinary source fixtures copy privately
without entering model tools or artifacts. Ignored runtime receives verified
private copies or COW clones, with exact-file secret consent. Directory-level
adapters support private dependency trees without exhausting the preview's
per-entry inventory budget; supported virtualenv launchers and Node workspace
links relocate into private snapshots. No installer runs automatically.
Hydrated paths stay excluded from source tools, artifacts, and proposals even
after ignore-rule changes. Commands and checks retain useful diagnostics while
filtering actual copied private values before delivery or storage. Closing
retires an idle workspace and Git registration; held proposals keep their runtime
until applied or discarded. Recovery archives preserve ordinary source evidence
and late writes while disposing of owned runtime and screened-source copies.
Creation, privacy, dependency, output, close, and restart regressions are tracked
with the current validation results on [PR #54](https://github.com/bkavuri-hwlr1/loupe/pull/54).
Publication remains the C3 handoff. See
[the isolated workspace guide](isolated-workspaces.md) for supported boundaries.

### C3. Isolated mode: publish and receive batches

**Depends on C2.** Publish an isolated session's changes to the shared
checkout through the batch publisher (merging with #41's machinery when the
checkout moved), and apply other sessions' published batches into the
isolated worktree at safe points, preserving local edits.

**Done when.** A shared and an isolated session work on the same repository;
each sees the other's published work, and neither loses edits.

**Implemented (Wave 3 C3):** Isolated sessions freeze source deltas against a
separate accepted manifest and publish through the validated shared batch
publisher. Incoming batches replay at safe points, and exact primary scans catch
outside edits. Clean text merges preserve local work; durable conflicts retain
private bytes while unrelated paths advance. Runtime paths and their aliases
are excluded before content retention. Normal-mode apply and undo check the
frozen proposal against current shared source with private runtime inputs, even
after restart, preserving later private edits. Journal recovery accepts only
recorded before/after states; confirmed publication survives a private catch-up
failure with a durable notice. The focused synchronization/publication/held-review
suite passed 27 tests; Ruff and mypy passed. See
[isolated workspaces](isolated-workspaces.md) for supported limits.

### C4. Track edits made outside Loupe while sessions work

**Implementation (Wave 2).** Durable bounded observations, periodic scans and
model-boundary refreshes now emit path-only notices for sessions that read a
changed path. Source identities are screened; ignored/secret paths remain
opaque, and own publications are deduplicated. The combined Wave 2 candidate
passed 2,316 tests with one expected filesystem skip; see the
[validation record](experiments/wave2-validation-2026-10-14.json).
Native watchers and Git-operation reconciliation remain later work.

**Why.** #41 merges edits onto files changed outside Loupe when the agent
next validates, checks, or finishes, but sessions are not told about outside
edits as they happen: the coordination context says "External editor changes
are not continuously tracked". Phase 7 of the workspace plan.

**Scope.** A cheap periodic or event-driven scan of the checkout (mtime and
size, then content identity) recording outside changes as checkout events, so
the next model request's coordination update lists them with paths only, and
never contents of ignored or secret files. Watchers are wake-ups only; the scan
is the authority.

**Files.** `src/llm_cli/workspace/` (new scan module, `context.py`),
`src/llm_cli/daemon/service.py` (scheduling), tests.

**Done when.** An outside edit to a file a session has read appears in that
session's next coordination update; a `.env` change appears without bytes or
hashes; a session that never read the file is not interrupted.

### C5. Symlink and binary edits

**Depends on #41.** The shared tools refuse symlink and binary edits (§15.9).
Add create, retarget, and delete for symlinks inside the scope (never
escaping), and binary replacement from base64 within the size limits, with
the same base checks and journaled publication. Binary files are never merged:
a changed base is a conflict.

**Files.** `src/llm_cli/agent/shared_tools.py`, `src/llm_cli/agent/tools.py`
(schemas), `src/llm_cli/workspace/batches.py`, `src/llm_cli/workspace/broker.py`,
recovery tests.

**Done when.** Each operation publishes, rolls back, and recovers after a
crash like text edits do; an escaping symlink is refused.

**Implemented.** Shared and isolated brokers now provide bounded base64 binary
replacement and contained symlink creation, retargeting, and deletion. Typed
candidates retain exact bases through checks, checkpoints, publication, undo,
and crash recovery; binary edits never merge. Exploration helpers can read the
typed parent overlay without gaining write access or authorizing parent writes.
Focused regressions cover limits, scope and target validation, stale bases,
parent-path races, recovery, and helper permissions.

### C6. Live isolated-mode smoke test (*live*)

**Why.** C2 and C3 have been tested only with scripted providers and review
probes. Shared mode had a live subscription smoke test before it was trusted;
isolated mode has not had one. A cheap live run should find real-use problems
before Waves 5–6 build on isolated mode.

**Scope.**

- Add `--workspace shared|isolated|mixed` to `scripts/live_sessions.py`, passed
  to each session's `loupe chat`. In `mixed`, the first lane is shared and the
  second isolated. Pass `--approve-secret` for the fixture's `.env`.
- Add a variant of the `toolkit` fixture with an ignored `.env` holding a
  canary value. A visible check reads it, so a passing check proves that
  runtime hydration worked.
- Run the pair arm in `isolated` and in `mixed`, in both the normal and the
  disrupted scenario (outside edit plus daemon crash). Do one run each first,
  then three repeats if the first runs pass.
- After each run, check:
  - the hidden tests pass;
  - the primary checkout's HEAD and index are unchanged;
  - the canary value appears nowhere in the control database, the daemon log
    or the session files;
  - after the sessions close, `git worktree list` shows no private worktree
    and no `.env` copy remains under Loupe's managed worktree directory.
- Record wall time and tokens beside the existing shared-mode medians.
- File defects in C7's or C8's files as additions to that package. Fix any
  others in a small follow-up PR.

**Files.** `scripts/live_sessions.py`, `scripts/live_sessions.toml`,
`scripts/benchmark_fixtures/<toolkit variant>/`,
`scripts/benchmark_acceptance/<toolkit variant>/`, `tests/unit/test_live_sessions.py`,
`docs/implementation-status.md`, `docs/experiments/`.

**Done when.** The option and fixture validate in unit tests, and the
recorded runs meet every check above or name the defects that block them.

**Implemented and measured with named defects (October 17, 2026).**
The runner supports all three workspace selections and exact consent for isolated
lanes. Required runtime checks, trusted hidden acceptance, per-case checkpoints
and post-close privacy/cleanup audits are covered by focused tests. The frozen
C6 branch passed 2,575 tests with one expected filesystem skip, Ruff, mypy and
package builds. The original 16-run matrix stopped after eleven passes and one
operator-preservation failure, with four repeats unstarted. The C3 incoming-sync
fix then passed a separate mixed disrupted pilot and three repetitions. Two
repetitions needed the existing one-follow-up recovery after a second defect:
a synchronization notice contradicted a quiet coordination flag. The later
flag fix `608dd2b` has offline validation only; no final-candidate full live matrix is
claimed. The [measurement record](experiments/wave4-isolated-smoke-2026-10-17.json)
retains both cohorts, the failed case and original tasks, all follow-ups and
all costs: 2,892,023 of 3,000,000 tokens. All fourteen runtime/privacy/cleanup
flags passed on every recorded post-initial run. Historical shared medians are
context only because source, fixture and host load differ.

### C7. Faster isolated checks and Wave 3 leftovers

**Why.** These are the items left open by the Wave 3 review.

- **Every run copies the runtime.** Each command and check copies the
  session's whole runtime, including `.venv`, into a fresh snapshot
  (`copy_runtime_files` in `execution/commands.py`, `_copy_runtime` in
  `execution/checks.py`). A check on a clone of this repository with its
  `.venv` takes about 4 s end to end.
- **Screened fixture edits never arrive.** An edit in the primary checkout to
  a screened source fixture, a tracked file the secret screener flags such as
  a test with a fake key, is deferred forever (`workspace/isolated_sync.py`).
  Its notice repeats every turn.
- **Secret consent is all or nothing.** Opening an isolated session without
  consent for every likely secret is refused outright
  (`workspace/isolated.py`). Credential files inside dependency trees are
  instead left out with a notice.

**Scope.**

- **Faster checks.** Profile a check on a clone of this repository with its
  `.venv` first, then remove the per-run cost the profile finds. Options:
  clone files instead of copying them where the filesystem supports it;
  prepare the runtime once per session runtime generation and clone from that
  copy; or expose dependency trees read-only in the sandbox. Two guarantees
  must stay: a command's changes never reach the session's runtime or the
  primary checkout, and output screening still sees every private value.
- **Screened fixture sync.** If the private copy of a screened fixture is
  unchanged, apply the incoming version privately, without its bytes entering
  model tools or artifacts. If both sides changed, keep the private copy and
  report a path-only conflict once, not on every turn.
- **Optional secrets.** Amend ADR 0009 with the decision. Recommended: open
  the session, leave out unapproved secrets, and give an exact-path notice
  that names `--approve-secret PATH`.
- Take on any C6 findings that fall in these files.

**Files.** `src/llm_cli/workspace/runtime.py`, `src/llm_cli/workspace/isolated.py`,
`src/llm_cli/workspace/isolated_sync.py`, `src/llm_cli/execution/commands.py`,
`src/llm_cli/execution/checks.py`, `src/llm_cli/execution/sandbox.py`,
`docs/adr/0009-isolated-runtime-hydration.md`, `docs/isolated-workspaces.md`,
tests.

**Done when.**
- A repeated no-op check on a clone of this repository with its `.venv`
  finishes in under 1.5 s. Record the before and after timings.
- A screened fixture edited in the primary checkout reaches an unchanged
  private copy with no repeating notice.
- When both sides changed, the private bytes are kept and there is exactly
  one notice.
- An open without secret consent succeeds with the notice, and the omitted
  file is absent from the private worktree. If the ADR keeps the refusal
  instead, it says why.
- Every existing isolated privacy and recovery test still passes.

**Implementation (2026-10-17).** C7 is implemented with fresh private writable
copies, bounded verified COW, optional-secret omission, and privately journaled
screened fixture synchronization. Repeated actual-venv checks measured
1.428/1.452/1.471 s versus 3.032/3.033/3.037 s; see
[the experiment](experiments/isolated-runtime-c7-2026-10-17.md). Opaque incoming
changes retain the existing 8 MiB changed-path bound and defer an oversized path.

### C8. Runtime refresh for isolated sessions

**Why.** Runtime files are copied once, when the session opens. If the user
edits `.env` or reinstalls dependencies in the primary checkout, the isolated
session keeps stale copies until it is reopened (workspace plan §19.6).

**Scope.**

- **Detection.** At safe points, detect changes to hydrated paths in the
  primary checkout cheaply, with stat signatures as C4's scanner does. Emit
  path-only refresh notices, with no bytes and no digests of secrets.
- **Refresh at safe points**, never while a command or check runs:
  - If the private copy is unchanged since hydration, update it from the
    stable primary file.
  - If both sides changed, keep the private copy and report a runtime
    conflict once.
  - Never overwrite a likely secret unless the session opted in when it
    opened.
  - Hydrate newly appearing entries under the normal budgets and consent.
- **Runtime generations.** Each refresh creates a new runtime generation.
  Held proposals keep the generation they were frozen with, so `/apply`
  reruns their checks on the same runtime.
- **Recovery.** Journal each refresh so that a crash partway recovers to the
  recorded before or after state.
- Runtime changes never enter the source ledger. Runtime export
  (`workspace runtime export`) stays out of scope.

**Files.** `src/llm_cli/workspace/runtime.py`, `src/llm_cli/workspace/isolated.py`,
`src/llm_cli/workspace/isolated_sync.py`, `src/llm_cli/workspace/hydration.py`,
`docs/isolated-workspaces.md`, tests.

**Done when.**
- A non-secret runtime file edited in the primary checkout reaches an idle
  isolated session at its next safe point.
- A file changed on both sides is preserved with one notice.
- A changed `.env` is reported but not copied without the opt-in.
- A change made during a running check waits until the check ends.
- A held proposal's `/apply` checks use its frozen runtime.
- A crash at each refresh step recovers without mixing generations.

### C9. Per-session command environment

**Why.** Commands and checks run with a fixed minimal environment (`PATH`,
`HOME`, locale and a few tool switches; `_environment` in
`execution/commands.py`). So projects whose tests need a variable from the
user's terminal, such as `DATABASE_URL` or `AWS_PROFILE`, fail in both modes.
The workspace plan (§19.1) gives commands the terminal's environment minus
registered credentials. That widens what a model-chosen command can read, so
it starts as an opt-in setting.

**Scope.**

- **Setting.** `agent.command_environment = "minimal" | "session"`, default
  `minimal`. It applies to both commands and checks. The config file
  currently accepts `agent`, `core`, `hooks`, `leases`, `mcp` and `knowledge`
  sections.
- **Transport.** Under `session`, the chat client sends its environment over
  the owner-only socket when it opens or resumes a session. The daemon keeps
  it in memory for that session only. It is never persisted or logged, and
  never put in events or checkpoints.
- **What commands and checks receive.** The minimal environment plus the
  session's variables, except:
  - Loupe's own variables;
  - provider credentials, meaning names registered during provider setup plus
    known provider key names;
  - the sandbox's `HOME`, `TMPDIR` and cache overrides, which stay as they are.
- **Screening.** Add the values of secret-like variable names to output
  screening.
- **Inspection.** Show a names-only view of the environment plan, such as
  `session show`.
- **Daemon restart.** A session whose environment is gone does not run
  commands until its client reconnects. It never falls back to the daemon's
  own environment.
- **Isolated sessions.** Rewrite variables that point into the primary
  checkout to the private equivalent only when an exact hydrated equivalent
  exists, and report each rewrite.

**Files.** `src/llm_cli/cli/session.py`, `src/llm_cli/cli/app.py`,
`src/llm_cli/protocol/`, `src/llm_cli/daemon/service.py`,
`src/llm_cli/execution/commands.py`, `src/llm_cli/execution/checks.py`,
`src/llm_cli/execution/sandbox.py`, `src/llm_cli/config/`, docs, tests.

**Done when.**
- Under `session`, a check sees a custom variable from the chat's terminal,
  while provider keys and Loupe's variables are absent.
- A command that prints a secret-named variable has the value screened.
- After a daemon restart, commands wait for the client instead of running
  with another environment.
- No variable value appears in the database or logs (search for a canary).
- `minimal` behaves exactly as today.

**Implemented.** C9 adds opt-in, memory-only per-session command environments,
role filtering, immutable secret screening (including multiline values), and
names-only inspection. Restarted launches wait for their originating client
without blocking unrelated sessions; cancel/close can settle them. Reconnect
invalidates environment-sensitive check evidence atomically. Exact hydrated
path rewrites support logical checkout aliases and retained runtime generations.
Closed-session held apply can receive a temporary applying-terminal map for fresh
checks without reopening the session. See
[command environments](command-environments.md) for configuration and recovery.

### C10. Dependency rebuild adapters

**Why.** Copying dependency trees has limits: 200,000 entries, 5 GiB and
60 seconds. It also refuses escaping links, as in a pnpm store, and ignores
virtualenvs that live outside the checkout, as Poetry's do. ADR 0009 names an
explicit rebuild as the alternative. No installer runs automatically today.

**Scope.**

- **Configuration.** A new `[isolated.rebuild]` table maps a dependency root
  to an installer argv, for example `".venv" = ["uv", "sync", "--frozen"]` or
  `node_modules = ["npm", "ci"]`. With that setting, the hydration plan shows
  `rebuild` for the root instead of `copy`. The explicit setting is the
  approval; nothing runs without it.
- **Running the installer.** It runs in the private worktree after the source
  is in place, through the command sandbox, with network access only as the
  setting allows. Output is screened, and time and size limits apply.
- **Failure.** The session still opens, with a notice. The partial tree is
  removed, so it is never mistaken for a working one.
- **Recovery.** The rebuild is a durable creation step. After a crash, the
  partial tree is removed and the rebuild runs again.

**Files.** `src/llm_cli/workspace/hydration.py`, `src/llm_cli/workspace/isolated.py`,
`src/llm_cli/workspace/runtime.py`, `src/llm_cli/config/`,
`docs/adr/0009-isolated-runtime-hydration.md`, `docs/isolated-workspaces.md`, tests.

**Done when.**
- A fixture whose `.venv` exceeds the copy budget, or contains an escaping
  link, opens with a rebuild, and its checks import their dependencies.
- A failing rebuild produces the notice and leaves no partial tree.
- A crash during a rebuild recovers.
- Without the setting, nothing runs.

**Implemented (Wave 6 C10).** Explicit shared settings now select bounded,
sandboxed private rebuilds for ignored dependency roots, with network denied
unless individually enabled. Plans do not traverse the primary rebuild root.
Failure removes partial output while allowing the conversation to open; compact
creation journals replay incomplete installers and dispose their private home
on recovery. Rebuilt roots retain normal private check/apply generations and
remain excluded from primary runtime refresh. First-opening secret consent is
frozen across recovery.

Creation records durable `rebuilding` preparation under the publication and
physical-authority mutation locks. Installers then run outside those locks and
the daemon dispatch lock, with a live ownership pin preventing checkout takeover.
Unrelated RPCs, commands, checks and shared publication can continue. Finalization
reacquires protection, verifies the frozen private source and creation identity,
records runtime receipts and activates the workspace. Primary source changes
arrive through ordinary incoming synchronization. Same-session setup reservations
last through daemon registration and refuse duplicate opens without disturbing
the original installer. The opening/resuming caller waits under the active
daemon's installer-specific timeout plus a bounded creation margin.
See [the guide](isolated-workspaces.md#explicit-dependency-rebuilds) for quoted
TOML keys, budgets, unlocked installation and recovery.

**Initial validation:** frozen commit `7f110cfc` passed the covered core (3,028 passed,
one filesystem skip; 87% coverage), the separate strict uncovered index-scale
test, Ruff, mypy for 123 source files, and offline package builds. The focused
rebuild/config/hydration/sandbox/creation/C8 cases passed 193 tests with one
filesystem skip. Regression coverage includes over-budget and escaping primary
dependencies, dependency imports in checks, held apply after restart, failed
installer cleanup and screened diagnostics, hard-crash private-home scrubbing
with late-source quarantine, physical-authority recovery, frozen secret consent
and installer-time source/runtime changes. No billed provider call was needed.

**Follow-up validation:** frozen C10 `592b55ab` passed 3,062 covered tests, and the
combined C9/F3 resolution `501db76f` passed 3,154. Each had one expected
case-sensitive-filesystem skip, one separately measured scale test deselected,
and 87% coverage. Separate 10,000-file / 400,000-function builds took 6.299 seconds
on C10 and 6.338 seconds on the combined resolution against the unchanged
30-second cap. Five real-RPC and identity regressions cover publication during
installation, prompt duplicate refusal and the late registration boundary;
rebuild/recovery, schema-upgrade and sandbox regressions also passed. Ruff,
mypy and offline package builds passed on both frozen branches. These are prior
frozen validation receipts; this documentation merge does not claim a new full
suite result.

### C11. Git index safety and Git-operation reconciliation

**Implementation:** adds read-only preflight checks to batch and manual
publication, and checks held apply before allocating an action. Git observations
run on the existing scan schedule; terminal/model notices contain paths and fixed
guidance. See [Git publication safety](git-publication-safety.md). Validation
receipts are recorded with the package PR.

**Why.** Loupe never runs Git history commands, but it does not yet look at
what you are doing with Git (workspace plan §16).

- **Mid-operation publication.** A publication can land while a rebase,
  merge or cherry-pick is in progress, or while `.git/index.lock` exists.
- **Staged and conflicted paths.** It can also overwrite a file whose staged
  version differs from HEAD, or one with unresolved conflict stages.
- **Commits and branch switches.** These reach sessions only as a burst of
  outside file edits that the C4 scanner catches up on over several scans.
  Nothing records that HEAD moved, and candidates prepared before a branch
  switch are not told their bases may be from another branch.

**Scope.**

- **Before every publication** (batch, manual single-file broker, isolated
  publication, held `/apply`):
  - Return a retryable `GIT_OPERATION_ACTIVE` when `.git/index.lock` exists,
    or when a rebase, merge, cherry-pick or revert is in progress. Never remove
    a Git lock or abort the user's operation.
  - Always refuse a path that has unmerged index stages.
  - For a path whose stage-0 index entry differs from HEAD, give a notice by
    default. A new `[core] refuse_staged_paths = true` setting turns this into
    an `INDEX_PATH_STAGED` refusal.
- **Around every apply:** fingerprint HEAD, the index and any in-progress
  operation, before and after. If they changed during the apply:
  - finish or recover the journal from exact file identities as today;
  - record a `git.concurrent_operation_attention` event with the affected
    paths;
  - rescan before the next publication.
- **Structural reconciliation** at the existing scan points:
  - **HEAD moved, working files unchanged** (an ordinary commit): record
    `git.commit_observed`, with no epoch change. Link known batches to the
    commit only when the exact tree proves they are in it.
  - **HEAD moved and files changed** (checkout, reset, merge, stash apply):
    reconcile the file changes as the scanner does today, increment the
    workspace epoch, and notify every session. Candidates from the old epoch
    must revalidate before they publish.
  - Isolated sessions keep their private HEAD. They see the primary
    checkout's changes through incoming sync, as today.
- Reuse the existing scan schedule; no new daemon loop.

**Files.** `src/llm_cli/workspace/gitstate.py` (new),
`src/llm_cli/workspace/batches.py`, `src/llm_cli/workspace/broker.py`,
`src/llm_cli/workspace/external.py`, `src/llm_cli/workspace/context.py`,
`src/llm_cli/workspace/isolated_sync.py` (publication preflight only),
`src/llm_cli/workspace/workflow.py` (held apply preflight only),
`src/llm_cli/storage/migrations.py`, `src/llm_cli/config/`,
`src/llm_cli/errors.py`, docs, tests.

**Done when.**
- A publication is refused, retryably, during a rebase, a merge or with
  `index.lock` present, and succeeds after the operation finishes.
- An unmerged path is always refused.
- A staged path gives a notice by default and is refused when the setting is
  on.
- A commit made between two publications records `git.commit_observed`,
  with no epoch change and no duplicate file batches.
- A branch switch increments the epoch, notifies every session, and makes an
  old-epoch candidate revalidate.
- A Git operation forced into the apply window (test hook) leaves the
  attention event and a rescan.

### C12. Native file-change watchers

**Implementation complete (October 28, 2026).** FSEvents and inotify, through
`ctypes`, narrow isolated safe points and outside-edit scans; anything a watcher
cannot vouch for gives a full scan, and lost events add a
`CONTEXT_RESYNC_REQUIRED` notice. Publication does not pause during a resync:
it already checks exact bases on disk, so a missed wake-up delays a notice but
cannot overwrite an outside edit. See [the design note](file-watchers.md) and
[measurements](experiments/file-watchers-c12-2026-10-28.md). The outside
catch-up failure that predated C12 (more than 50 changed files, a changed file
over 256 KiB, or a changed path kind) was fixed before the 0.1.0a3 tag.

*Gate met.* The threshold was an isolated safe-point scan or periodic
outside-edit scan above 250 ms on the 20,000-file fixture. E3 measured a
1.94-second safe point after growing an admitted checkout to 20,000 files.

**Why.** Outside edits are found by periodic stat-and-hash scans (C4), and
isolated sessions rescan the whole primary source at every safe point. On
large repositories that cost grows with the file count. Watchers can tell
the scanner where to look instead (workspace plan §17.1, §17.6).

**Scope.**

- **Wake-ups only.** FSEvents on macOS and inotify on Linux, through
  `ctypes`, with no new dependency unless an ADR accepts one. Watcher
  messages only mark paths dirty and schedule a scan; the scanner stays the
  authority.
- **Overflow and loss.** Lost, coalesced or overflowing events, and daemon
  downtime, trigger a full resync. It pauses publication, not agent
  reasoning, and sends sessions a `CONTEXT_RESYNC_REQUIRED` notice instead of
  silently skipping changes.
- **Fallback.** Unsupported platforms or filesystems keep today's periodic
  scans.
- **Measure** scan cost before and after on E3's fixtures
  (`scripts/scan_scale.py`).
- **Not included:** raising admission and coverage limits. Outside scans watch
  each session's latest 1,000 read paths, 20,000-file hydration admission and
  100,000-file isolated capture are refused, and watchers remove none of those
  limits by themselves. State them in the design.

**Files.** `src/llm_cli/workspace/watchers.py` (new),
`src/llm_cli/workspace/external.py`, `src/llm_cli/workspace/isolated_sync.py`,
`src/llm_cli/daemon/service.py` (watcher lifecycle), docs, tests.

**Done when.**
- On the 20,000-file fixture, an outside edit is noticed with the scan cost
  under the threshold.
- A forced overflow produces a full resync and the notice.
- Disabling watchers restores today's behavior exactly.

### C13. Switch a conversation between shared and isolated mode

**Implementation complete (October 24, 2026).** See [verification](experiments/wave9-c13-workspace-switching-2026-10-24.md).

**Why.** A conversation's mode is fixed when it opens. Switching means
closing it and losing its context, even when the user realizes mid-task that
they want a private worktree, or wants to bring finished isolated work back
to the shared checkout (workspace plan §20).

**Scope.**

- **Commands.** `/workspace isolated [--approve-secret PATH …]` and
  `/workspace shared` in the chat, plus their RPC. Switching is allowed only
  between tasks, never while one runs.
- **Durable switch intents.** They follow the §20.3 states (`requested` …
  `confirmed`, with rollback and attention). They are idempotent by intent ID
  and recover at every crash point. A retry never creates a second worktree
  or publishes a candidate twice.
- **Shared → isolated.** Create the private worktree through the existing C2
  and C10 path, then rebind the session to it. Held proposals stay as they
  are. The session receives a bootstrap context packet for the new binding,
  and no packet or edit observation from the old binding carries over.
- **Isolated → shared.**
  - Classify the private state: clean; runtime changes only; held proposal;
    unresolved incoming conflicts; unpublished private edits.
  - Require an explicit disposition: publish (auto mode) or freeze a
    proposal for `/apply` (normal mode), export a patch, keep the isolated
    workspace, or discard.
  - Unresolved conflicts block the switch until resolved. Changed runtime alone
    requires explicit retain/discard after a redacted summary. Unpublished source
    is never silently deleted.
- **Not included:** a `loupe session switch` CLI command. Add it later; it
  needs `cli/app.py`.

**Files.** `src/llm_cli/daemon/service.py`, `src/llm_cli/execution/shared.py`,
`src/llm_cli/workspace/isolated.py`, `src/llm_cli/cli/shell.py` (slash command
dispatch), `src/llm_cli/cli/composer.py` (command list),
`src/llm_cli/cli/session.py` (RPC client, if needed),
`src/llm_cli/storage/migrations.py`, docs, tests.

**Done when.**
- A shared conversation switches to isolated and continues with its history.
  Its next task runs in the private worktree.
- An isolated conversation with unpublished edits switches back only after a
  chosen disposition, and each disposition works.
- A crash at every switch state either resumes or rolls back without a
  second worktree, a duplicate publication or lost source.
- A switch during a running task is refused.

### C14. Managed background processes

**Implementation complete.** Isolated-only processes use the existing command
approval, sandbox, environment and screening policies; shared starts are refused.
Pending private edits are installed before launch. Migration 33 stores owner
binding, PID, start identity and process group. Close stops the whole group;
mode switches refuse with exact profile-aware stop commands. Incoming sync,
runtime refresh and cleanup wait while a process may write. Verified restart
survivors stop; reused or unverifiable identities remain visible and fenced
without unsafe signals. Screened output is bounded to 64 KiB with an 8 KiB tool
tail. Workspace-private Unix sockets support server/client workflows while
TCP/IP remains denied. Owner-bound private receipts retain launch-time values
through runtime changes and restart; model source reads, artifacts, publication
and later output remain screened. Cleanup scrubs private copies and receipts
while preserving ordinary source evidence. Process I/O and waits stay outside
daemon/publication locks; hot RPC source guards use cached state. Closing an
unused process manager does not initialize or query storage. Batched Git metadata
queries preserve fresh discovery and all authority gates during admission. See
[the guide](isolated-workspaces.md#managed-background-processes).

*Depends on C13: switching and closing must stop or keep processes
explicitly.*

**Why.** Every command has a timeout and runs to completion, so an agent
cannot start a dev server, a watch build or a test watcher and use it. The
workspace plan (§19.4) has long-running processes register as managed
processes, so sync and cleanup never race a live writer.

**Scope.**

- **Starting and stopping.**
  - Agent tools `start_process`, `process_output` and `stop_process`, with
    the same approval, sandbox, network policy and output screening as
    `run_command`.
  - Users get `loupe workspace process list|stop`.
- **Where they run.**
  - Isolated sessions run processes in their private worktree.
  - Shared sessions refuse starts; use an isolated session for processes.
- **Tracking.** Record PID plus process start identity plus process group,
  never PID alone. Output goes into a bounded, screened ring buffer. Network
  binding to localhost ports follows the sandbox policy.
- **Lifecycle.**
  - Processes stop when their session closes or the user stops them.
  - A mode switch refuses while processes remain and names each process and
    the exact command to stop it, including while a finished task's worker settles.
  - After a daemon restart, a process whose identity still matches is
    stopped by process group; an unverifiable one is reported, not
    signalled.
  - An isolated workspace with a live process defers incoming sync, refresh
    and cleanup, with a notice.

**Files.** `src/llm_cli/execution/processes.py` (new),
`src/llm_cli/daemon/service.py`, `src/llm_cli/execution/shared.py`,
`src/llm_cli/agent/tools.py`, `src/llm_cli/agent/shared_tools.py`,
`src/llm_cli/workspace/isolated_sync.py`, `src/llm_cli/cli/app.py`,
`src/llm_cli/storage/migrations.py`, docs, tests.

**Done when.**
- An agent starts a small HTTP server in an isolated session, queries it with
  `run_command`, reads its output and stops it.
- `workspace process list` shows the process, and `stop` ends its whole
  process group.
- Closing the session stops it.
- After a daemon restart, a matching process is stopped and a reused PID is
  never signalled.
- Sync and cleanup wait while it runs.

### C15. Export runtime files from an isolated session

**Implementation complete (October 24, 2026).** See [verification](experiments/wave9-c15-runtime-export-2026-10-24.md).

**Why.** Runtime changes inside an isolated session, such as a regenerated
`.env`, a built artifact or a lock file under an ignored path, are discarded
when the session closes. There is no supported way to keep one (workspace plan
§19.6).

**Scope.**

- **The command.** `loupe workspace runtime export PATH --session ID --to
  DEST` copies one runtime file or directory from the session's private
  worktree, through no-follow descriptors and within the hydration budgets.
- **Confirmation.** Show type, size and sensitivity, then require `--yes`.
  A likely secret also needs `--allow-secret`.
- **Destination.** Refuse a `DEST` inside the shared checkout unless
  `--into-checkout` is given, and never overwrite tracked source there.
- **Privacy.** Record only a redacted disposition event. Nothing enters
  model context, artifacts or the source ledger.

**Files.** `src/llm_cli/cli/app.py`, `src/llm_cli/daemon/service.py` (one RPC
handler), `src/llm_cli/workspace/runtime.py`, `docs/isolated-workspaces.md`,
tests.

**Done when.**
- A changed private `.env` exports to a destination outside the checkout only
  with both flags.
- A non-secret build file exports with `--yes`.
- Exporting into the checkout is refused without `--into-checkout`.
- Symlinks and oversized trees are refused.
- No exported bytes appear in the database or logs.

### C16. Bound isolated synchronization-journal storage

**Implementation complete (October 24, 2026).** See [verification](experiments/bounded-sync-journals-c16-2026-10-24.md).

**Why.** Every isolated safe-point sync appends an `isolated_sync_journal` row
holding the whole accepted manifest, and applied rows are never retired. E3's
growing-path soak exceeded its 128 MiB database bound after 52 minutes: 1,524
journal rows and 37 MB of manifest JSON by cycle 153. Growing one admitted
checkout from 7,000 to 20,000 files left 981 MB of SQLite files. Normal
isolated use on an active repository grows the database without limit. Keep the
[failed receipt](experiments/soak-growing-e3-2026-10-22.json) as the regression
workload rather than raising its bound.

**Scope.**

- **Manifests.** The accepted manifest already lives in
  `isolated_sync_state`. Keep a manifest only on `prepared` rows, which
  crash recovery replays; drop it in the same transaction that marks a row
  `applied` and advances the generation.
- **Unresolved conflicts.** `_conflicts()` currently replays the steps of
  every applied row to find unresolved divergences. Keep those in a compact
  per-workspace record, updated in the apply transaction, so applied rows can
  be deleted (or kept as a small bounded window for diagnosis).
- **Migration.** Backfill unresolved conflicts from existing applied rows,
  then compact them, in the migration transaction. Prepared rows are unchanged.
- **Recovery is unchanged.** A crash before, during or after apply recovers
  exactly as today, and a retry never re-applies a step.
- **Not included:** other retention (task output, events), which
  `loupe db prune` already covers.

**Files.** `src/llm_cli/workspace/isolated_sync.py`,
`src/llm_cli/storage/migrations.py`, `src/llm_cli/workspace/isolated.py`
(journal checks only; coordinate with C13), `docs/isolated-workspaces.md`,
docs, tests.

**Done when.**
- 200 syncs over growing paths keep applied-journal storage bounded, apart
  from unresolved conflicts.
- Unresolved divergences survive compaction, migration and restart, and
  resolve exactly as before. The existing conflict tests pass unchanged.
- A crash at each apply and compaction point recovers without re-applying a
  step.
- `PYTHONPATH=src python scripts/soak.py --duration 3600 --growing-paths`
  passes its original 128 MiB bound for the full hour, and the 7,000→20,000
  file growth run's database size is recorded in the PR.

### C17. Isolated catch-up follow-ups

**Why.** The outside catch-up fix (#85) left two known limits:

- **Directory replaced by a file.** A directory that existed when the
  workspace opened has no directory marker. If outside work replaces it with
  a file, catch-up probably reports a conflict on that path even when the
  private copy is untouched. This was read from the code, not tested.
- **Publication lock.** A large catch-up, such as after a branch switch,
  holds the publication lock for its whole duration, so other sessions'
  publications wait for it.

**Scope.**

- **Directory replaced by a file.** Write the test first. If it conflicts,
  treat an opened-with directory whose private subtree is unchanged as
  replaceable: remove the subtree, then create the file in a later journal,
  as catch-up already does for kind changes. A changed private subtree stays
  a conflict on that path.
- **Lock.** Release and reacquire the publication lock between journals so
  shared publications can run in between. Each journal still starts from the
  manifest the previous one confirmed. If the primary changed in between, the
  remaining steps are recomputed rather than replayed from the old plan.
- **Unchanged:** recovery, chunk limits and publication limits.

**Files.** `src/llm_cli/workspace/isolated_sync.py`,
`docs/isolated-workspaces.md`, `tests/integration/test_isolated_sync.py`.

**Done when.**
- An untouched opened-with directory replaced by an outside file arrives at
  the next safe point. With a private edit inside it, the path is a conflict
  and later safe points still work.
- During a 150-file catch-up, another session's publication completes before
  the catch-up finishes (use a fault hook between journals).
- A crash between journals, including after an interleaved publication,
  recovers without re-applying a step. The existing catch-up tests pass
  unchanged.

### C18. Larger isolated admission

**Why.** E3 found two admission limits that real monorepos exceed:

- a checkout with more than 20,000 source paths cannot open an isolated
  workspace ("isolated source exceeds its 20,000-path budget" in
  `workspace/isolated.py`);
- hydration refuses more than 20,000 inventory entries by default
  (`HydrationBudgets.max_entries`, whose ceiling is already 100,000).

E3 said to raise these only with their own measurements.

**Scope.**

- **Measure first.** Extend `scripts/scan_scale.py` to open isolated
  workspaces at 20,000, 50,000 and 100,000 source files with native watchers
  on. Record creation time, peak memory, database size and safe-point time.
- **Explicit settings.** Make the source-path budget and hydration entry
  budget settings, with defaults set from the measurement. A refusal names
  the setting that would admit the checkout.
- **Unchanged:** byte, depth and time budgets; no-follow capture; secret
  screening; the 100,000-entry hydration ceiling.

**Files.** `src/llm_cli/workspace/isolated.py` (source-path budget only),
`src/llm_cli/workspace/hydration.py`, `src/llm_cli/config/models.py`,
`scripts/scan_scale.py`, `docs/isolated-workspaces.md`, `docs/experiments/`,
tests.

**Done when.**
- A recorded measurement at each size justifies the new defaults.
- A checkout at the new default opens isolated and completes a safe point
  within the measured time. One just above it is refused with a message
  naming the setting.
- The settings accept values up to the existing ceilings and refuse values
  beyond them.

### C19. Outside-scan coverage beyond the read window

**Why.** An outside scan covers only the latest 1,000 paths its session has
read (`MAX_PATHS` in `workspace/external.py`). An outside edit to a file the
session read earlier is not reported, so the model can keep working from
stale content until publication's base check refuses it. Since C12, native
watchers already report which paths changed.

**Scope.**

- **Use watcher changes.** While native watchers are active and have not
  overflowed, scan every changed path the session has read, not only the
  latest 1,000. Keep each scan within count, byte and time bounds.
- **Fallback.** On watcher overflow or the polling fallback, keep today's
  window, and state the coverage limit in the notice or status.
- **Storage.** Keep each session's read registrations bounded; measure the
  cost at 20,000 read paths.
- **Unchanged:** notices stay path-only and bounded.

**Files.** `src/llm_cli/workspace/external.py`, `src/llm_cli/workspace/watchers.py`
(read-only additions only), `scripts/scan_scale.py`, `docs/file-watchers.md`,
tests.

**Done when.**
- With native watchers active, a session that has read 5,000 files is notified
  when an outside edit changes the first file it read.
- On overflow or polling, behavior matches today's window and the limit is
  reported.
- The 20,000-file safe point stays under E3's threshold, recorded in the PR.

### C20. Installer cache for dependency rebuilds

**Why.** Each C10 rebuild runs with a fresh, empty installer home
(`loupe-rebuild-home`, deleted afterwards). So every isolated open downloads
all dependencies again: slow, dependent on the network, and failing offline
even right after a successful rebuild.

**Scope.**

- **Opt-in cache.** A setting such as `[isolated.rebuild] cache = true` gives
  installers a private (0700), per-profile cache directory. The cache is
  passed through the cache variables (`XDG_CACHE_HOME`, `UV_CACHE_DIR`,
  `npm_config_cache`, `PIP_CACHE_DIR`). The home itself stays fresh.
- **Who sees it.** The installer sandbox may write the cache. Model commands,
  checks and background processes can neither read nor write it.
- **Integrity.** The cache holds downloaded artifacts, not installed trees.
  Document that it is safe only with installers that verify lockfile hashes,
  such as `uv sync --frozen` and `npm ci`.
- **Bounds.** A size cap, with eviction or pruning when it is exceeded.
  Concurrent rebuilds share the cache through the installers' own locking.
- **Unchanged without the setting:** each rebuild gets an empty home.

**Files.** `src/llm_cli/workspace/rebuild.py`, `src/llm_cli/execution/sandbox.py`
(the cache path allowance only), `src/llm_cli/config/models.py`,
`docs/isolated-workspaces.md`, `docs/adr/0009-isolated-runtime-hydration.md`,
tests.

**Done when.**
- A second open of the same fixture rebuilds with network denied, using what
  the first open cached (use a local package index fixture, not the internet).
- Model commands and checks cannot reach the cache.
- Two concurrent opens both rebuild correctly.
- The cache stays within its cap, and without the setting nothing changes.

### C21. `loupe session switch`

**Why.** C13 added mode switching only as the chat's `/workspace` command and
left the CLI command out because it needs `cli/app.py`. Scripts and other
front ends cannot switch a conversation's mode.

**Scope.**

- **The command.** `loupe session switch SESSION --to shared|isolated`, with
  `--approve-secret PATH` (repeatable) and the chat's dispositions:
  `--publish`, `--hold`, `--export`, `--retain` or `--discard`.
- **Flow.** It uses the existing `workspace.switch_mode`,
  `workspace.confirm_switch` and `workspace.rollback_switch` RPCs. It shows
  the private-state classification and requires an explicit disposition
  before switching to shared. It can confirm or roll back a pending switch.
- **Output.** `--json`, and the CLI's usual exit codes, including 75 for
  retryable refusals.
- **Unchanged:** the daemon. This package adds no RPC.

**Files.** `src/llm_cli/cli/app.py`, `src/llm_cli/cli/session.py` (RPC client,
if needed), `docs/isolated-workspaces.md`, `README.md`, tests.

**Done when.**
- Each of C13's switch outcomes works through the CLI: shared to isolated,
  and isolated to shared with each disposition.
- A switch during a running task is refused, and a pending switch can be
  confirmed or rolled back.
- An unknown or missing disposition is refused before any RPC is sent.

**Implemented (Wave 12).** The standalone command prepares and confirms one
exact target bootstrap and reports classification, runtime summary and retained
work. Shared targets require an explicit disposition before any RPC; isolated
targets accept exact-file consent. `--switch-intent-id` makes retries idempotent,
`--confirm ID --sequence N` acknowledges a pending target, and `--rollback ID`
keeps the source binding. Existing daemon guards and retry exit codes are
preserved. See the [CLI guide](isolated-workspaces.md#switch-from-scripts-or-another-terminal).

### C22. Patch and bundle exports of candidates and batches

*Run after C21 in the same session: both change `cli/app.py`.*

**Why.** Workspace plan §16.6 calls for explicit exports of a candidate or
batch. Today only a mode switch's export disposition writes one. Moving work
to another machine, attaching it to a review or applying it with `git apply`
means reconstructing it from `task diff`.

**Scope.**

- **Commands.** Export a held proposal or a published batch as `--format
  patch` (git-apply compatible) or `--format bundle` (content-addressed bodies
  plus a manifest with exact before identities) to `--to DEST`.
- **Reuse.** Build on `portable_patch` and `write_export_artifact` in
  `workspace/patches.py`, which mode switching already uses.
- **Durability.** `export_intents_v2` (§16.6) records each export, idempotent
  by intent ID. A crash never leaves a partial file at the destination, and a
  retry never writes twice.
- **Destination.** Refuse a destination inside the shared checkout unless
  `--into-checkout` is given, and never overwrite an existing file.
- **Privacy.** Runtime files and screened source bytes are never exported. A
  candidate holding screened content is refused with a path-only reason.
- **Not included:** internal refs, commits or pull requests (§16.6's later
  integration adapter).

**Files.** `src/llm_cli/workspace/patches.py`, `src/llm_cli/workspace/exports.py`
(new), `src/llm_cli/storage/migrations.py` (migration 34),
`src/llm_cli/daemon/service.py` (one RPC handler), `src/llm_cli/cli/app.py`,
`docs/isolated-workspaces.md`, tests.

**Done when.**
- `git apply --check` accepts an exported patch of a held proposal with a
  binary file, a mode change and a symlink, on a clean checkout of its base.
- A bundle's manifest identities match the candidate, and every body hash
  verifies.
- A crash at each export step leaves either no file or the complete file, and
  a retry does not write twice.
- A candidate with screened content is refused, and no exported bytes appear
  in the database or logs.

**Implemented (Wave 12).** `loupe change export` selects a held proposal,
staged candidate or published batch, with optional explicit source kind. Patches
preserve Git binary, mode and symlink changes; bundles retain exact before/after
identities and SHA-256 addressed bodies. Migration 34 journals immutable request
metadata and filesystem receipts, with private staging, no-replace publication
and safe same-intent crash replay. Runtime and screened bytes are refused, and
checkout destinations require explicit consent. Existing daemon and publication
locks do not cover export I/O. See [source exports](isolated-workspaces.md#export-source-changes).

## Lane D — operations

Run D1 → D2 → D3 in one session, in order: they share `cli/app.py`.

### D1. `loupe db backup`

`storage/backup.py` already makes verified online backups; nothing exposes it.
Add `loupe db backup [--out DIR]`, writing timestamped, owner-only copies of
the three databases and printing their paths and integrity results. Done when
a backup taken while the daemon runs opens, passes `PRAGMA integrity_check`,
and holds the latest tasks.

**Implemented:** `db.backup` runs through the daemon and creates a new private
timestamped directory, including the latest committed WAL state. A real-daemon
CLI integration test checks all three integrity results, owner-only modes,
latest-task visibility, repeat backups, and output directories reached
through a symlink, as `/tmp` is on macOS.

### D2. Retention: prune old task output

The default profile's control database is 18 MB after a few weeks; nothing
removes old task events or check output (command snapshots are already
cleaned at daemon start). Add
`loupe db prune --older-than 30d` (dry run by default, `--apply` to delete)
that removes streaming deltas and output chunks for settled tasks older than
the cutoff while keeping each task's record, answer, and summary events, then
runs `VACUUM`. Never touch an unsettled task, a held proposal, or anything a
recovery path still needs. Done when pruning a fixture database keeps every
task inspectable with `task show` and its answer, and recovery tests pass on a
pruned database.

**Implemented:** `db.prune` previews by default and deletes only disposable
output when `--apply` is present, including multipart provisional answer
previews. Multipart committed answers and summary metadata remain intact.
Eligibility checks terminal timestamps and
all claims, workflows, checks, execution checkpoints and publication journals.
A pruned fixture remains inspectable with its final answer and summary; an
interrupted execution retains its evidence and recovers normally. Settled
checkpoints lose the saved conversation and published file contents, which
were most of the profile database's size; an interrupted answer keeps them for
`/retry-answer`, and a failed turn keeps its pending files for `task diff`.
Malformed checkpoint phases protect their task without preventing cleanup of
other settled tasks; dry-run and applied pruning both preserve that evidence.

### D3. `loupe config show` and `set`

Show the effective settings and where each came from (default, config file,
profile), and set a key with validation through the existing loader. Never
print secrets. Done when `show` matches what the daemon uses and `set` refuses
invalid values without changing the file.

**Implemented:** `config.show` uses the daemon's immutable startup settings
and source map; saved differences appear as pending restart. `config.set`
validates a staged private file through the existing loader, serializes
cross-profile writes, and durably replaces the file atomically. Tests cover
active/saved values across restart, secret redaction, concurrent updates and
invalid-value refusal without changing the original bytes/inode/timestamp,
including malformed typed values, unsupported value types and mixed table arrays.
TOML dotted/quoted keys support digit-prefixed server names and punctuated header names.
Credential-looking keys are refused, except MCP header keys whose value is an
environment-variable name; the loader still validates the complete candidate.

### D4. Release 0.1.0a3

**Why.** The last release, 0.1.0a2 (2026-10-11), predates Waves 1–7. Homebrew
users have none of this work. Their upgrade also crosses many schema
migrations and the G2 ownership change, which needs care.

**Scope.**

- **Version.** Bump to `0.1.0a3` in `pyproject.toml` and
  `src/llm_cli/__init__.py`, and refresh `uv.lock`, as
  `docs/macos-releases.md` step 1 describes.
- **Upgrade test.** Build a small profile database from the `v0.1.0a2`
  source, holding sessions, tasks, a held proposal and a published batch, and
  commit it as a test fixture. The test opens it with the current code and
  asserts:
  - migrations apply;
  - the daemon starts and serves;
  - existing history and the held proposal are inspectable;
  - every checkout stays in the legacy regime.
- **Release notes.** Write `docs/release-notes/0.1.0a3.md` with the
  user-visible changes since 0.1.0a2 and these upgrade notes:
  - **Stop old daemons first.** Stop every profile's daemon before upgrading,
    because daemons older than G2 ignore the new cross-profile lock.
  - **Cutover is explicit.** Checkouts stay in legacy mode until
    `loupe workspace cutover`.
  - **Filesystem limits.** Managed operations are refused on filesystems
    outside the supported list.
  - **Defaults.** Independent review and lexical indexing stay off by default.
- **Publishing is your step.** You push the `v0.1.0a3` tag on the reviewed
  commit. The package prepares everything up to that point.

**Files.** `pyproject.toml`, `src/llm_cli/__init__.py`, `uv.lock`,
`tests/fixtures/` (new upgrade fixture), an upgrade test under
`tests/integration/`, `docs/release-notes/0.1.0a3.md` (new),
`docs/macos-releases.md` (link only).

**Done when.**
- The upgrade test passes and fails if a migration is removed.
- CI passes on the version bump.
- You have reviewed the release notes.
- After the tag, the tap's workflow upgrades from 0.1.0a2 on both Mac
  architectures. Before testing a local `brew install`, set
  `HOMEBREW_NO_INSTALL_CLEANUP=1`. Brew's automatic cleanup previously
  removed a Python version that running daemons still used.

## Lane E — reliability

### E1. Stress test survives stalled CI runners

`test_six_cli_instances_repeated_seeded_publication` intermittently fails on
macOS CI with a daemon RPC timeout (5 s, `protocol/client.py`) while the runner
stalls. Find which client timed out. If it is the test's own client, give it a
generous timeout; if it is a chat process, let tests lengthen its timeout
through the environment. It is a test of correctness, not latency. Done when
the test passes under an artificial stall (for example a slowed daemon
handler) and the product default is unchanged.

### E2. Live provider checks (*live*)

Remaining gaps from harness plan items 1–3: a live Anthropic prompt-cache check
and a live oversized-prompt rejection on each provider. Needs a profile signed
in to Anthropic and to OpenAI with API keys, which only the user can provide.
Record results in the harness plan.

**Wave 12.** Taken off hold on 2026-10-30 (it was on hold from 2026-10-16 at
the user's request). Sign both providers in to a profile other than `default`
before starting.

**Files.** A live check script under `scripts/` (new), `src/llm_cli/providers/`
only if a check finds a bug, `docs/cli-harness-response-plan.md`.

**Done when.**
- On each provider, an oversized prompt is reported as
  `provider_error: context_overflow`.
- A repeated Anthropic request reports prompt-cache reads.
- The results are recorded in the harness plan.

### E3. Long-running soak and scan-cost measurement

**Why.** Each package is tested in isolation, and the longest combined runs
are the ~20-minute test suite and short live smoke tests. Nothing checks
that hours of mixed use keep every invariant and stay within resource bounds.
C12 also needs numbers before anyone builds watchers.

**Scope.**

- **The soak run.** `scripts/soak.py` drives scripted-provider sessions, so no
  model calls are billed, against a disposable repository. It mixes shared
  and isolated sessions, and each session edits, publishes, applies and
  undoes. Outside edits, commits and branch switches run alongside, with a
  daemon kill and restart every few minutes. Duration and seed are
  configurable; the default is 30 minutes.
- **Invariants, checked periodically and at the end:**
  - no published change is lost;
  - no journal is left unrecovered;
  - no task is stuck;
  - every ownership marker ends clean;
  - no private worktree or runtime copy is left over.
- **Resources to watch.** Database and candidate-directory growth, the
  daemon's open file descriptors, threads and memory, and RPC latency
  percentiles. Each must stay within recorded bounds.
- **Scan-cost measurement.** Measure the outside-edit scan and the isolated
  safe-point scan on synthetic 20,000- and 100,000-file repositories, and
  record the results for C12's gate.

**Files.** `scripts/soak.py` (new), `scripts/scan_scale.py` (new),
`tests/unit/test_soak.py` (option and invariant checks), `docs/experiments/`,
`docs/implementation-status.md`.

**Done when.**
- A one-hour local run passes every invariant and resource bound, with its
  record committed.
- The scan-cost table is recorded with a recommendation on C12.
- A deliberately injected lost write (test hook) makes the soak fail.

## Lane F — knowledge

Phase 12 of the workspace plan (§13.4, §13.6) and ADR 0005. F1 now provides an
opt-in local lexical store with source, document, chunk, FTS, job and generation
metadata. Knowledge and vector stores remain absent by default; existing stores
are preserved when indexing is off. The agent still finds code with exact and
regular-expression search (B5); F1 is explicit CLI/RPC discovery. F2 adds the
optional graph, and F3 measures agent retrieval benefit before F4 adds embeddings.
Throughout this lane, retrieval may only help discovery. It
never answers coordination questions, such as who changed a file, what a
current base is, or whether a candidate can publish. Coordination output must
be identical with the lane's features on or off.

### F1. Knowledge store and lexical index

**Scope.**

- **Schema.** Knowledge migrations for sources, documents, chunks, an
  external-content FTS5 table, projection jobs, per-checkout cursors and
  generation metadata, as listed in §13.6.
- **Projection outbox.** When a batch or an outside-change event commits,
  record IDs only, in a control-database outbox row inside the same
  transaction. Never write `knowledge.sqlite3` while holding the publication
  barrier.
- **Indexer.** A daemon background worker reads accepted file versions at a
  bounded revision. It excludes ignored, runtime, protected and secret-flagged
  content. It chunks deterministically by line windows, records the chunker
  version, stages a new generation, verifies counts, then swaps generations.
- **Commands.** `loupe index status|build|rebuild|drop` and
  `loupe search lexical QUERY [--explain]`. Each result cites its path,
  content identity, indexed revision, line range and generation. A result
  older than the current file is labeled stale.
- **Setting.** A new `[knowledge]` section with
  `index = "off" | "lexical"`, default `off`. When off, nothing is created or
  scheduled.
- **Independent failure.** An index failure records health in
  `doctor`/`db status` and never blocks publication or sessions.

**Files.** `src/llm_cli/knowledge/` (new: store, chunker, indexer, search),
knowledge migrations in `src/llm_cli/storage/migrations.py`, the outbox insert
in `src/llm_cli/workspace/batches.py` and `src/llm_cli/workspace/external.py`,
worker scheduling in `src/llm_cli/daemon/service.py`, the setting in
`src/llm_cli/config/`, commands in
`src/llm_cli/cli/app.py`, status in `src/llm_cli/doctor.py`, tests, docs.

**Done when.**
- Indexing this repository and searching it returns cited chunks.
- After a publication and an indexer pass, search shows the new content, and
  the old content is not presented as current.
- `.env` content and screened files never reach the index, checked by
  searching the database for a canary.
- An unwritable `knowledge.sqlite3` leaves publication and sessions working,
  with an error in health.
- Dropping and rebuilding gives identical chunk IDs.

**Implemented (Wave 4 F1):** Explicit lexical CLI/RPC discovery uses the shared
`[knowledge] index = "off" | "lexical"` configuration, default off. A build
subscribes one registered checkout and captures exact Git/accepted-artifact
versions; an independent worker projects accepted publication/outside-event IDs
into transactional knowledge generations. Citations include exact revisions,
identities, line ranges and generation, with stale labels and current privacy
filtering. Private isolated intermediates are excluded. Off creates no optional
store, worker or projection intent; failures do not block sessions/publication.
Drop disables the checkout subscription before derived deletion. The
[lexical search guide](lexical-search.md) covers commands, limits and recovery.

Source revision `507bd41` passed 73 focused lexical/backup/configuration/schema/
scanner tests, repository Ruff, mypy on 116 source files and distribution builds;
the full suite is pending. An offline real-Loupe build/search/drop/rebuild at
`fa6632a` produced 338 documents and 2,560 chunks in 7.16 seconds, with cited
results and identical rebuilt chunk IDs. Agent retrieval, graph and embedding
behavior remain F2/F3/F4 work; this run does not establish answer-quality gains.

### F2. Deterministic code graph

**Scope.**

- **Graph.** Graph nodes and edges in `knowledge.sqlite3`. Nodes are files
  and Python modules, classes and functions. Edges cover imports (parsed with
  `ast` for Python, and a small tokenizer for JavaScript and TypeScript
  `import` and `require`), tests linked to the code they import, and changed-by
  links to ledger batches.
- **Evidence.** Each edge cites its parser and version, the file's content
  identity, and its generation.
- **Parse failures** are recorded per file and are not fatal.
- **Dependencies.** None new; a parser dependency would need an ADR.
- **Command.** `loupe graph neighbors NODE [--edge TYPE] [--depth N]`.

**Files.** `src/llm_cli/knowledge/` (graph and parsers, indexer hook),
knowledge migrations, the `graph` command in `src/llm_cli/cli/app.py`, tests,
docs.

**Done when.**
- The neighbors of `workspace/batches.py` list its importers and its tests.
- After a publication removes an import, the edge is gone in the next
  generation.
- A file that fails to parse leaves the rest of the graph intact.
- Coordination output is identical with the graph on and off.

**Implemented (October 18, 2026).** F2 extends the opt-in lexical store with
transactional graph nodes, import/test/definition edges and links to the latest
accepted batch. Python uses `ast`; JavaScript/TypeScript use a bounded tokenizer
without a new dependency. Changed-document updates remove prior edges while
preserving unaffected rows. Every returned edge cites parser/version, source
identity/revision and active/indexed generation; syntax failures are recorded per
file. CLI/RPC neighbors support edge filters and depth 1–4, with current policy
revalidation, private-intermediate traversal refusal and bounded results.
Graph traversal skips expression trees while preserving statement and scope facts.
The earlier, simpler 10,000-file fixture built in 3.82 seconds; it differs from
the final dense fixture with 40 functions per module (400,000 function nodes).
At `0ab3fcd`, that dense 10,000-file build took 11.655 seconds under coverage,
within the unchanged 30-second budget. Single-publication medians were 40.5 ms
at 1,000 files and 46.2 ms at 10,000. A single before/after profile of the dense
fixture measured 12.683 to 10.324 seconds overall and 4.517 to 2.298 seconds in
parsing; these local timings do not establish general throughput gains. See the
[code graph guide](code-graph.md) for parser limits and upgrade/rebuild behavior.

At `aa6a5c1`, compact internal source/document references reduced the same dense
build to 8.301 seconds under coverage, retaining all 400,000 function nodes.
Publication updates measured 43.2 ms at 1,000 files and 37.6 ms at 10,000.
The graph-only format migration preserves lexical search while each graph awaits
its complete rebuild; public graph IDs and evidence are unchanged.

### F3. Optional retrieval in the agent, with a live comparison (*live*)

**Scope.**

- **The tool.** A read-only `search_project` tool offered to the agent and to
  exploration helpers when `knowledge.index` is on. It returns lexical
  results plus bounded graph expansion.
- **Labeling.** Results appear in a labeled `optional_retrieval` block after
  the mandatory coordination context, with stale results marked. The tool
  cannot acknowledge events or authorize writes, and its calls count against
  budgets.
- **Live comparison.** Run exploration-heavy tasks with the tool on and off,
  three times each. Use `trace-shared-edit`, `repo-summary`, and two or three
  new "where does X happen" tasks against this repository. Compare answer
  scores, tokens, tool calls and time.
- **Recommendation.** Record whether to continue to F4 and whether the tool
  should be on by default.

**Files.** `src/llm_cli/agent/tools.py`, `src/llm_cli/agent/shared_tools.py`
(read-only view), `src/llm_cli/knowledge/search.py`, `scripts/live_benchmark.toml`,
tests, `docs/implementation-status.md`, `docs/experiments/`.

**Done when.**
- Scripted tests show the tool only when the index is on.
- Stale results carry their label.
- A missing or broken index degrades with a notice instead of an error.
- The live comparison is recorded with its recommendation.

**Implemented (Wave 6 F3, live comparison complete; indexing stays off).** The opt-in
`search_project` tool offers bounded cited lexical snippets plus one graph hop to
main agents and read-only helpers. It revalidates source privacy and identities
in each task's root, labels stale evidence, counts calls/returned bytes, and
fails softly without changing coordination or initializing a missing store.
Mandatory coordination context precedes optional result blocks; tight context
budgets preserve the mandatory facts. Scripted visibility, privacy, stale/broken
index and cursor/write-authority negative controls cover these boundaries.
The five-task off/on corpus is pinned to merged main `6e5d65d`; all three
alternating repetitions now yield 30 valid cells and 15 matched pairs. Both
arms passed 12/15 contracts. On covered 90/102 facts versus 92/102 off; median
paired time increased 6.5 seconds and tool use six calls. Keep indexing off and
F4 gated. Seven retrieval calls came entirely from helpers.

The user superseded the old token ceiling. Four valid cells remain unchanged;
26 fresh cells followed a multiline input-framing repair. Valid failures stay
in the comparison, and malformed-attempt usage stays in cumulative accounting.
The equivalent shipped harness fix followed measurement and passed 42 tests.
Eligible usage is 17,904,476 known tokens plus 76 unknown attempts; cumulative
known usage, including excluded and historical attempts, is 22,677,799 plus 80
unknown attempts. Only one pair has exact token accounting. The small fixture
supports no overall token-savings or statistical-significance claim. See
[optional retrieval](optional-retrieval.md) and the
[completed comparison](experiments/optional-retrieval-f3-2026-10-21-sol.md);
the older pilot and bounded-attempt records remain unchanged.

### F4. Embeddings with exact scoring

**Gate.** F3 showed no benefit from lexical retrieval. Run only if F5's
offline comparison recommends it, or the user asks for semantic search.

**Scope.**

- **Provider interface.** An embedding interface separate from chat
  providers.
- **Backends.** `none` (the default) and `exact`. `exact` stores normalized
  vectors in `vectors.sqlite3`, recording model, dimensions, normalization and
  task type per generation. A change of model or dimensions creates a new
  generation.
- **Hybrid search.** Combine lexical and vector results with reciprocal-rank
  fusion. Each result says which arms ran and why any arm was skipped.
- **Privacy.** Repository content goes to a remote embedding provider only
  under an explicit transmission policy.
- **Out of scope.** `sqlite-vec` stays out until it is qualified, as ADR 0005
  requires.

**Files.** `src/llm_cli/knowledge/` (vectors, embedding interface), vector
migrations in `src/llm_cli/storage/migrations.py`, `src/llm_cli/config/`, the
`search` command in `src/llm_cli/cli/app.py`, tests, docs.

**Done when.**
- Exact cosine scoring passes conformance fixtures.
- A model change creates a new generation.
- Deleting `vectors.sqlite3` leaves lexical search working.
- A recording fake provider shows that no content is sent without the
  policy.

### F5. Offline comparison for semantic retrieval

**Why.** F3's lexical retrieval covered 90 of 102 expected facts against 92
without it, so F4 is gated until there is a stronger case. Building F4 into the
product first would be wasted if semantic search doesn't beat lexical search
either. An offline retrieval comparison answers that cheaply.

**Scope.**

- **Prototype outside the product.** An experiment script chunks a
  repository, embeds the chunks with one embedding model, scores queries by
  exact cosine similarity, and fuses the result with the existing lexical
  index by reciprocal-rank fusion. Nothing under `src/` changes.
- **Model.** Prefer a local model: no billing, and no code leaves the machine.
  A hosted model needs a profile the user signs in, and may be sent only
  public repositories.
- **Queries lexical search finds hard.** At least 30 "where does X happen"
  questions phrased without the code's identifiers, each with the files that
  answer it, over this repository and one larger public repository.
- **Measures.** Recall@5 and recall@10 of the expected files for lexical,
  semantic and hybrid search; embedding cost; index build time.
- **Recommendation.** Open F4 only if hybrid search clearly beats lexical
  search on these queries. Record the decision and the numbers in F4's gate.

**Files.** `scripts/semantic_retrieval_eval.py` (new),
`scripts/semantic_retrieval.toml` (new), `tests/unit/test_semantic_retrieval_eval.py`
(new; scoring and fusion only), `docs/experiments/`,
`docs/implementation-status.md`, this plan's F4 gate.

**Done when.**
- Unit tests cover recall scoring and rank fusion on fixed inputs.
- The recorded run has a recall table for all three arms on both
  repositories, with cost and build time.
- F4's gate records go or no-go with those numbers.

## Lane G — authority across profiles

**Original problem before G2.** Each profile has its own state directory and
control database. Two profiles on the same checkout were separate coordination
domains:

- Each daemon believed it was the only publisher, so neither publication
  barrier saw the other's publications.
- One profile's unfinished publication journal was invisible to the other.

The workspace plan's full answer includes an OS lock in a persistent per-user
root, handoff markers with regime generations, handoff equality keys,
offline-drift adoption, and a legacy-to-workspace regime cutover (§14.22,
§24.2, Phase 11). G2 now implements bounded physical ownership, dirty/clean
markers and conservative original-owner recovery under accepted
[ADR 0010](adr/0010-cross-profile-publication-authority.md). Handoff equality,
offline-drift adoption and the claim-free cutover remain deferred.

The legacy claim scheduler still runs beside workspace publication.
Sessionless `loupe run` tasks, `loupe claim` and exclusive claims use it
(`coordination/coordinator.py`). The accepted G1 decision retains that contract
in G2; G3's explicit claim-free migration remains separate.

### G1. Multi-profile authority: design and ADR

**Scope.** Write ADR 0010, covering:

- **The real risk.** Which workflows put two profiles on one checkout (for
  example benchmark profiles, or one profile per account), and what goes
  wrong today. Back this with a checked-in reproduction: two daemons, two
  profiles and overlapping publications, as an expected-failure test.
- **The minimum guarantee.** Likely:
  - a profile-independent physical lock key from the device and inode of the
    checkout root and its Git admin directory;
  - an OS `flock` held by the publishing daemon in an owner-only, persistent
    per-user root (not `/tmp` and not a profile directory);
  - read-only access and `CHECKOUT_OWNED_BY_OTHER_PROFILE` for other profiles;
  - a dirty marker written before a journal applies, so that another profile
    refuses to publish over an unfinished journal.

  Say what is deferred or dropped from §24.2, such as handoff equality keys
  and offline-drift adoption, which C4's scanner may cover on takeover.
- **Ownership.** When a daemon acquires and releases ownership, how idle
  release works, and how a stale owner is detected (PID plus process start
  time, never PID alone).
- **The legacy path.** What still uses claims, and whether sessionless tasks
  move to workspace publication or keep legacy behavior under a per-checkout
  regime. Cover how queued and active claims migrate without being silently
  dropped, and when `loupe claim` mutation is deprecated.
- **Implementation split.** Rewrite G2 and G3 below with their final scope,
  files and done-when.

**Files.** `docs/adr/0010-*.md` (new), this plan, a reproduction test under
`tests/integration/`. No product code.

**Done when.** The user accepts the ADR, and G2 and G3 are rewritten from it.

**Accepted (2026-10-18).** The user explicitly accepted ADR 0010 and the Wave 5
plan. G1's original real-daemon strict expected-failure witness proved the same-base
overwrite window. G2 converts that checked-in witness to passing owner/refusal
assertions. The bounded G2/G3 split below remains the accepted implementation
contract; G1 itself introduced no product authority behavior.

### G2. One publisher per physical checkout

**Acceptance satisfied (2026-10-18) for G1's
[ADR 0010](adr/0010-cross-profile-publication-authority.md).** This package
protects the current claim-backed execution contract; it does not perform G3's
claim-free cutover.

**Scope.**

- Verified physical checkout/admin keys and a separate common-Git-store key;
  persistent account-home authority root selected through `getpwuid`, independent
  of profile, XDG and runtime overrides. Unsafe/unverified storage fails closed.
- Held kernel locks, PID plus process-start diagnostics, immutable local DB-instance
  identity/binding and versioned, checksummed, fsynced dirty/clean markers.
- Acquire/pin ownership for all managed mutation and consistent-read capabilities,
  startup/explicit recovery, isolated creation, manual/batch/held publication and
  legacy ref/worktree operations. Release only clean, unpinned ownership after
  30 seconds idle; worker threads and unresolved journals prevent early release.
- Other profiles receive labeled read-only observation and
  `CHECKOUT_OWNED_BY_OTHER_PROFILE`; dirty foreign/mismatched database ownership
  receives `CHECKOUT_FOREIGN_RECOVERY_REQUIRED`. Status/doctor report the state.
- Mark dirty before a journal becomes applicable or any path changes, and clean
  only after durable DB/events and final-path verification. Foreign profiles
  never clear/replay an owner's journal. Missing/unknown markers and missing
  recovery proof require attention.
- A clean handoff re-observes stable current source and reports an epoch/history
  gap without changing retained candidate bases. No global protected equality
  key, exact cross-profile history, restore-generation layout or old-binary
  downgrade fence is claimed; ADR 0010 defines those deferred boundaries.

**Files.** New `src/llm_cli/workspace/authority.py` and installation-identity helper;
`storage/migrations.py`, `storage/control.py`, `paths.py`, `errors.py`,
`daemon/service.py`, `workspace/batches.py`, `workspace/workflow.py`,
`workspace/isolated.py`, legacy manual publication wiring in `daemon/service.py`,
`execution/runner.py`, `execution/recovery.py`, `git/worktrees.py`,
`git/integrate.py`, `doctor.py`, owner/status rendering and tests/docs.
Coordinate all storage and daemon wiring under one integration owner.

**Done when.**

- G1's real two-profile reproduction is a passing owner/refusal regression,
  with no expected-failure annotation; independent clones remain independent.
- Aliases contend on one key, distinct linked worktrees have distinct checkout
  keys and share their Git-store operation lock; path/identity changes fail closed.
- Process tests cover idle reacquisition, PID reuse, environment overrides and
  shutdown with a worker still running. No live worker loses its ownership pin.
- Crash injection at dirty/journal/file/DB/clean-marker boundaries prevents a
  foreign profile from publishing or recovering; original-owner exact recovery
  succeeds or retains explicit attention when proof is unavailable.
- Changed physical DB objects or missing bindings, dirty-journal mismatches,
  missing/corrupt/unknown markers, unsafe roots and unknown filesystem semantics
  cannot silently initialize authority. In-place manual restore preserving inode
  and ID is explicitly unsupported, not claimed to be fenced. One blocked checkout
  does not disable status/read-only access or unrelated checkouts.
- Clean handoff notices expose no runtime/source bytes, do not invent history,
  and stale retained proposals still pass ordinary merge/check/base validation.

### G3. Legacy claim cutover

**Depends on G2 and acceptance of ADR 0010.** This package implements the selected
Phase 11 cutover; an authority-regime flag alone is insufficient because today's
shared and isolated runners still require claims.

**Scope.**

- Add a read-only conversion plan covering all physical checkout mappings across
  repo keys, targets and adapters, preserving registration provenance. Ambiguous
  overwritten clone paths block automatic conversion.
- Add durable claim-free workspace executions and advisory intents using additive
  tables/contracts. Preserve old claim-bound executions, task refs, checkpoints,
  histories, held proposals and inspect/export access.
- Upgrade G2's versioned compatibility marker to monotonic
  `legacy → transition → workspace` regimes. Require explicit cutover after a
  complete preview; ordinary upgrade retains the legacy contract.
- Gate both new claim requests and waiter activation for every relevant mapping
  during transition. Queued claims remain legacy by default or receive explicit
  cancellation; active/expired work drains/recovers or is explicitly cancelled
  with evidence retained. Ambiguous publishing/integration blocks cutover.
- Journal marker/DB transitions with an idempotent intent and exact checkpoint;
  resume after crashes without decrementing the global generation. Validate the
  compatible regime at every final execution/publication/recovery boundary.
- In workspace mode, sessionless coding uses durable one-shot workspace sessions;
  fixture/ref work stays explicit legacy/export work. Deprecate `loupe claim`
  mutation for one compatibility release; retain read/status/history commands.

**Files.** New workspace migration/planning module and claim-free execution
module(s); `coordination/coordinator.py`, `coordination/models.py`,
`storage/migrations.py`, `storage/control.py`, `daemon/service.py`,
`execution/shared.py`, `execution/runner.py`, `execution/recovery.py`,
`workspace/authority.py`, `workspace/workflow.py`, `cli/app.py`, protocol
capabilities/errors, tests and docs. One owner coordinates storage/daemon/runner
changes; physical locking remains G2's implementation.

**Done when.**

- Upgrade and process tests cover queued, active, expired, publishing,
  integrating, confirmed and held work across several mappings/targets; every
  row/ref has a visible disposition, and ambiguous evidence blocks conversion.
- New workspace tasks create no legacy editing claim; explicit legacy tasks
  retain their prior contract. Transition blocks both request and activation,
  and the two incompatible authorities never operate on one checkout together.
- Crash/retry at every intent/marker/DB boundary resumes one transition with
  monotonic generations; older G2 marker readers fail closed on the new format.
- Pre-cutover diffs, histories, refs and supported apply/undo remain inspectable
  or explicitly exportable. Compatibility clients receive stable capability
  errors, and feature rollback never deletes the new ledger or revives old claims.

**Command contract.** `loupe workspace cutover plan [PATH]` is a complete
read-only preview. `apply --checkpoint TOKEN` explicitly starts the irreversible
transition; optional repeatable `--cancel-claim ID` selects only those
queued-claim cancellations, while defaults retain queued work and drain/recover
active work. `resume --intent ID --checkpoint TOKEN` continues the exact saved
intent. `status` and explicit `export --out FILE` retain inspection and metadata
export without exposing runtime, credentials, conversations or source bodies.
See [workspace cutover](workspace-cutover.md). Ordinary upgrade remains legacy;
new claim-free records are additive, old rows/refs stay readable, and no legacy
flag bypasses a converted checkout. F4 remains gated by F3's recorded result;
E2 stays deferred.

A follow-up (#72) let saved cutovers finish despite source edits. Later stages
and `resume` compare retained legacy history only. A saved intent reads as
`transition` before its marker advances. Another profile's ownership after the
intent still needs operator attention; G4 adds tooling for that.

### G4. Authority maintenance and stuck-cutover recovery

**Why.** Three gaps remain from the G2 and G3 reviews.

- **Ownership records pile up.** Every checkout ever owned leaves a directory
  under `~/.llm-coord/physical-authority/v1/checkouts`, and nothing removes
  records for checkouts that are gone. Sixteen leftover test records had to
  be moved out by hand.
- **Some stuck cutovers can't be recovered.**
  - If another profile owned the checkout after a cutover intent was saved,
    `resume` refuses and nothing else applies.
  - The same holds if retained legacy history really changed during the
    transition.
  - Either way the checkout stays in `transition`, with legacy claims
    disabled and workspace mode not active.
- **Filesystem errors are vague.** On an unsupported filesystem, such as
  WSL's `/mnt/c`, a devcontainer mount, exFAT or FUSE, every managed
  operation is refused without naming the filesystem.

**Scope.**

- **Pruning ownership records.**
  - Record the checkout path in `owner.json` for diagnostics only.
  - Add `loupe workspace authority status|prune`, as a dry run unless
    `--apply`.
  - Prune lists a record only when all three hold: its lock is free, its
    marker is clean, and its recorded path no longer resolves to the same
    physical identity.
  - Never remove a record that this profile's database binds. Records with no
    recorded path are listed and pruned only with `--include-unrecorded`.
- **Stuck-cutover recovery.**
  - Amend ADR 0010 with a recovery design and get your acceptance.
  - Candidates:
    - `cutover abandon` for an intent whose transition marker was never
      written, returning this profile to legacy;
    - an operator-acknowledged `resume --accept-current-history` that records
      the history change as a disposition before continuing.
  - The checkout regime and intent stage can only move forward, enforced by
    database triggers, so abandoning needs a migration.
  - Implement only what the amendment accepts.
- **Filesystem errors.** Name the detected filesystem and the supported list
  in the refusal. Whether unverified filesystems may run as a single profile
  with a warning is your decision; do not change ADR 0010's fail-closed rule
  without it.

**Files.** `src/llm_cli/workspace/authority.py`,
`src/llm_cli/workspace/regime.py`, `src/llm_cli/cli/app.py`,
`src/llm_cli/daemon/service.py` (one RPC handler for cutover recovery),
`src/llm_cli/doctor.py`, `src/llm_cli/storage/migrations.py` (if recovery needs
it), `docs/adr/0010-cross-profile-publication-authority.md`,
`docs/workspace-cutover.md`, `docs/cross-profile-authority.md`, tests.

**Done when.**
- `authority prune` lists and removes only records that are free, clean and
  stale, and never one bound by this profile.
- A fixture with another profile's ownership after a saved intent can be
  recovered through the accepted command.
- The filesystem refusal names the filesystem.
- The ADR amendment is accepted.

## Later

Wave 12 took up the earlier Later list: larger admission (C18) and scan
coverage (C19), the installer cache (C20), `loupe session switch` (C21),
patch and bundle exports (C22), review on natural tasks (B7), the case for
embeddings (F5, then F4) and E2's live checks. These remain outside it:

- **The `sqlite-vec` accelerator and opt-in task memory.** Both follow F4 and
  stay out unless F5 opens it.
- **Internal refs, commits and pull requests as exports** (§16.6's later
  integration adapter), after C22.
