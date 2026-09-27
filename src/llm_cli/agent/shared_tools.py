"""Model tools with private candidate bytes over a coordinated shared checkout.

Tools never write the checkout. The execution runner publishes the collected
candidates together after the agent finishes, comparing each path with its
last full-file observation before editing. Checkpoints retain observations and pending
bytes so a resumed tool call cannot silently adopt another session's base.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import json
import re
from _thread import RLock
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from llm_cli.agent.tools import (
    ToolBroker,
    ToolOutcome,
    _bounded_children,
    _display_path,
    _error,
    _listing_response,
    _ok,
    _page_options,
    _page_response,
    _read_range,
    _read_response,
    _search_match,
    _string,
)
from llm_cli.coordination.scopes import (
    normalize_changed_path,
    normalize_scope,
    uncovered_paths,
)
from llm_cli.errors import LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.workspace.batches import BatchFile, candidate_target
from llm_cli.workspace.broker import (
    MAX_SHARED_TEXT_BYTES,
    WorkspaceBrokerError,
)
from llm_cli.workspace.identity import (
    ABSENT,
    DIRECTORY_MODE,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    FileIdentity,
    IdentityError,
    ObjectKind,
    content_identity,
    read_identified_path,
)

_MAX_CANDIDATES = 50
_MAX_OBSERVATIONS = 200
_MAX_RETAINED_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Observation:
    base: FileIdentity
    content: bytes | None


@dataclass(slots=True)
class SharedToolBroker(ToolBroker):
    """Read shared source while accumulating one task's unpublished overlay.

    Ignored files and nested repositories are excluded from this tool surface.
    This is a cooperative boundary, not a sandbox for arbitrary processes.
    Reads outside the write claim remain available for source context.
    """

    publication_lock: RLock = field(default_factory=RLock)
    guard: Callable[[], None] | None = None
    _observations: dict[str, _Observation] = field(default_factory=dict, init=False)
    _contents: dict[str, bytes | None] = field(default_factory=dict, init=False)

    _modes: dict[str, str] = field(default_factory=dict, init=False)

    def invoke(self, name: str, arguments: Mapping[str, object]) -> ToolOutcome:
        # An operator can take minutes to answer. This tool has no shared
        # checkout effect and must not hold every other session's read barrier.
        if name in {"ask_user", "run_check", "finish_task"}:
            return ToolBroker.invoke(self, name, arguments)
        before = self.usage.calls
        with self.publication_lock:
            snapshot = (
                self.usage_snapshot()
                if name
                in {
                    "write_file",
                    "apply_patch",
                    "delete_file",
                    "rename_file",
                    "create_directory",
                }
                else None
            )
            try:
                if self.guard is not None:
                    self.guard()
                result = ToolBroker.invoke(self, name, arguments)
                if result.is_error and snapshot is not None:
                    self.restore_usage({**snapshot, **ToolBroker.usage_snapshot(self)})
                return result
            except (WorkspaceBrokerError, IdentityError) as exc:
                if snapshot is not None:
                    self.restore_usage({**snapshot, **ToolBroker.usage_snapshot(self)})
                if self.usage.calls == before:
                    self.record_interrupted_call()
                return _error(f"the shared workspace refused that operation: {exc}")
            except LlmCoordError as exc:
                if snapshot is not None:
                    self.restore_usage({**snapshot, **ToolBroker.usage_snapshot(self)})
                if self.usage.calls == before:
                    self.record_interrupted_call()
                return _error(f"the repository refused that operation: {exc.message}")

    def candidates(self) -> tuple[BatchFile, ...]:
        """Return immutable pending files; only the runner may publish them."""

        with self.publication_lock:
            return tuple(
                BatchFile(
                    relative_path=relative,
                    base=self._observations[relative].base,
                    content=content,
                    mode=self._modes.get(
                        relative, self._observations[relative].base.mode or REGULAR_MODE
                    ),
                    original=self._observations[relative].content,
                )
                for relative, content in sorted(self._contents.items())
            )

    def usage_snapshot(self) -> dict[str, object]:
        with self.publication_lock:
            saved = ToolBroker.usage_snapshot(self)
            saved["shared_workspace_state"] = {
                "version": 2,
                "files": [
                    {
                        "path": relative,
                        "base": {
                            "kind": str(observation.base.kind),
                            "mode": observation.base.mode,
                            "digest": observation.base.digest,
                            "size": observation.base.size,
                        },
                        "read_content": _encode(observation.content),
                        "content": _encode(self._contents.get(relative)),
                        "pending": relative in self._contents,
                        "mode": self._modes.get(relative),
                    }
                    for relative, observation in sorted(self._observations.items())
                ],
            }
            return saved

    def restore_usage(self, saved: Mapping[str, object]) -> None:
        with self.publication_lock:
            state = saved.get("shared_workspace_state")
            if state is None:
                if saved.get("files_read", 0) or saved.get("writes", 0):
                    raise ValueError("saved shared workspace observations are missing")
                ToolBroker.restore_usage(self, saved)
                self._observations = {}
                self._contents = {}
                return
            if (
                not isinstance(state, dict)
                or set(state) != {"version", "files"}
                or type(state["version"]) is not int
                or state["version"] not in {1, 2}
                or not isinstance(state["files"], list)
            ):
                raise ValueError("saved shared workspace state is invalid")
            files = state["files"]
            if len(files) > min(_MAX_OBSERVATIONS, self.limits.max_read_files):
                raise ValueError("saved shared workspace has too many observations")
            observations: dict[str, _Observation] = {}
            contents: dict[str, bytes | None] = {}
            modes: dict[str, str] = {}
            for item in files:
                if not isinstance(item, dict) or set(item) != (
                    {
                        "path",
                        "base",
                        "read_content",
                        "content",
                    }
                    | ({"pending", "mode"} if state["version"] == 2 else set())
                ):
                    raise ValueError("saved shared workspace file is invalid")
                relative = item["path"]
                if (
                    not isinstance(relative, str)
                    or normalize_changed_path(relative) != relative
                    or relative in observations
                ):
                    raise ValueError("saved shared workspace path is invalid")
                self._target(relative)
                original = _decode(item["read_content"])
                base = _restore_identity(item["base"], original)
                observations[relative] = _Observation(base, original)
                content = _decode(item["content"])
                pending = item.get("pending", content is not None)
                if not isinstance(pending, bool):
                    raise ValueError("invalid pending flag")
                if pending:
                    if self.agent_mode == "plan":
                        raise ValueError(
                            "plan mode cannot restore a checkpoint containing edits"
                        )
                    mode = item.get("mode") or (base.mode or REGULAR_MODE)
                    if content is None:
                        mode = ""
                    if mode not in {"", DIRECTORY_MODE, REGULAR_MODE, EXECUTABLE_MODE}:
                        raise ValueError("invalid candidate mode")
                    modes[relative] = mode
                    outside = uncovered_paths(
                        self.scopes,
                        (relative,),
                        case_insensitive_filesystem=self.case_insensitive_filesystem,
                    )
                    if outside:
                        raise ValueError("saved shared candidate is outside the claim")
                    if len(content or b"") > self.limits.max_write_bytes:
                        raise ValueError(
                            "saved shared candidate exceeds the write limit"
                        )
                    contents[relative] = content
            _check_retained_bounds(observations, contents)
            ToolBroker.restore_usage(self, saved)
            self._observations = observations
            self._contents = contents
            self._modes = modes

    def _list_files(self, arguments: Mapping[str, object]) -> ToolOutcome:
        directory = self._directory(_string(arguments, "path", default="."))
        children, capped = self._visible_children(directory)
        entries = {
            child.relative_to(self.worktree).as_posix()
            + ("/" if child.is_dir() else "")
            for child in children
        }
        entries.update(
            relative
            for relative in self._contents
            if (self.worktree / relative).parent == directory
            and self._contents[relative] is not None
        )
        entries = {
            entry
            for entry in entries
            if self._contents.get(entry.rstrip("/"), b"") is not None
        }
        entries = {
            entry.rstrip("/")
            + (
                "/"
                if self._modes.get(entry.rstrip("/")) == DIRECTORY_MODE
                else ("/" if entry.endswith("/") else "")
            )
            for entry in entries
        }
        return _listing_response(
            sorted(entries),
            arguments,
            self.limits,
            stop_reason="scanned_file_limit; narrow path" if capped else None,
        )

    def _read_file(self, arguments: Mapping[str, object]) -> ToolOutcome:
        relative = normalize_changed_path(_string(arguments, "path"))
        _read_range(arguments)
        partial_request = any(
            key in arguments for key in ("start_line", "end_line", "start_column")
        )
        refresh = arguments.get("refresh", False)
        if not isinstance(refresh, bool):
            return _error("refresh must be a boolean")
        content = self._read(
            relative,
            pin_observation=(partial_request or relative in self._partial_reads)
            and not refresh,
            charge=not partial_request,
        )
        if content is None:
            # An explicit refresh with no private candidate adopts the current
            # ABSENT identity just as a successful full-file refresh adopts new
            # content. The model has now seen the whole current state, so a
            # later write may safely create the path against that exact base.
            if refresh and relative not in self._contents:
                self._partial_reads.discard(relative)
            return _error(
                f"{_display_path(relative)} is absent. "
                "Use list_files to check the exact path."
            )
        result = _read_response(content.decode("utf-8"), arguments, self.limits)
        if partial_request:
            returned_bytes = result.metadata.get("bytes_returned", 0)
            assert isinstance(returned_bytes, int)
            if self.usage.bytes_read + returned_bytes > self.limits.max_read_bytes:
                self._partial_reads.add(relative)
                return _error(
                    "this execution reached its total read-size limit; "
                    "request fewer lines"
                )
            self.usage.bytes_read += returned_bytes
        if not result.is_error and result.metadata.get("complete_file", False):
            self._partial_reads.discard(relative)
        else:
            self._partial_reads.add(relative)
        return result

    def _search_text(self, arguments: Mapping[str, object]) -> ToolOutcome:
        offset, limit = _page_options(arguments, self.limits)
        try:
            expression = re.compile(_string(arguments, "pattern"))
        except re.error as exc:
            return _error(f"that is not a valid regular expression: {exc}")
        raw = _string(arguments, "path", default=".")
        relative = (
            None
            if raw.strip() in {"", ".", "./", "*"}
            else normalize_scope(raw).rstrip("/")
        )
        single_file = (
            relative
            if relative is not None
            and (
                self._target(relative).is_file()
                or (
                    relative in self._contents
                    and self._modes.get(relative) != DIRECTORY_MODE
                )
            )
            else None
        )
        directories = [] if single_file is not None else [self._directory(raw)]
        matches: list[str] = []
        scanned = 0
        scanned_bytes = 0
        seen = 0
        fingerprint = hashlib.sha256(
            (_string(arguments, "pattern") + "\0" + raw).encode("utf-8")
        )

        def finish(
            *, more: bool = False, stop_reason: str | None = None
        ) -> ToolOutcome:
            return _page_response(
                matches[:limit],
                offset=offset,
                more=more or len(matches) > limit,
                explicit="offset" in arguments or "limit" in arguments,
                empty="(no matches at this offset)" if offset else "(no matches)",
                limits=self.limits,
                stop_reason=stop_reason,
                snapshot=fingerprint.hexdigest(),
                expected_snapshot=arguments.get("snapshot"),
            )

        while directories or single_file is not None:
            directory = directories.pop() if single_file is None else None
            children, capped = (
                self._visible_children(directory)
                if directory is not None
                else ([], False)
            )
            directories.extend(
                reversed(
                    [
                        path
                        for path in children
                        if path.is_dir()
                        or self._modes.get(path.relative_to(self.worktree).as_posix())
                        == DIRECTORY_MODE
                    ]
                )
            )
            relative_paths = {
                path.relative_to(self.worktree).as_posix()
                for path in children
                if not path.is_dir()
                and self._modes.get(path.relative_to(self.worktree).as_posix())
                != DIRECTORY_MODE
            }
            relative_paths.update(
                relative
                for relative in self._contents
                if (self.worktree / relative).parent == directory
            )
            if single_file is not None:
                relative_paths.add(single_file)
                single_file = None
            for relative in sorted(relative_paths):
                if relative in self._contents and (
                    self._contents[relative] is None
                    or self._modes.get(relative) == DIRECTORY_MODE
                ):
                    continue
                if scanned >= self.limits.max_scanned_files:
                    return finish(stop_reason="scanned_file_limit; narrow path")
                scanned += 1
                try:
                    content = self._contents.get(relative)
                    if content is None:
                        # This directory's entries were already filtered in
                        # one Git call; do not spawn Git again for every file.
                        _, content = self._observe(relative, check_ignored=False)
                    if content is None:
                        continue
                    scanned_bytes += len(content)
                    if scanned_bytes > self.limits.max_read_bytes:
                        return finish(stop_reason="read_size_limit; narrow path")
                    current = content.decode("utf-8")
                    fingerprint.update(
                        json.dumps(
                            [relative, hashlib.sha256(content).hexdigest()]
                        ).encode("utf-8")
                    )
                except (WorkspaceBrokerError, IdentityError, UnicodeDecodeError):
                    continue
                for number, line in enumerate(current.splitlines(), start=1):
                    if expression.search(line):
                        if seen >= offset and len(matches) <= limit:
                            matches.append(_search_match(relative, number, line))
                        seen += 1
            if capped:
                return finish(stop_reason="scanned_file_limit; narrow path")
        return finish()

    def _write_file(self, arguments: Mapping[str, object]) -> ToolOutcome:
        relative = normalize_changed_path(_string(arguments, "path"))
        if relative in self._partial_reads:
            return _error(
                "only part of this file was shown; use apply_patch to preserve "
                "unseen content, or read the complete file before replacing it"
            )
        denial = self._write_denial(relative)
        if denial is not None:
            return denial
        content = _string(arguments, "content").encode("utf-8")
        if self._modes.get(relative) == DIRECTORY_MODE:
            return _error("that path is a staged directory")
        self._target(relative)
        if relative not in self._observations:
            identity, original = self._observe(relative)
            if not identity.absent:
                return _error(f"read {relative} with read_file before replacing it")
            self._remember(relative, identity, original)
        self._parents(relative)
        return self._stage(relative, content)

    def _apply_patch(self, arguments: Mapping[str, object]) -> ToolOutcome:
        relative = normalize_changed_path(_string(arguments, "path"))
        old_text = _string(arguments, "old_text")
        new_text = _string(arguments, "new_text")
        if not old_text:
            return _error("old_text must not be empty; use write_file to create a file")
        denial = self._write_denial(relative)
        if denial is not None:
            return denial
        self._target(relative)
        current = self._contents.get(relative)
        if current is None:
            current = self._read(
                relative,
                pin_observation=relative in self._partial_reads,
                charge=relative not in self._partial_reads,
            )
        if current is None:
            return _error(f"{relative} is absent; use write_file to create it")
        text = current.decode("utf-8")
        occurrences = text.count(old_text)
        if occurrences != 1:
            return _error(
                f"old_text appears {occurrences} times in {relative}; "
                "include enough surrounding context to make it unique"
            )
        content = text.replace(old_text, new_text, 1).encode("utf-8")
        return self._stage(relative, content)

    def _read_diff(self, arguments: Mapping[str, object]) -> ToolOutcome:
        del arguments
        if not self._contents:
            return _ok("(no changes yet)", self.limits)
        diffs: list[str] = []
        for relative, content in sorted(self._contents.items()):
            observed = self._observations[relative]
            diffs.extend(
                difflib.unified_diff(
                    (observed.content or b"").decode("utf-8").splitlines(keepends=True),
                    (content or b"").decode("utf-8").splitlines(keepends=True),
                    fromfile="/dev/null" if observed.base.absent else f"a/{relative}",
                    tofile="/dev/null" if content is None else f"b/{relative}",
                )
            )
        paths = "\n".join(f"  {relative}" for relative in sorted(self._contents))
        return _ok(
            f"Pending candidate paths:\n{paths}\n\n{''.join(diffs)}", self.limits
        )

    def _validate_changes(self, arguments: Mapping[str, object]) -> ToolOutcome:
        del arguments
        if not self._contents:
            return _ok("no changes yet; nothing to validate", self.limits)
        conflicts: list[str] = []
        for candidate in self.candidates():
            denial = self._write_denial(candidate.relative_path)
            if denial is not None:
                return denial
            identity, _ = read_identified_path(self._target(candidate.relative_path))
            if identity != candidate.base:
                conflicts.append(candidate.relative_path)
        if conflicts:
            return _error(
                "shared paths changed since their base read; candidates are preserved "
                f"but publication will conflict: {', '.join(conflicts)}"
            )
        listing = "\n".join(f"  {relative}" for relative in sorted(self._contents))
        return _ok(
            f"all {len(self._contents)} candidate path(s) are in scope and match "
            f"their observed bases:\n{listing}\nPublication rechecks these identities.",
            self.limits,
        )

    def _parents(self, relative: str) -> None:
        for parent in reversed(Path(relative).parents):
            if str(parent) == ".":
                continue
            path = parent.as_posix()
            target = self._target(path)
            if path in self._contents:
                if self._modes.get(path) != DIRECTORY_MODE:
                    raise WorkspaceBrokerError("candidate parent is not a directory")
            elif not target.exists():
                denied = self._write_denial(path)
                if denied:
                    raise WorkspaceBrokerError(denied.content)
                self._remember(path, ABSENT, None)
                self._stage(path, b"", mode=DIRECTORY_MODE)

    def _create_directory(self, arguments: Mapping[str, object]) -> ToolOutcome:
        relative = normalize_changed_path(_string(arguments, "path"))
        denied = self._write_denial(relative)
        if denied:
            return denied
        target = self._target(relative)
        if relative in self._contents and self._modes.get(relative) != DIRECTORY_MODE:
            return _error("directory destination already has a staged file")
        if target.exists():
            if target.is_dir():
                return _ok("directory already exists", self.limits)
            return _error("directory destination already exists")
        self._parents(relative)
        self._remember(relative, ABSENT, None)
        return self._stage(relative, b"", mode=DIRECTORY_MODE)

    def _delete_file(self, arguments: Mapping[str, object]) -> ToolOutcome:
        relative = normalize_changed_path(_string(arguments, "path"))
        denied = self._write_denial(relative)
        if denied:
            return denied
        if (
            self._read(relative, pin_observation=relative in self._partial_reads)
            is None
        ):
            return _error("file is absent")
        return self._stage(relative, None)

    def _rename_file(self, arguments: Mapping[str, object]) -> ToolOutcome:
        source = normalize_changed_path(_string(arguments, "source"))
        destination = normalize_changed_path(_string(arguments, "destination"))
        if source.casefold() == destination.casefold():
            return _error("same-path and case-only renames are unavailable")
        for path in (source, destination):
            denied = self._write_denial(path)
            if denied:
                return denied
        # Roll back all private state if either side cannot be staged.
        saved = self.usage_snapshot()
        try:
            content = self._read(source, pin_observation=source in self._partial_reads)
            if content is None:
                return _error("source file is absent")
            if (
                self._read(destination) is not None
                or self._target(destination).exists()
            ):
                return _error("rename destination must be absent")
            self._parents(destination)
            mode = self._modes.get(source, self._observations[source].base.mode)
            result = self._stage(destination, content, mode=mode)
            if result.is_error:
                raise WorkspaceBrokerError(result.content)
            self._stage(source, None)
            return _ok(f"staged rename {source} -> {destination}", self.limits)
        except Exception:
            self.restore_usage(saved)
            raise

    def _read(
        self, relative: str, *, pin_observation: bool = False, charge: bool = True
    ) -> bytes | None:
        if self.usage.files_read >= self.limits.max_read_files:
            raise WorkspaceBrokerError("this execution reached its file-read limit")
        self._target(relative)
        if self._modes.get(relative) == DIRECTORY_MODE:
            raise WorkspaceBrokerError("that path is a directory")
        content: bytes | None
        identity: FileIdentity | None = None
        if relative in self._contents:
            content = self._contents[relative]
        elif pin_observation and relative in self._observations:
            # A later range or patch must keep the base captured with the first
            # partial view, even when a peer published while the model thought.
            content = self._observations[relative].content
        else:
            identity, content = self._observe(relative)
            if content is not None:
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WorkspaceBrokerError(f"{relative} is not UTF-8 text") from exc
        size = len(content) if content is not None else 0
        if charge and self.usage.bytes_read + size > self.limits.max_read_bytes:
            raise WorkspaceBrokerError(
                "this execution reached its total read-size limit"
            )
        if identity is not None:
            self._remember(relative, identity, content)
        self.usage.files_read += 1
        if charge:
            self.usage.bytes_read += size
        return content

    def _remember(
        self, relative: str, identity: FileIdentity, content: bytes | None
    ) -> None:
        if relative in self._contents:
            return
        proposed = {**self._observations, relative: _Observation(identity, content)}
        if len(proposed) > min(_MAX_OBSERVATIONS, self.limits.max_read_files):
            raise WorkspaceBrokerError("this task reached its observation limit")
        _check_retained_bounds(proposed, self._contents)
        self._observations = proposed

    def _stage(
        self, relative: str, content: bytes | None, *, mode: str | None = None
    ) -> ToolOutcome:
        if len(content or b"") > min(
            MAX_SHARED_TEXT_BYTES, self.limits.max_write_bytes
        ):
            return _error("that content exceeds the shared per-file size limit")
        proposed = dict(self._contents)
        if content == self._observations[relative].content:
            proposed.pop(relative, None)
        else:
            proposed[relative] = content
        _check_retained_bounds(self._observations, proposed)
        self._contents = proposed
        self._modes[relative] = (
            ""
            if content is None
            else (mode or self._observations[relative].base.mode or REGULAR_MODE)
        )
        self.usage.writes += 1
        if relative not in proposed:
            self._modes.pop(relative, None)
            return _ok(
                f"{relative} matches its observed base; no pending change", self.limits
            )
        return _ok(
            f"staged {relative} ({len(content or b'')} bytes; unpublished)", self.limits
        )

    def _observe(
        self, relative: str, *, check_ignored: bool = True
    ) -> tuple[FileIdentity, bytes | None]:
        identity, content = read_identified_path(
            self._target(relative, check_ignored=check_ignored),
            max_bytes=MAX_SHARED_TEXT_BYTES,
        )
        if identity.kind not in {ObjectKind.ABSENT, ObjectKind.REGULAR}:
            raise WorkspaceBrokerError(f"{relative} is not a regular source file")
        return identity, content

    def _target(self, relative: str, *, check_ignored: bool = True) -> Path:
        target = candidate_target(self.worktree, relative)
        current = self.worktree
        for component in Path(relative).parts[:-1]:
            current /= component
            if (current / ".git").exists():
                raise WorkspaceBrokerError("tools cannot enter a nested repository")
        if target.is_symlink():
            raise WorkspaceBrokerError("tools cannot follow repository symlinks")
        if check_ignored and relative in self._ignored_paths([relative]):
            raise WorkspaceBrokerError("ignored runtime files are not shared source")
        return target

    def _directory(self, raw: str) -> Path:
        if raw.strip() in {"", ".", "./", "*"}:
            return self.worktree
        relative = normalize_scope(raw).rstrip("/")
        target = self._target(relative)
        if (not target.is_dir() and self._modes.get(relative) != DIRECTORY_MODE) or (
            target / ".git"
        ).exists():
            raise WorkspaceBrokerError(
                f"{_display_path(relative)} is not a real source directory. "
                "Use list_files to check the exact path."
            )
        return target

    def _visible_children(self, directory: Path) -> tuple[list[Path], bool]:
        raw_children, capped = (
            _bounded_children(directory, self.limits.max_scanned_files)
            if directory.exists()
            else ([], False)
        )
        children = [
            child
            for child in raw_children
            if child.name.casefold() != ".git"
            and not child.is_symlink()
            and (child.is_file() or child.is_dir())
            and not (child.is_dir() and (child / ".git").exists())
        ]
        children.extend(
            self.worktree / path
            for path, mode in self._modes.items()
            if mode == DIRECTORY_MODE
            and (self.worktree / path).parent == directory
            and self.worktree / path not in children
        )
        ignored = self._ignored_paths(
            [child.relative_to(self.worktree).as_posix() for child in children]
        )
        return sorted(
            [
                child
                for child in children
                if child.relative_to(self.worktree).as_posix() not in ignored
            ],
            key=lambda child: child.name,
        ), capped

    def _ignored_paths(self, relative_paths: list[str]) -> set[str]:
        ignored: set[str] = set()
        for start in range(0, len(relative_paths), 200):
            result = run_git(
                self.worktree,
                ["check-ignore", "-z", "--stdin"],
                check=False,
                input_data="\x00".join(relative_paths[start : start + 200]) + "\x00",
            )
            if result.returncode not in {0, 1}:
                raise WorkspaceBrokerError("could not determine ignored source paths")
            assert isinstance(result.stdout, str)
            ignored.update(filter(None, result.stdout.split("\x00")))
        return ignored


def _check_retained_bounds(
    observations: Mapping[str, _Observation], contents: Mapping[str, bytes | None]
) -> None:
    if len(contents) > _MAX_CANDIDATES:
        raise WorkspaceBrokerError("this task exceeds the 50-candidate-file limit")
    retained = sum(len(item.content or b"") for item in observations.values())
    retained += sum(len(content or b"") for content in contents.values())
    if retained > _MAX_RETAINED_BYTES:
        raise WorkspaceBrokerError(
            "this task exceeds its 4-MiB private workspace limit"
        )


def _encode(content: bytes | None) -> str | None:
    return base64.b64encode(content).decode("ascii") if content is not None else None


def _decode(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4 * (
        (MAX_SHARED_TEXT_BYTES + 2) // 3
    ):
        raise ValueError("saved shared workspace content is invalid or too large")
    try:
        content = base64.b64decode(value, validate=True)
        content.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("saved shared workspace content is not encoded UTF-8") from exc
    if len(content) > MAX_SHARED_TEXT_BYTES:
        raise ValueError("saved shared workspace content exceeds its file limit")
    return content


def _restore_identity(value: object, content: bytes | None) -> FileIdentity:
    if not isinstance(value, dict) or set(value) != {"kind", "mode", "digest", "size"}:
        raise ValueError("saved shared file identity is invalid")
    kind, mode, digest, size = (
        value["kind"],
        value["mode"],
        value["digest"],
        value["size"],
    )
    if type(size) is not int or size < 0:
        raise ValueError("saved shared file identity has an invalid size")
    if kind == str(ObjectKind.DIRECTORY_MARKER):
        from llm_cli.workspace.batches import directory_identity

        identity = directory_identity()
        if (
            content is not None
            or mode != identity.mode
            or digest != identity.digest
            or size != 0
        ):
            raise ValueError("invalid directory identity")
        return identity
    if kind == str(ObjectKind.ABSENT):
        if content is not None or mode != "" or digest != ABSENT.digest or size != 0:
            raise ValueError("saved absent file identity is inconsistent")
        return ABSENT
    if (
        kind != str(ObjectKind.REGULAR)
        or mode not in (REGULAR_MODE, EXECUTABLE_MODE)
        or content is None
        or size != len(content)
        or digest != content_identity(ObjectKind.REGULAR, mode, content)
    ):
        raise ValueError("saved shared file identity does not match its read content")
    return FileIdentity(ObjectKind.REGULAR, mode, digest, size)


__all__ = ["SharedToolBroker"]
