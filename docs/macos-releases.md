# macOS releases

The public install command is `brew install bkavuri-hwlr1/tap/loupe`.
Development happens in the public `bkavuri-hwlr1/loupe` repository, which holds
the only release credential. The public `bkavuri-hwlr1/homebrew-tap` repository
contains the formula, installation guide, release bundles, and Homebrew bottles.
Its Actions workflows never have credentials for the development repository.
Loupe was previously published from `magnifiosearchengine/tap`, which is no
longer associated with this project; `packaging/README.md` explains how users
move to the new tap.

## Release pipeline

1. Update both `project.version` in `pyproject.toml` and `__version__` in
   `src/llm_cli/__init__.py`; refresh `uv.lock` with `uv lock` and commit. Versions
   use `X.Y.Z` or `X.Y.ZaN`, `X.Y.ZbN`, or `X.Y.ZrcN`.
2. Push a matching `vX.Y.Z` tag on the reviewed commit. The release
   workflow runs the full CI matrix and builds on native Apple Silicon and
   Intel macOS 15 runners using Python 3.14.
3. The builder uses `uv build --wheel --no-sources` and verifies an allowlist of
   application wheel contents. Package metadata uses `packaging/README.md`,
   not the internal development README. Runtime dependencies, including both
   provider extras, come from `uv export --locked`; pip downloads wheels only,
   verifying the lockfile hashes. Missing wheels fail the release.
4. Each archive contains only those wheels, public installation documentation,
   a checksummed manifest, and the installed-package smoke test. An isolated
   install outside the checkout must pass before handing off either archive.
5. A repository-scoped SSH deploy key pushes the public inputs to a temporary
   `release/vX.Y.Z` branch in the tap. The handoff includes only the bundles,
   formula, installation guide, and publish workflow: no Git history,
   development documentation, credentials, or developer paths.
6. The public workflow uploads candidate downloads as a prerelease, then builds,
   tests, bottles, uninstalls, reinstalls, and tests on both Mac architectures.
   After both pass it uploads bottles, merges their checksums, and updates the
   main formula. The candidate branch is deleted; archives are not added to the
   main branch's history. Alpha/beta/RC releases retain their prerelease label.

The formula installs the bundled wheels into its own Python 3.14 virtualenv with
network dependency resolution disabled. Only the four application commands are
linked globally. No login credentials are needed to install or run tests.
No `brew services` registration is installed; the existing CLI starts the daemon.

## Initial tap setup

Create a public `bkavuri-hwlr1/homebrew-tap` repository. Seed its `main`
branch with `packaging/README.md`, an empty `Formula/` directory (with `.gitkeep`),
and `packaging/homebrew/publish.yml` at `.github/workflows/publish.yml`.

Generate a dedicated Ed25519 SSH key. Add its public half to the tap as a deploy
key with write access. Store its private half as the Actions secret
`HOMEBREW_TAP_DEPLOY_KEY` in the **development repository only**. Delete the
local private key after setup. This key can write only the public tap, not other
repositories. The public workflow uses its own repository's `GITHUB_TOKEN` to
upload release assets and advance its main branch. Allow Actions contents-write
permissions in the public tap. Never copy a maintainer's general GitHub token
into either repository.

```sh
ssh-keygen -t ed25519 -N "" -C "loupe homebrew-tap deploy key" -f loupe-tap-key
gh repo deploy-key add loupe-tap-key.pub --repo bkavuri-hwlr1/homebrew-tap \
  --allow-write --title "Loupe releases"
gh secret set HOMEBREW_TAP_DEPLOY_KEY --repo bkavuri-hwlr1/loupe < loupe-tap-key
rm loupe-tap-key loupe-tap-key.pub
gh api --method PUT repos/bkavuri-hwlr1/homebrew-tap/actions/permissions/workflow \
  -f default_workflow_permissions=write
```

## Local verification

On an Apple Silicon or Intel Mac:

```sh
uv sync --locked --all-groups --all-extras
uv run --locked python scripts/macos_release.py build --tag v0.1.0a0 --output dist/macos
```

Replace the tag with the current package version. Building is native to the Mac
architecture; both bundles are required by the `prepare` subcommand. To inspect
the candidate tap without publishing, pass a separate tap checkout directory:

```sh
uv run --locked python scripts/macos_release.py prepare \
  --tag v0.1.0a0 --assets dist/macos --tap /path/to/tap-checkout
```

The tap's publish workflow runs `brew style` on the formula before installing
it. CI's `formula` job runs the same check on every pull request, against the
template rendered for the current version with placeholder checksums. To run
it locally (the first run installs Homebrew's style gems):

```sh
brew tap-new --no-git loupe/local
uv run --locked python scripts/macos_release.py formula \
  --output "$(brew --repo loupe/local)/Formula/loupe.rb"
brew style loupe/local/loupe
brew untap loupe/local
```

On a Mac, `brew test` runs the formula's test in Homebrew's sandbox, which
blocks the Unix socket Loupe's background service listens on. The formula's test
therefore sets `LOUPE_SMOKE_NO_SERVICE=1` and checks everything that needs no
service: the version, help, demo, an unconfigured chat, and the provider imports.
The tap's publish workflow runs the full smoke test outside the sandbox, after
`brew test`, so the service checks below still run for every release.

The full smoke test (`smoke_test.py` without that variable) exercises version/help, all four
entry points, the unconfigured conversation, provider imports, doctor, daemon
start/restart/stop, and profile preservation. It uses temporary HOME/state paths,
removes account environment variables, and runs outside the source checkout.

Before a real upgrade, finish tasks and stop every profile's daemon. Upgrade
with `brew update && brew upgrade loupe`; the next task starts the new daemon.
Uninstall retains profiles and credentials. The installed interpreter is the
daemon interpreter, so removing an old Homebrew keg while its daemon is running
is unsupported.

## Failures and retries

- A failed release build publishes nothing. Re-run the failed workflow after
  resolving infrastructure failures; change code in a new version/tag.
- If the public bottle job fails, the previous formula remains available. Use
  Actions **Re-run failed jobs**, or dispatch the public workflow on the same
  `release/vX.Y.Z` branch. Repeating a release verifies existing asset bytes;
  released URLs are never overwritten with different contents.
- If the tap fails only because of the formula (its style check or install
  test), fix `packaging/homebrew/loupe.rb.in` here, and apply the same change to
  `Formula/loupe.rb` on the tap's `release/vX.Y.Z` branch. Pushing that branch
  reruns the publish workflow, which verifies the already-published bundles are
  unchanged, so no new version is needed. Any change to a bundle needs a new
  version. 0.1.0a1 was finished this way: `brew test` puts Homebrew's internal
  shims first on `PATH`, so its test now puts the real `git` ahead of them.
- If the handoff already pushed its branch, retry the public workflow rather
  than overwriting that branch. Failed or partially published releases retain
  their candidate branch for diagnosis.
- Roll back a faulty formula by reverting its release commit in the tap. Users
  explicitly reinstall the restored formula; do not overwrite published assets
  or move an existing version tag.
- Review bundle contents and keep bundled dependency license notices intact.
  This packaging work does not change the project's license status.
