"""Wrap model-chosen commands in an operating-system sandbox.

macOS uses Seatbelt (``sandbox-exec``) with a deny-by-default profile, and
Linux uses bubblewrap. Under either, a command:

- has no network access, except Unix sockets inside its writable paths;
- can write only to its writable paths (a disposable snapshot and home);
- can read the rest of the filesystem except protected paths, which hold
  credentials, Loupe's own state, and the real checkout.

Reads outside protected paths remain possible, so this is a weaker guarantee
than a full read allowlist; callers also screen command output for recognized
secrets. Without a working sandbox the capability is unavailable: a
model-chosen command never runs unsandboxed.
"""

from __future__ import annotations

import os
import shutil
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
    ".npmrc",
    ".pypirc",
    ".password-store",
    ".codex",
    ".claude",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    "Library/Keychains",
    "Library/Cookies",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Firefox",
    "Library/Safari",
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
    return [_SANDBOX_EXEC, "-p", profile, *parameters, "--", *argv]


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
    for path in policy.writable:
        command.extend(("--bind", str(path), str(path)))
    command.extend(("--chdir", str(cwd), "--", *argv))
    return command


__all__ = [
    "BUBBLEWRAP",
    "HOME_SECRET_PATHS",
    "SEATBELT",
    "SandboxPolicy",
    "available_sandbox",
    "protected_home_paths",
    "sandbox_kind",
    "wrap",
]
