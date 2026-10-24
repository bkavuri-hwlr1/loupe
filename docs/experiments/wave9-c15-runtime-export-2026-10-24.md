# Wave 9 C15 runtime export verification

The measured product/test commit is `70a72e740227e42b62051a84601e6317e0bddd6a`,
based on `codex/wave9-base`. The later checkpoint commit changes documentation
only. No schema migration is needed.

`loupe workspace runtime export PATH --session ID --to DEST` previews type,
bytes, entry count and sensitivity. Copying requires `--yes`, plus
`--allow-secret` for sensitive path names or recognized credential contents.
Screening reads complete bounded files locally, including late and split-read
signatures, and repeats on every invocation.

Exports use no-follow descriptors, hydration budgets, complete source receipts,
owner-only output permissions and atomic no-replace publication. Existing
destinations are preserved. Checkout destinations additionally require
`--into-checkout`, must be ignored and cannot overlap tracked source. Physical
ancestor identities and conservative spelling aliases protect all shared
checkouts and coordinator storage.

Copies into a checkout stage in the source session's verified private Git
directory on the same filesystem. Interrupted private staging is removed during
owned workspace retirement. Large copies hold runtime activity and physical
ownership pins without holding the daemon or publication barrier. Disposition
events use `session.runtime_exported`; only fixed metadata is stored, and its
payload is excluded from model coordination packets.

## Final checks

- Full correctness with coverage, excluding scale markers: **3,543 passed,
  1 expected filesystem skip, 2 deselected**, in 1,578.43 seconds. Aggregate
  coverage is **88%**; the new export module is **90%**.
- Separate index scale, without coverage or changed budgets: **1 passed** in
  11.95 seconds. Dense baseline builds took 0.819 seconds for 1,000 files and
  7.264 seconds for 10,000 files; warmed single-publication work took
  44.45 milliseconds and 50.12 milliseconds respectively.
- Separate admission scale, without coverage or changed timeouts:
  **1 passed** in 3.20 seconds. Six clients retained the default five-second
  RPC timeout.
- Ruff across source/tests, mypy across 130 source files, formatter checks for
  the three new files, and `git diff --check`: clean.
- Focused security/CLI suite: **40 passed**. Actual disposable daemons verify
  non-signature and recognized-secret byte absence from SQLite/WAL, artifacts,
  logs and model packets. A paused staged copy remains invisible to real source
  capture and the next model task; abrupt daemon death, restart and close remove
  private staging and runtime generations without publishing the destination.
- Earlier interrupted runs with 208 and 217 passes are incomplete preliminary
  runs and are replaced by the final frozen-source result above.

## Measured SHA-256 identities

| File | SHA-256 |
| --- | --- |
| `src/llm_cli/cli/app.py` | `cfbf2e6bacb246fba9981999085db9c90cb3494d27cf0d7a577072a2d21126c0` |
| `src/llm_cli/daemon/service.py` | `7e4fa8d0ac2517a1da6c055bd87045d69d58afd1fa39d34488af4becdd22b98f` |
| `src/llm_cli/workspace/isolated.py` | `529ae3d53e1174e5f22f80b83f541f7746c7b78a40a4217ac1fad3a1813e4004` |
| `src/llm_cli/workspace/runtime_export.py` | `09cd59cbad741491e4b74b892019100e82df964910d0761f462cfab6299ecb90` |
| `src/llm_cli/workspace/runtime_refresh.py` | `392e5541c8ebf8139b8d8271dd3a997a6fa5d37515906db28c02eda68b5f9f51` |
| `tests/unit/test_runtime_export.py` | `281a39ec1a3dac9bd568f72e28f98bdd383579dc922cccca37f5321b0017cf90` |
| `tests/integration/test_runtime_export_cli.py` | `9f947267f47e13084c5ed58351ad929b8b6c7c19eafe20e52312d576cbab794f` |
