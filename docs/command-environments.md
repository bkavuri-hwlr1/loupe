# Command environments

Commands, hooks and configured checks use a minimal child environment by
default. Enable terminal forwarding in Loupe's shared configuration, then restart
the daemon for the selected profile before opening a chat:

```console
loupe config set agent.command_environment session
loupe daemon restart
loupe chat --repo .
```

The setting is shared across profiles. Other running daemons keep their loaded
policy until restarted; use `loupe --profile NAME daemon restart` to select one.
The alternative is `minimal`, which preserves the default behavior. Session
forwarding lets project commands read arbitrary variables from the terminal
that opens or resumes the chat, including project flags, `AWS_PROFILE`, and
connection settings such as `DATABASE_URL`. Existing command approval,
filesystem and network policies still apply.

## What reaches a child

The opening or resuming CLI sends the terminal map over Loupe's owner-only
socket. The daemon retains it only in memory for that session. Separate
sessions use their own maps; commands use the minimal child environment plus
their session's forwarded values and the check's explicit `env` overrides.

Loupe removes `LOUPE_*` and `LLM_COORD_*`, known provider credential variables,
and credential names registered when a provider is configured. Private
`HOME`, `TMPDIR`, cache paths, and related temporary-directory variables remain
owned by the command/check snapshot, including when a check has an `env` table.
Hooks receive their temporary `LOUPE_HOOK_INPUT` path from the trusted launcher.

Values from secret-like names, including password, token, API/private/access
key names and common database URL names, join the existing exact-value output
screening. Multiline values also contribute each nonempty logical line, so
printing them line by line keeps them private. Original and rewritten values
are screened before command/check output, hook results, launch diagnostics or
command metadata reach the conversation, events or stored check output. Recognized secret signatures retain their existing
screening. Ordinary command output continues to be recorded after screening.

The forwarded map remains in memory and is never serialized into Loupe's
database, logs, events or checkpoints. Closing a session drops its map. A child
already running keeps its frozen screening material until it stops.

## Inspect and reconnect

```console
loupe --json session show SESSION_ID
loupe session resume SESSION_ID
```

`command_environment` reports the policy, state, included/removed/screened
variable names, sandbox override names and rewrite decisions. It never shows
values. Each rewritten path-list component is identified by variable name and
component index.

After a daemon restart, session environments are absent. A resumable task
reports `awaiting_session_environment` and keeps its checkpoint until its
originating client reconnects; unrelated ready sessions can continue. Resume
from the intended terminal, or use `loupe chat --resume SESSION_ID`. Read/status
inspection remains available while waiting. Cancellation and session close
settle a waiting task without starting its commands.

A changed forwarded map invalidates successful checks for that session.
A check finishing during reconnect cannot restore evidence from the earlier
map. A reconnect with the same retained map leaves its evidence valid.

## Isolated paths and retained apply

Absolute path values and path-list components inside the selected primary
checkout are rewritten only when an exact hydrated equivalent exists in the
private workspace and the command/check snapshot. Roots such as `PWD` can map
to the private source root. A missing, unhydrated or escaping equivalent refuses
the launch with variable-name-only diagnostics; adjust the terminal and
reconnect. Unrelated external paths keep their original meaning.

Held proposals use their retained runtime generation for runtime path rewrites,
even if the current private runtime has since changed or disappeared. Their
source and `PWD` paths remain bound to the private source workspace.

An open session's registered environment remains authoritative when applying
its proposal. If the environment is absent, or the originating session was
closed, `loupe task apply TASK_ID` and `/apply TASK_ID` can supply the applying
terminal's map under the explicit `session` policy. Loupe binds that temporary
map only to this proposal's fresh checks and retained runtime, then wipes it.
The session stays closed. An API client that omits the required map receives
`awaiting_session_environment` rather than a different terminal's environment.
