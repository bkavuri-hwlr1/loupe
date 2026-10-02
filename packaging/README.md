# Loupe

Loupe is a coding agent for your terminal. Install on macOS 15 or newer
(Apple Silicon or Intel) with [Homebrew](https://brew.sh):

```sh
brew install magnifiosearchengine/tap/loupe
loupe
```

Type `/login` to connect a ChatGPT subscription, an OpenAI API key, or an
Anthropic API key. Both provider SDKs are included. Use `/model` to choose a
model, `/help` for commands, or `loupe demo` to preview the interface without
an account. Run Loupe in your Git project or use `/cd PATH` inside the app.

## Update

Finish active tasks and exit open Loupe terminals. Stop the daemon for each
profile you use, then upgrade:

```sh
loupe daemon stop
brew update
brew upgrade loupe
```

For another profile, use `loupe --profile NAME daemon stop`. A message saying
the daemon is unavailable means it is already stopped. Your next task starts
the updated daemon. Profiles, logins, and saved sessions are preserved.

## Remove

After finishing active tasks, exit Loupe and stop each profile's daemon:

```sh
loupe daemon stop
brew uninstall loupe
```

Uninstalling keeps your local profiles, credentials, and sessions.

## Move from the Magnifio Homebrew formula

Finish active tasks and exit open Magnifio terminals. Stop the daemon for each
profile before removing the old formula:

```sh
magnifio daemon stop
brew uninstall magnifio
brew install magnifiosearchengine/tap/loupe
hash -r
loupe --version
```

For another profile, run `magnifio --profile NAME daemon stop` before uninstalling.
Uninstalling the old formula preserves your profiles, logins, and saved sessions.
Remove it before installing Loupe because both formulas link the `llm-coord`
compatibility commands. Use `loupe` for new terminals and update shell aliases
that invoke `magnifio`.

## Move from a source or uv installation

Finish active tasks and exit open terminals. Stop each profile's daemon with
`llm-coord --profile NAME daemon stop`. For a checkout-only installation, run
`uv run llm-coord --profile NAME daemon stop` from that checkout instead. Then run:

```sh
uv tool uninstall llm-coord
brew install magnifiosearchengine/tap/loupe
hash -r
command -v loupe
loupe --version
```

The command should resolve inside your Homebrew prefix. A checkout-only
`uv run loupe` installation does not require `uv tool uninstall`. Remove any
custom shell aliases that still run the checkout. Existing state is reused.

## Troubleshooting

Run `loupe doctor` to check Python, Git, SQLite, and local state. Report
installation problems at
https://github.com/MagnifioSearchEngine/homebrew-tap/issues.

The public distribution includes readable Python application code. Development
history and internal project documents are maintained separately. No project
license has been selected; availability of a download does not itself grant
redistribution rights. Bundled dependencies retain their own license notices.
