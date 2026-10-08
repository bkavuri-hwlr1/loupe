"""Wrap model-chosen commands in an operating-system sandbox.

macOS uses Seatbelt (``sandbox-exec``) with a deny-by-default profile, and
Linux uses bubblewrap. Under either, a command:

- has no network access, except Unix sockets inside its writable paths;
- can write only to its writable paths (a disposable snapshot and home);
- can read the rest of the filesystem except protected paths, which hold
  credentials, Loupe's own state, and the real checkout;
- leaves no process running after it ends, even one that left its process
  group.

Reads outside protected paths remain possible, so this is a weaker guarantee
than a full read allowlist; callers also screen command output for recognized
secrets. Without a working sandbox the capability is unavailable: a
model-chosen command never runs unsandboxed.

Seatbelt decides each socket connection by path. Bubblewrap cannot: a
read-only mount does not stop a Unix-socket connect, so host sockets are
hidden instead. Private ``/tmp`` and ``/run`` hide the usual ones, and every
other socket bound when the command starts is covered by an empty file.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SEATBELT = "seatbelt"
BUBBLEWRAP = "bubblewrap"
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Bound sockets in this network namespace, with the paths they were bound at.
_NET_UNIX = Path("/proc/net/unix")
# Directories bubblewrap replaces with private copies; host sockets there are
# already out of reach.
_PRIVATE_DIRECTORIES = (Path("/tmp"), Path("/run"), Path("/dev"), Path("/proc"))

# Credential and secret stores under the home directory. Reading these is
# never needed to build or test a project.
HOME_SECRET_PATHS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".config/gcloud",
    ".config/gh",
    ".kube",
    ".docker",
    ".netrc",
    ".git-credentials",
    # Git's credential cache and 1Password's SSH agent listen on sockets here.
    ".git-credential-cache",
    ".cache/git/credential",
    ".1password",
    ".npmrc",
    ".pypirc",
    ".password-store",
    ".codex",
    ".claude",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    # macOS keychains and browser profiles.
    "Library/Keychains",
    "Library/Cookies",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Vivaldi",
    "Library/Application Support/Arc",
    "Library/Application Support/Firefox",
    "Library/Safari",
    "Library/Containers/com.apple.Safari",
    # Linux keyrings and browser profiles, including Snap and Flatpak copies.
    ".local/share/keyrings",
    ".local/share/kwalletd",
    ".mozilla",
    ".config/google-chrome",
    ".config/google-chrome-beta",
    ".config/google-chrome-unstable",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".config/opera",
    "snap/firefox",
    "snap/chromium",
    ".var/app/org.mozilla.firefox",
    ".var/app/org.chromium.Chromium",
    ".var/app/com.google.Chrome",
    ".var/app/com.brave.Browser",
    ".var/app/com.microsoft.Edge",
)

_SEATBELT_BASE = """\
(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow ipc-posix-sem)
(allow ipc-posix-shm)
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow file-read*)
(allow file-write-data (literal "/dev/null"))
"""

# Seatbelt has no process namespace, so a process that leaves the command's
# process group would outlive it. The command therefore starts under this
# shell. Given descriptors 3 and 4 by the supervisor (see check_process.py),
# the shell first starts a reaper that waits for 4 to close and then signals
# every process it may. The profile confines signals to this sandbox, so only
# the command's processes are killed. Only the reaper keeps 3; its closing
# tells the supervisor it is done. The shell catches termination signals only
# to outlive the command, keeping the supervisor's grace period. The command
# gets neither descriptor and default signal handling, and starts through
# exec so that a shell builtin never stands in for the program it names.
_SEATBELT_LAUNCHER = """\
trap : HUP INT TERM
if { true <&4; } 2>/dev/null; then
  (trap '' HUP INT TERM; exec </dev/null >/dev/null 2>&1
   read -r ignored <&4; kill -KILL -1) &
  exec 3>&- 4<&-
fi
(exec "$@")
"""

_probe_lock = threading.Lock()
_probe_result: dict[str, str | None] = {}


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """What a sandboxed command may write, and what it may not read."""

    writable: tuple[Path, ...]
    protected: tuple[Path, ...] = ()
    # Read-only exceptions inside protected paths, such as dependency folders.
    readable: tuple[Path, ...] = ()


def sandbox_kind() -> str | None:
    """The sandbox this platform supports, before checking that it works."""

    if sys.platform == "darwin" and os.access(_SANDBOX_EXEC, os.X_OK):
        return SEATBELT
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return BUBBLEWRAP
    return None


def available_sandbox() -> str | None:
    """Return a sandbox that actually starts processes here, or None.

    Kernels can refuse unprivileged namespaces even when bubblewrap is
    installed, so a trivial command must succeed before the capability is
    offered. The answer is cached for the life of the process.
    """

    kind = sandbox_kind()
    if kind is None:
        return None
    with _probe_lock:
        if kind not in _probe_result:
            _probe_result[kind] = kind if _probe(kind) else None
        return _probe_result[kind]


def wrap(
    kind: str, policy: SandboxPolicy, argv: Sequence[str], *, cwd: Path
) -> list[str]:
    """Return argv that runs ``argv`` in ``cwd`` inside the sandbox."""

    if kind == SEATBELT:
        return _seatbelt(policy, argv)
    if kind == BUBBLEWRAP:
        return _bubblewrap(policy, argv, cwd)
    raise ValueError(f"unsupported sandbox {kind!r}")


def uses_reaper(kind: str) -> bool:
    """Whether wrapped commands expect the supervisor's reaper descriptors."""

    return kind == SEATBELT


