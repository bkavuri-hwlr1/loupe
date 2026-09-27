# macOS releases

The public install command is `brew install magnifiosearchengine/tap/magnifio`.
Development remains in the private `MagnifioSearchEngine/loupe` repository.
This public repository is a source snapshot with its own fresh Git history.
The public `MagnifioSearchEngine/homebrew-tap` repository contains the formula,
installation guide, release bundles, and Homebrew bottles. Its Actions workflows
never have credentials for the private development repository.

## Release pipeline

1. Update both `project.version` in `pyproject.toml` and `__version__` in
   `src/llm_cli/__init__.py`; refresh `uv.lock` with `uv lock` and commit. Versions
   use `X.Y.Z` or `X.Y.ZaN`, `X.Y.ZbN`, or `X.Y.ZrcN`.
2. Push a matching `vX.Y.Z` tag on the reviewed commit. The private release
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
   `release/vX.Y.Z` branch in the tap. The handoff includes no private Git history,
   internal documentation, credentials, or developer paths. These inputs become
   publicly readable at this point, including the Python application code.
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

Create a public `MagnifioSearchEngine/homebrew-tap` repository. Seed its `main`
branch with `packaging/README.md`, an empty `Formula/` directory (with `.gitkeep`),
and `packaging/homebrew/publish.yml` at `.github/workflows/publish.yml`.

Generate a dedicated Ed25519 SSH key. Add its public half to the tap as a deploy
key with write access. Store its private half as the Actions secret
`HOMEBREW_TAP_DEPLOY_KEY` in the **private development repository only**. Delete the local
private key after setup. This key can write only the public tap, not other
repositories. The public workflow uses its own repository's `GITHUB_TOKEN` to
upload release assets and advance its main branch. Allow Actions contents-write
permissions in the public tap. Never copy a maintainer's general GitHub token
into either repository.

Keep the private development repository and release credentials separate from this
public snapshot. Publish source snapshots with fresh Git history.

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

`brew test magnifiosearchengine/tap/magnifio` exercises version/help, all four
entry points, the unconfigured conversation, provider imports, doctor, daemon
start/restart/stop, and profile preservation. It uses temporary HOME/state paths,
removes account environment variables, and runs outside the source checkout.

Before a real upgrade, finish tasks and stop every profile's daemon. Upgrade
with `brew update && brew upgrade magnifio`; the next task starts the new daemon.
Uninstall retains profiles and credentials. The installed interpreter is the
daemon interpreter, so removing an old Homebrew keg while its daemon is running
is unsupported.

## Failures and retries

- A failed private build publishes nothing. Re-run the failed workflow after
  resolving infrastructure failures; change code in a new version/tag.
- If the public bottle job fails, the previous formula remains available. Use
  Actions **Re-run failed jobs**, or dispatch the public workflow on the same
  `release/vX.Y.Z` branch. Repeating a release verifies existing asset bytes;
  released URLs are never overwritten with different contents.
- If the handoff already pushed its branch, retry the public workflow rather
  than overwriting that branch. Failed or partially published releases retain
  their candidate branch for diagnosis.
- Roll back a faulty formula by reverting its release commit in the tap. Users
  explicitly reinstall the restored formula; do not overwrite published assets
  or move an existing version tag.
- Review bundle contents and keep bundled dependency license notices intact.
  This packaging work does not change the project's license status.
