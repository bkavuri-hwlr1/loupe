# 0.1.0a2 upgrade profile

This fixture was generated with the unmodified `v0.1.0a2` source at
`a4b37a95921505d792180468eef89e231c8843dc`, using its real daemon service,
scripted-provider task runner, workflow and batch publisher. Its control schema
ends at migration 19; no current application code generates its schema or data.
It contains two shared sessions, two completed model executions, one published
batch (`value = 1` → `2`), and a held normal-mode proposal (`2` → `3`).
No API credentials, user profiles or billed providers are used.

`repository.bundle` contains the original local Git baseline. `candidates/`
contains the old publisher's ordinary candidate objects. Machine paths in TEXT
columns are replaced with `/loupe-upgrade-fixture`; the upgrade test relocates
only that prefix and preserves the migration receipts, opaque IDs and content.
`manifest.json` identifies the source commit and checksums the database.

To regenerate, create a clean local checkout of the tag, then run with a Python
3.12–3.14 development environment that has the all-extras dependencies:

```sh
git clone --branch v0.1.0a2 --single-branch /path/to/loupe /tmp/loupe-0.1.0a2
python tests/fixtures/upgrade_0_1_0a2/generate.py /tmp/loupe-0.1.0a2
```

The generator refuses a changed or wrong source revision. Random durable IDs and
timestamps make regenerated database bytes differ; the manifest is refreshed.
The disposable repository uses only `example.invalid` identities and no network.
