# Parallel work plan

Date: 2026-10-13, extended 2026-10-16. Waves 1–3 started from `main` after
#38, #39 and #41 and are merged (through #56). Waves 4–7 start from `main` at
#56.

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

## At a glance: Waves 4–7

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
  currently accepts only `agent`, `core`, `hooks`, `leases` and `mcp`
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

### C10. Dependency rebuild adapters

**Why.** Copying dependency trees has limits: 200,000 entries, 5 GiB and
60 seconds. It also refuses escaping links, as in a pnpm store, and ignores
virtualenvs that live outside the checkout, as Poetry's do. ADR 0009 names an
explicit rebuild as the alternative. No installer runs automatically today.

**Scope.**

- **Configuration.** A new `[isolated.rebuild]` table maps a dependency root
  to an installer argv, for example `.venv = ["uv", "sync", "--frozen"]` or
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

**On hold** (2026-10-16), at the user's request.

## Lane F — knowledge

Phase 12 of the workspace plan (§13.4, §13.6) and ADR 0005. Today
`knowledge.sqlite3` and `vectors.sqlite3` hold only bootstrap tables, and the
agent finds code with exact and regular-expression search (B5). Nothing
measured so far has been limited by retrieval. F1 and F2 are therefore local,
opt-in and cheap to drop, and F3 measures whether retrieval helps before F4
adds embeddings. Throughout this lane, retrieval may only help discovery. It
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

### F4. Embeddings with exact scoring

**Gate.** Run only if F3 shows retrieval helps, or the user asks for semantic
search.

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

## Lane G — authority across profiles

Each profile has its own state directory and control database. Two profiles
on the same checkout are therefore separate coordination domains:

- Each daemon believes it is the only publisher, so neither publication
  barrier sees the other's publications.
- One profile's unfinished publication journal is invisible to the other.

The workspace plan's answer is large: an OS lock in a persistent per-user
root, handoff markers with regime generations, handoff equality keys,
offline-drift adoption, and a legacy-to-workspace regime cutover (§14.22,
§24.2, Phase 11). None of it is in the code yet.

The legacy claim scheduler also still runs beside workspace publication.
Sessionless `loupe run` tasks, `loupe claim` and exclusive claims use it
(`coordination/coordinator.py`). G1 decides how much of the plan is needed
before anything is built.

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

### G2. One publisher per physical checkout

*Provisional; G1 sets the final scope.*

**Scope.**

- The physical lock key and the persistent per-user authority root.
- The owning daemon holds the lock, and publication and isolated-session
  creation require it.
- Other profiles get read-only observation and the stable error.
- A dirty marker around journals, and refusal to publish over another
  profile's unfinished recovery.
- `doctor` and `workspace status` show the owner.

**Files.** `src/llm_cli/workspace/authority.py` (new), `src/llm_cli/workspace/batches.py`,
`src/llm_cli/workspace/broker.py`, `src/llm_cli/workspace/isolated.py`,
`src/llm_cli/daemon/service.py`, `src/llm_cli/paths.py`, `src/llm_cli/doctor.py`,
tests, docs.

**Done when.**
- With two daemons under two profiles on one checkout, only one publishes and
  the other gets the stable error.
- Killing the owner partway through a journal leaves a dirty marker, and the
  other profile refuses to publish until the owner recovers.
- A symlinked path to the checkout resolves to the same lock.
- An identity that cannot be proved fails closed for publication.
- G1's reproduction test passes.

### G3. Legacy claim cutover

*Provisional; G1 sets the final scope.* Phase 11 of the workspace plan.

**Scope.**

- A per-checkout regime (`legacy`, `transition`, `workspace`) carried by
  G2's marker.
- Queued and active legacy claims are drained with an explicit disposition
  for each.
- Legacy execution refuses under the `workspace` regime.
- Internal task refs stay inspectable and exportable.
- `loupe claim` mutation is deprecated for one release.

**Files.** `src/llm_cli/coordination/coordinator.py`, `src/llm_cli/storage/migrations.py`,
`src/llm_cli/daemon/service.py`, `src/llm_cli/cli/app.py`,
`src/llm_cli/workspace/authority.py`, tests, docs.

**Done when.**
- Upgrade fixtures cover queued, active, expired, publishing and integrating
  legacy rows, and none is silently discarded.
- The two authorities are never active on one checkout at once.
- Pre-cutover work stays inspectable.

## Later

These are deliberately outside Waves 4–7:

- switching a conversation between shared and isolated mode (§20);
- managed background processes, such as `workspace process` for dev servers
  and watchers (§19.4);
- runtime export (§19.6);
- native file watchers and reconciliation of Git operations (the C4
  follow-up);
- the `sqlite-vec` accelerator and opt-in task memory;
- E2.
