# Parallel work plan

Date: 2026-10-13. Baseline: `main` after #38 and #39, plus #41 (merging
pending edits onto changed files), which several packages below build on.

The remaining work, split into packages that separate sessions can run at the
same time. Each package is sized for one session, names the files it may
change, and says what "done" means. Packages in the first wave touch disjoint
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

## At a glance

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
| E2 | Live provider checks: Anthropic cache, oversized prompts (*live*) | Reliability | S | — | — | 1 |
| F1 | Knowledge, graph and RAG planes | Deferred | XL | most of the above | — | later |

**First wave, all at once:** A1, A2, B3, B4, B5, E1, E2, and one session
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

### C3. Isolated mode: publish and receive batches

**Depends on C2.** Publish an isolated session's changes to the shared
checkout through the batch publisher (merging with #41's machinery when the
checkout moved), and apply other sessions' published batches into the
isolated worktree at safe points, preserving local edits.

**Done when.** A shared and an isolated session work on the same repository;
each sees the other's published work, and neither loses edits.

### C4. Track edits made outside Loupe while sessions work

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

## Deferred

### F1. Knowledge, graph and RAG planes

Phase 12 of the workspace plan and ADR 0005. Deliberately last: it is opt-in,
needs the durable lifecycle above to settle, and nothing measured so far is
limited by retrieval.

### Not yet scoped

The full multi-profile authority cutover and legacy-claim migration (workspace
plan §14.22, §24.2, Phase 11) needs its own design pass before it can be split
into packages.
