# Magnifio

Magnifio is a coding agent for your terminal. Install on macOS 15 or newer
(Apple Silicon or Intel) with [Homebrew](https://brew.sh):

```sh
brew install magnifiosearchengine/tap/magnifio
magnifio
```

Type `/login` to connect a ChatGPT subscription, an OpenAI API key, or an
Anthropic API key. Both provider SDKs are included. Use `/model` to choose a
model, `/help` for commands, or `magnifio demo` to preview the interface without
an account. Run Magnifio in your Git project or use `/cd PATH` inside the app.

## Update

Finish active tasks and exit open Magnifio terminals. Stop the daemon for each
profile you use, then upgrade:

```sh
magnifio daemon stop
brew update
brew upgrade magnifio
```

For another profile, use `magnifio --profile NAME daemon stop`. A message saying
the daemon is unavailable means it is already stopped. Your next task starts
the updated daemon. Profiles, logins, and saved sessions are preserved.

## Remove

After finishing active tasks, exit Magnifio and stop each profile's daemon:

```sh
magnifio daemon stop
brew uninstall magnifio
```

Uninstalling keeps your local profiles, credentials, and sessions.

## Move from a source or uv installation

Finish active tasks, exit Magnifio, and stop each profile's daemon, then run:

```sh
uv tool uninstall llm-coord
brew install magnifiosearchengine/tap/magnifio
hash -r
command -v magnifio
magnifio --version
```

The command should resolve inside your Homebrew prefix. A checkout-only
`uv run magnifio` installation does not require `uv tool uninstall`. Remove any
custom shell aliases that still run the checkout. Existing state is reused.

## Troubleshooting

Run `magnifio doctor` to check Python, Git, SQLite, and local state. Report
installation problems at
https://github.com/MagnifioSearchEngine/homebrew-tap/issues.

The public distribution includes readable Python application code. Development
history and internal project documents are maintained separately. No project
license has been selected; availability of a download does not itself grant
redistribution rights. Bundled dependencies retain their own license notices.
