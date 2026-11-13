# 0.1.0a3 in-place upgrade check

A release check that a real 0.1.0a3 profile keeps working after upgrading. It
runs by hand before a release; it is not part of the test suite.

Unlike [`upgrade_0_1_0a2`](../upgrade_0_1_0a2/README.md), there is no stored
database. From 0.1.0a3 a profile is bound to its machine: it records each
checkout's physical identity, keeps an ownership marker under the physical
authority root outside the database, and protects those records with
triggers. A copied database would test a profile moved to another machine,
not an upgrade. So the old release builds the profile and the current code
opens the same files in place.

## Run it

Use a clean checkout of the old tag and a new, empty location for `ROOT`:

```sh
git clone --branch v0.1.0a3 --single-branch /path/to/loupe /tmp/loupe-0.1.0a3
uv run python tests/fixtures/upgrade_0_1_0a3/build_profile.py /tmp/loupe-0.1.0a3 ROOT
uv run python tests/fixtures/upgrade_0_1_0a3/check_upgrade.py ROOT
```

`build_profile.py` refuses a changed or wrong source revision and asserts that
the old release's code is the code that ran. It uses that release's daemon
service and scripted-provider test helpers to create:

- two shared sessions: one published change (`a.py`: `value = 1` → `2`) and
  one held proposal (`2` → `3`);
- one isolated session, left open, that published `b.py` from its private
  worktree.

`check_upgrade.py` starts the current daemon service on the same directories
and prints one line per step, ending with `UPGRADE CHECK PASSED`:

- migrations apply, and the database passes integrity and foreign-key checks;
- old sessions, tasks, events and the held proposal's diff are readable;
- the checkout stays in the legacy regime and its files are untouched;
- the old proposal and both old batches export as patch and bundle;
- the old proposal applies;
- the old shared session runs a new task;
- the old isolated session receives the other session's changes and an
  outside edit, runs a new task and publishes;
- closing it removes its private worktree, and Git `HEAD` never moves.

Everything stays under `ROOT`: the profile, the repository and the physical
authority root. No account directories, credentials, billed providers or
network are used. A `ROOT` can be checked once, because the check changes it;
build a new one to run again.

For a later release, copy this directory, point `SOURCE_COMMIT` and the
version assertion at the previous tag, and extend the steps for what changed.
