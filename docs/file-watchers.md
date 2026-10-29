# Native file watchers

Loupe watches checkouts that active sessions use, so scans look only where
something may have changed. Watchers are wake-ups, not authority (workspace
plan §17.1). A scanner still stats, hashes and compares exact identities, and
every publication still checks its exact bases on disk.

## What watchers narrow

- **Isolated safe points.** Before model requests, checks, commands and
  publication, an isolated session receives the primary checkout's current
  source. With a watcher, the safe point reuses its previous complete capture
  and rereads only paths the watcher names, plus paths a crossing save deferred
  last time. Git lists the source set again only when a path may have appeared,
  vanished or been respelled, a directory changed, or the Git index changed.
- **Outside-edit scans.** Shared sessions get notices when files they read
  change outside Loupe. A narrowed scan checks reads the watcher names, reads
  whose identity was never verified, and reads an earlier scan could not settle.
  Model boundaries use the daemon's scanner, so its cursors persist between
  turns.
- **Background scans.** A watcher wakes the daemon's existing background scan,
  still at most once every two seconds. No new daemon loop is added.

## Platforms

| Platform | Mechanism | Supported filesystems |
| --- | --- | --- |
| macOS | FSEvents, one stream per watched checkout | APFS, HFS+ |
| Linux | inotify, one instance for the daemon | ext2/3/4, XFS, Btrfs, tmpfs, overlayfs |

Both are reached through `ctypes`; there is no new dependency. Network and other
filesystems, other platforms, and a watcher that cannot start keep today's
periodic full scans for that checkout. Loupe tries again after five minutes.

Before a narrowed answer, Loupe confirms delivery. On macOS it creates and
removes a cookie file in the profile's private runtime directory and waits until
FSEvents reports it; events are delivered in order, so earlier changes have
arrived too. On Linux, inotify queues an event before the changing system call
returns, so draining the queue is enough. An unconfirmed delivery gives a full
scan.

## When a scan is full

A scan is full, exactly as without watchers, after:

- the first scan of a checkout, including after daemon downtime;
- a kernel queue overflow (inotify `IN_Q_OVERFLOW`) or dropped FSEvents
  (`MustScanSubDirs`, `UserDropped`, `KernelDropped`, wrapped event IDs);
- a replaced, removed or remounted checkout directory;
- a renamed directory on Linux, whose watches still carry their old names;
- a change to `.gitignore`, `.git/info/exclude`, Git config, or the global Git
  config and excludes files outside the checkout;
- more than 8,192 distinct changed paths since the cursor;
- an event that cannot be mapped to a path;
- at least five minutes since that cursor's last full scan.

Lost events also notify sessions. Loupe adds a `checkout.resync_required`
event with code `CONTEXT_RESYNC_REQUIRED` to every active session on the
checkout. The terminal shows "File watching lost changes here; Loupe rescanned
the checkout." The model's next coordination packet carries a
`context_resync` entry asking it to re-read files before relying on earlier
reads. Changes found by the full scan produce ordinary outside-edit notices.

Publication does not pause during a resync. The workspace plan's pause
(§17.6) protected external batches built from scans. Loupe publishes only after
checking exact bases on disk (C3, C11), so a missed wake-up can delay a notice
or a safe-point update. It cannot let a publication overwrite an outside edit;
that becomes a conflict.

## Git activity

Events inside `.git` never become source paths. The index (`index`,
`index.lock`, split-index files) marks the source set for relisting. Config and
`info/exclude` force a full scan. Objects, refs, logs and Loupe's
`llm-coord-stage` publication staging are ignored. C11's Git reconciliation
still runs at every scan point, so checkouts, rebases and commits are detected
exactly as before.

Untracked ignored directories, such as `node_modules/` or `build/`, are not
watched. Git reports a directory as ignored only when nothing inside it is
tracked. A read below one of them is always checked.

## Setting

```toml
[workspace]
watchers = "auto"   # or "off"
```

`"off"` starts no watcher. Every scan is then a full periodic one, with the
same scanner code as before watchers.

## Limits watchers do not change

- Outside-edit scans cover each session's latest 1,000 read paths.
- Isolated hydration admission refuses checkouts its 20,000-entry inventory
  budget cannot cover, and isolated capture refuses more than 20,000 source
  paths, so a 100,000-file checkout cannot open an isolated session.
- Global Git config is followed through its main files; settings reached only
  through `include` directives are caught by the five-minute verification.
- inotify needs a watch per directory. A checkout with more than 50,000
  unignored directories, or a system out of `fs.inotify.max_user_watches`, keeps
  periodic scans.

Measurements are in
[the C12 experiment](experiments/file-watchers-c12-2026-10-28.md).