def protected_home_paths(home: Path) -> tuple[Path, ...]:
    return tuple(home / relative for relative in HOME_SECRET_PATHS)


def _probe(kind: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="loupe-sandbox-probe-") as directory:
        root = Path(directory).resolve()
        policy = SandboxPolicy(writable=(root,))
        try:
            completed = subprocess.run(
                wrap(kind, policy, ["/bin/sh", "-c", "exit 0"], cwd=root),
                cwd=root,
                env={"PATH": os.defpath},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0


def _seatbelt(policy: SandboxPolicy, argv: Sequence[str]) -> list[str]:
    # Paths travel as -D parameters, so no path text is ever parsed as profile
    # syntax. Seatbelt applies the last matching rule: denials precede the
    # exceptions and writable paths that may sit inside them.
    parameters: list[str] = []
    rules: list[str] = []

    def filters(prefix: str, paths: Sequence[Path]) -> str:
        names = []
        for index, path in enumerate(paths):
            name = f"{prefix}_{index}"
            parameters.extend(("-D", f"{name}={path}"))
            names.append(f'(subpath (param "{name}"))')
        return " ".join(names)

    if policy.protected:
        rules.append(f"(deny file-read* {filters('PROTECTED', policy.protected)})")
    if policy.readable:
        rules.append(f"(allow file-read* {filters('READABLE', policy.readable)})")
    if policy.writable:
        writable = filters("WRITABLE", policy.writable)
        rules.append(f"(allow file-read* file-write* {writable})")
        rules.append(
            f"(allow network-bind network-inbound (local unix-socket {writable}))"
        )
        rules.append(f"(allow network-outbound (remote unix-socket {writable}))")
    profile = _SEATBELT_BASE + "\n".join(rules) + "\n"
    return [
        _SANDBOX_EXEC,
        "-p",
        profile,
        *parameters,
        "--",
        "/bin/sh",
        "-c",
        _SEATBELT_LAUNCHER,
        "loupe-sandbox",
        *argv,
    ]


def _bubblewrap(policy: SandboxPolicy, argv: Sequence[str], cwd: Path) -> list[str]:
    command = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        # Private /tmp and /run hide host sockets such as SSH agents and the
        # Docker daemon. A read-only mount does not stop a Unix-socket connect.
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
    ]
    # --die-with-parent and the private process namespace already stop
    # everything the command started when bubblewrap is killed.
    # A protected directory becomes an empty tmpfs. If a writable path lies
    # inside it, only the directories leading to that mount point appear.
    for path in policy.protected:
        if path.is_dir():
            command.extend(("--tmpfs", str(path)))
        elif path.exists():
            command.extend(("--ro-bind", "/dev/null", str(path)))
    # Later mounts sit on top of earlier ones, so exceptions follow denials.
    for path in policy.readable:
        if path.exists():
            command.extend(("--ro-bind", str(path), str(path)))
    # Any other host socket the command could see is covered by an empty file.
    for path in _host_sockets():
        if _visible(path, policy):
            command.extend(("--ro-bind", "/dev/null", str(path)))
    for path in policy.writable:
        command.extend(("--bind", str(path), str(path)))
    command.extend(("--chdir", str(cwd), "--", *argv))
    return command


def _host_sockets() -> list[Path]:
    """Sockets bound at absolute paths in this network namespace, resolved.

    Sockets bound later, bound in another network namespace, or bound by a
    relative path are not listed.
    """

    try:
        table = _NET_UNIX.read_bytes()
    except OSError:
        return []
    found: set[Path] = set()
    for line in table.splitlines()[1:]:
        # Num RefCount Protocol Flags Type St Inode [Path], where a path may
        # contain spaces and an abstract name starts with "@".
        fields = line.split(maxsplit=7)
        if len(fields) < 8 or not fields[7].startswith(b"/"):
            continue
        path = Path(os.path.realpath(os.fsdecode(fields[7])))
        try:
            if stat.S_ISSOCK(path.lstat().st_mode):
                found.add(path)
        except OSError:
            # An unreachable path is unreachable from the sandbox too.
            continue
    return sorted(found)


def _visible(path: Path, policy: SandboxPolicy) -> bool:
    """Whether a host path shows through the sandbox's mounts at its location."""

    def under(roots: Sequence[Path]) -> bool:
        return any(path.is_relative_to(root) for root in roots)

    if under(_PRIVATE_DIRECTORIES) or under(policy.writable):
        return False
    return under(policy.readable) or not under(policy.protected)


__all__ = [
    "BUBBLEWRAP",
    "HOME_SECRET_PATHS",
    "SEATBELT",
    "SandboxPolicy",
    "available_sandbox",
    "protected_home_paths",
    "sandbox_kind",
    "uses_reaper",
    "wrap",
]
