"""Shared source exclusions and bounded, conservative secret classification."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git, sanitized_git_environment

_PRIVATE_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "credentials",
        "credentials.json",
        "auth.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_PRIVATE_DIRECTORIES = frozenset({".ssh", ".aws", ".azure", ".gnupg"})
_EXAMPLE_SUFFIXES = (".example", ".sample", ".template")
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,255}\b"),
    re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}\b"),
)


def _private_path(relative: str) -> bool:
    for name in (part.casefold() for part in Path(relative).parts):
        if name in _PRIVATE_DIRECTORIES:
            return True
        if name.endswith(_EXAMPLE_SUFFIXES):
            continue
        if (
            name in _PRIVATE_NAMES
            or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
            or (name.startswith("service-account") and name.endswith(".json"))
        ):
            return True
    return False


def _user_excludes_file(root: Path, remaining: Callable[[], float] | None) -> str:
    # Read exactly one passive setting from the user's configuration. Do not
    # re-enable their hooks, filters, aliases or other config for tool operations.
    env = sanitized_git_environment()
    if "GIT_CONFIG_GLOBAL" in os.environ:
        env["GIT_CONFIG_GLOBAL"] = os.environ["GIT_CONFIG_GLOBAL"]
    else:
        env.pop("GIT_CONFIG_GLOBAL", None)
    try:
        result = subprocess.run(
            [
                "git",
                "config",
                "--global",
                "--includes",
                "--path",
                "--null",
                "--get",
                "core.excludesFile",
            ],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=min(10, remaining()) if remaining is not None else 10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if remaining is not None:
            remaining()
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "could not read user source exclusions"
        ) from exc
    if result.returncode == 0:
        return os.fsdecode(result.stdout.removesuffix(b"\x00"))
    if result.returncode != 1:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "could not read user source exclusions"
        )
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return str(Path(config_home) / "git" / "ignore")


def _ignored(
    root: Path,
    paths: list[str],
    *,
    options: list[str],
    remaining: Callable[[], float] | None,
) -> set[str]:
    result = run_git(
        root,
        [*options, "check-ignore", "--no-index", "-z", "--stdin"],
        check=False,
        input_data="\x00".join(paths) + "\x00",
        remaining=remaining,
    )
    if result.returncode not in {0, 1}:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "could not determine source exclusions"
        )
    assert isinstance(result.stdout, str)
    return set(filter(None, result.stdout.split("\x00")))


def excluded_paths(
    root: Path,
    relative_paths: list[str],
    *,
    remaining: Callable[[], float] | None = None,
) -> set[str]:
    if not relative_paths:
        return set()
    excluded = {path for path in relative_paths if _private_path(path)}
    for start in range(0, len(relative_paths), 200):
        excluded.update(
            _ignored(
                root,
                relative_paths[start : start + 200],
                options=[],
                remaining=remaining,
            )
        )
    user_excludes = _user_excludes_file(root, remaining)
    if not user_excludes:
        return excluded
    user_path = Path(user_excludes)
    if not user_path.is_absolute():
        user_path = root / user_path
    if not user_path.exists():
        return excluded
    if not user_path.is_file():
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "user source exclusions are not a regular file"
        )
    # Evaluate user rules in an empty, owner-private repository. The real
    # repository's .gitignore negations and core.excludesFile cannot undo them.
    with tempfile.TemporaryDirectory(prefix="loupe-exclusions-") as directory:
        empty = Path(directory)
        run_git(empty, ["init", "--quiet", "--template="], remaining=remaining)
        for start in range(0, len(relative_paths), 200):
            paths = relative_paths[start : start + 200]
            queries = [path + "/" if (root / path).is_dir() else path for path in paths]
            matches = _ignored(
                empty,
                queries,
                options=["-c", f"core.excludesFile={user_path}"],
                remaining=remaining,
            )
            excluded.update(
                path
                for path, query in zip(paths, queries, strict=True)
                if query in matches
            )
    return excluded


def ensure_safe_content(content: str | bytes) -> None:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if any(pattern.search(raw) for pattern in _SECRET_PATTERNS):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "source content contains recognized secret material "
            "and cannot be used by model tools",
        )
