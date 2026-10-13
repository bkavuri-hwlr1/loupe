# Benchmark fixtures

Small repositories for `scripts/live_benchmark.py`. A task that names a
`fixture` runs against a fresh Git repository made from that directory, so
edits, checks, and reviews start from the same files every time. Keep them
small: each run copies them and commits the result.

`scripts/live_sessions.py` uses them the same way. Its hidden acceptance tests
live in `../benchmark_acceptance/`, outside the fixtures, so agents never see
them; they run against a copy of the final checkout.
