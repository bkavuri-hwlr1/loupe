# In-place upgrade check

CI opens a used profile created by a previous release and runs new tasks in
its existing shared and isolated sessions. Each release runs with its own
locked dependencies. CI runs this check on Linux for pull requests and main,
and on both Linux and Apple Silicon for release tags before the tap handoff.

Unlike the [0.1.0a2 fixture](../upgrade_0_1_0a2/README.md), this check builds
a fresh profile on each machine. From 0.1.0a3 a profile records the checkout's
physical identity and has an ownership marker outside the database. Both
versions open the same profile, checkout and authority root on the same host.

## Run locally

From the candidate source checkout, with the [pinned release tags](releases.py)
available locally, run:

```sh
uv run --no-project --python 3.14 python tests/fixtures/upgrade_in_place/run_check.py
```

The default source is the newest listed release. To choose another listed
tag, pass it as the final argument. The runner verifies the tag against
its pinned commit, creates a temporary detached worktree and environments,
and removes them on exit. Dependency installation can access the network.
The profile scenario uses scripted providers and temporary profile directories.

The old release creates two shared sessions (one published change to
`a.py` and one held proposal) and an open isolated session with a published
change to `b.py`. It records the current control schema version. The candidate
prints one line per check and ends with `UPGRADE CHECK PASSED` after it:

- verifies the old schema and current migrations, foreign keys and integrity;
- reads the old sessions, task history, events and the held proposal's diff;
- verifies that startup preserved the legacy regime and checkout files;
- exports the proposal and both published batches as patch and bundle;
- applies the old proposal and runs a new task in the old shared session;
- resumes the old isolated session, observes the other session's changes and
  an outside edit, and publishes new work;
- closes the isolated session, verifies its worktree was removed and checks
  that Git `HEAD` remains at the original commit.

After publishing a release, update the table as described in the
[release guide](../../../docs/macos-releases.md#release-pipeline).
