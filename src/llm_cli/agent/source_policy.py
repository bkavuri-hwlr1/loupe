"""Shared source exclusions and bounded, conservative secret classification."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
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
# Git metadata is never source, including when a source-looking symlink
# resolves into it. Scope validation only sees the spelling supplied by the
# model; these exclusions also apply to the resolved path.
_PRIVATE_DIRECTORIES = frozenset({".git", ".ssh", ".aws", ".azure", ".gnupg"})
_EXAMPLE_SUFFIXES = (".example", ".sample", ".template")
# Paths per git check-ignore process.
_CHECK_BATCH = 1_000
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,255}\b"),
    re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}\b"),
)


_runtime_roots: dict[Path, tuple[str, ...]] = {}
_runtime_lock = threading.Lock()


def register_runtime_paths(root: Path, paths: tuple[str, ...]) -> None:
    """Pin hydrated paths independently of mutable repository ignore rules."""
    with _runtime_lock:
        _runtime_roots[root.resolve()] = paths


def runtime_path(root: Path, relative: str) -> bool:
    with _runtime_lock:
        paths = _runtime_roots.get(root.resolve(), ())
    return any(relative == path or relative.startswith(path + "/") for path in paths)


# Within one tool call, each root's user exclusion setting is read once.
_user_excludes_cache: ContextVar[dict[Path, str] | None] = ContextVar(
    "loupe_user_excludes", default=None
)
# A batch of reads checks all its paths at once, then answers each read's
# own check from these results.
_known_exclusions: ContextVar[tuple[Path, Mapping[str, bool]] | None] = ContextVar(
    "loupe_known_exclusions", default=None
)
# A synchronous helper may have a shorter deadline than an individual source
# operation. Keep it local to that call, including nested exclusion checks.
_source_remaining: ContextVar[Callable[[], float] | None] = ContextVar(
    "loupe_source_remaining", default=None
)
# User rules are evaluated in an empty repository; one per process suffices
# because nothing ever writes to it.
_empty_lock = threading.Lock()
_empty_repository_directory: tempfile.TemporaryDirectory[str] | None = None


def _combined_remaining(
    explicit: Callable[[], float] | None,
) -> Callable[[], float] | None:
    scoped = _source_remaining.get()
    if scoped is None:
        return explicit
    if explicit is None:
        return scoped
    return lambda: min(scoped(), explicit())


@contextmanager
def source_deadline(remaining: Callable[[], float]) -> Iterator[None]:
    """Bound source metadata by both its caller's and a helper's deadlines."""

    token = _source_remaining.set(_combined_remaining(remaining))
    try:
        yield
    finally:
        _source_remaining.reset(token)


@contextmanager
def known_exclusions(root: Path, answers: Mapping[str, bool]) -> Iterator[None]:
    """Answer exclusion checks of these paths from results already computed.

    ``answers`` maps each path to whether ``excluded_paths`` excludes it. A
    check that names any other path runs normally.
    """

    token = _known_exclusions.set((root, answers))
    try:
        yield
    finally:
        _known_exclusions.reset(token)


def exclusion_answers(
    root: Path,
    relative_paths: list[str],
    *,
    remaining: Callable[[], float] | None = None,
) -> dict[str, bool]:
    """Check many paths in one pass, for later use with ``known_exclusions``."""

    excluded = excluded_paths(root, relative_paths, remaining=remaining)
    return {path: path in excluded for path in relative_paths}


@contextmanager
def exclusion_scope() -> Iterator[None]:
    """Read the user's exclusion setting at most once per root in this block.

    One tool call then sees one consistent setting, and a walk over many
    directories does not start a Git process per directory to read it again.
    """

    if _user_excludes_cache.get() is not None:
        yield
        return
    token = _user_excludes_cache.set({})
    try:
        yield
    finally:
        _user_excludes_cache.reset(token)


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


def _scoped_user_excludes_file(
    root: Path, remaining: Callable[[], float] | None
) -> str:
    cache = _user_excludes_cache.get()
    if cache is None:
        return _user_excludes_file(root, remaining)
    if root not in cache:
        cache[root] = _user_excludes_file(root, remaining)
    return cache[root]


def _empty_repository(remaining: Callable[[], float] | None) -> Path:
    global _empty_repository_directory
    with _exclusion_repository_lock(remaining):
        directory = _empty_repository_directory
        if directory is not None and not (Path(directory.name) / ".git/HEAD").is_file():
            # A temporary-file cleaner removed it; start a fresh one.
            directory.cleanup()
            directory = _empty_repository_directory = None
        if directory is None:
            directory = tempfile.TemporaryDirectory(
                prefix="loupe-exclusions-", ignore_cleanup_errors=True
            )
            try:
                run_git(
                    Path(directory.name),
                    ["init", "--quiet", "--template="],
                    remaining=remaining,
                )
            except BaseException:
                directory.cleanup()
                raise
            _empty_repository_directory = directory
        return Path(directory.name)


@contextmanager
def _exclusion_repository_lock(
    remaining: Callable[[], float] | None,
) -> Iterator[None]:
    if remaining is None:
        _empty_lock.acquire()
    else:
        while not _empty_lock.acquire(timeout=min(0.05, remaining())):
            pass
    try:
        if remaining is not None:
            remaining()
        yield
    finally:
        _empty_lock.release()


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
    remaining = _combined_remaining(remaining)
    if remaining is not None:
        remaining()
    if not relative_paths:
        return set()
    known = _known_exclusions.get()
    if known is not None and known[0] == root:
        answers = known[1]
        if all(path in answers for path in relative_paths):
            return {
                path
                for path in relative_paths
                if answers[path] or runtime_path(root, path)
            }
    excluded = {
        path
        for path in relative_paths
        if _private_path(path) or runtime_path(root, path)
    }
    for start in range(0, len(relative_paths), _CHECK_BATCH):
        excluded.update(
            _ignored(
                root,
                relative_paths[start : start + _CHECK_BATCH],
                options=[],
                remaining=remaining,
            )
        )
    user_excludes = _scoped_user_excludes_file(root, remaining)
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
    empty = _empty_repository(remaining)
    for start in range(0, len(relative_paths), _CHECK_BATCH):
        paths = relative_paths[start : start + _CHECK_BATCH]
        queries = [path + "/" if (root / path).is_dir() else path for path in paths]
        matches = _ignored(
            empty,
            queries,
            options=["-c", f"core.excludesFile={user_path}"],
            remaining=remaining,
        )
        excluded.update(
            path for path, query in zip(paths, queries, strict=True) if query in matches
        )
    return excluded


def contains_secret_material(content: str | bytes) -> bool:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return any(pattern.search(raw) for pattern in _SECRET_PATTERNS)


def ensure_safe_content(content: str | bytes) -> None:
    if contains_secret_material(content):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "source content contains recognized secret material "
            "and cannot be used by model tools",
        )
