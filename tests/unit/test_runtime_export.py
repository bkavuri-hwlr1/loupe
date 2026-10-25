"""Runtime export is explicit, private, bounded and atomically creates new paths."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.workspace import runtime_export as export
from llm_cli.workspace.hydration import HydrationBudgets
from llm_cli.workspace.isolated import capture_source


@pytest.fixture
def runtime(tmp_path: Path, repository_factory: Any, git_run: Any) -> dict[str, Any]:
    checkout = repository_factory(
        tmp_path,
        {
            "a.txt": "source",
            "tracked/child": "source",
            ".gitignore": ".env\nbuild/\n*.local\n",
        },
    )
    private = tmp_path / "private"
    git_run(checkout, "worktree", "add", "--detach", str(private), "HEAD")
    (private / ".env").write_text("MARKER=ordinary-canary-value-93372\n")
    (private / "build").mkdir()
    (private / "build" / "result").write_bytes(b"built output\x00\xff")
    (private / "build" / "nested").mkdir()
    (private / "build" / "nested" / "other").write_bytes(b"more output")
    return {
        "root": private,
        "checkout": checkout,
        "runtime_paths": (".env", "build"),
        "staging_root": Path(git_run(private, "rev-parse", "--absolute-git-dir")),
        "source_paths": ("a.txt", "tracked/child", ".gitignore"),
    }


def test_secret_preview_and_two_independent_approvals(
    runtime: Any, tmp_path: Path
) -> None:
    destination = tmp_path / "exported.env"
    for flags in ({}, {"yes": True}, {"allow_secret": True}):
        result = export.export_runtime(
            relative=".env", destination=destination, **runtime, **flags
        )
        assert result["state"] == "confirmation_required"
        assert result["kind"] == "file" and result["sensitivity"] == "likely_secret"
        assert not destination.exists()
        assert ".env" not in repr(result) and "ordinary-canary" not in repr(result)
    result = export.export_runtime(
        relative=".env", destination=destination, **runtime, yes=True, allow_secret=True
    )
    assert result["state"] == "exported"
    assert destination.read_bytes() == (runtime["root"] / ".env").read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o600


def test_file_and_directory_publish_complete_private_bytes(
    runtime: Any, tmp_path: Path
) -> None:
    for relative in ("build/result", "build"):
        destination = tmp_path / ("tree" if relative == "build" else "file")
        result = export.export_runtime(
            relative=relative, destination=destination, **runtime, yes=True
        )
        assert result["state"] == "exported"
        target = destination / "result" if relative == "build" else destination
        assert target.read_bytes() == b"built output\x00\xff"
        assert target.stat().st_mode & 0o777 == 0o600
        if relative == "build":
            assert destination.stat().st_mode & 0o777 == 0o700
            assert (destination / "nested/other").read_bytes() == b"more output"
    assert not list(tmp_path.glob(".loupe-runtime-export-*"))


@pytest.mark.parametrize("offset", [0, 1024 * 1024 - 6, 2 * 1024 * 1024 + 31])
def test_secret_signature_in_ordinary_runtime_filename_needs_protected_approval(
    runtime: Any,
    tmp_path: Path,
    offset: int,
) -> None:
    path = runtime["root"] / "build" / "credentials-data.json"
    token = b"AKIA" + b"Q" * 16
    path.write_bytes(b" " * offset + token + b"\n")
    destination = tmp_path / "out"
    preview = export.export_runtime(
        relative="build/credentials-data.json",
        destination=destination,
        **runtime,
        yes=True,
    )
    assert preview["state"] == "confirmation_required"
    assert preview["sensitivity"] == "likely_secret"
    assert preview["requires_allow_secret"]
    assert not destination.exists()
    assert token.decode() not in repr(preview)
    assert (
        export.export_runtime(
            relative="build/credentials-data.json",
            destination=destination,
            **runtime,
            yes=True,
            allow_secret=True,
        )["state"]
        == "exported"
    )
    assert destination.read_bytes() == path.read_bytes()


def test_regenerated_secret_after_nonsecret_preview_requires_new_approval(
    runtime: Any,
    tmp_path: Path,
) -> None:
    path = runtime["root"] / "build" / "result"
    destination = tmp_path / "out"
    preview = export.export_runtime(
        relative="build/result",
        destination=destination,
        **runtime,
    )
    assert preview["sensitivity"] == "unclassified"
    path.write_bytes(b"secret = sk-" + b"r" * 48 + b"\n")
    changed = export.export_runtime(
        relative="build/result",
        destination=destination,
        **runtime,
        yes=True,
    )
    assert changed["state"] == "confirmation_required"
    assert changed["requires_allow_secret"]
    assert not destination.exists()


@pytest.mark.parametrize("name", ["a.txt", "tracked", "new.local", "new-source"])
def test_checkout_guard_tracked_prefix_and_existing_ignored_untracked(
    runtime: Any, name: str
) -> None:
    destination = runtime["checkout"] / name
    with pytest.raises(LlmCoordError, match="--into-checkout"):
        export.export_runtime(
            relative="build", destination=destination, **runtime, yes=True
        )
    if name in {"a.txt", "tracked"}:
        with pytest.raises(LlmCoordError, match="tracked source"):
            export.export_runtime(
                relative="build",
                destination=destination,
                **runtime,
                yes=True,
                into_checkout=True,
            )
    else:
        destination.write_text("existing user bytes")
        with pytest.raises(LlmCoordError):
            export.export_runtime(
                relative="build",
                destination=destination,
                **runtime,
                yes=True,
                into_checkout=True,
            )
        assert destination.read_text() == "existing user bytes"
        destination.unlink()
        if name == "new.local":
            assert (
                export.export_runtime(
                    relative="build",
                    destination=destination,
                    **runtime,
                    yes=True,
                    into_checkout=True,
                )["state"]
                == "exported"
            )
        else:
            with pytest.raises(LlmCoordError, match="ignored destination"):
                export.export_runtime(
                    relative="build",
                    destination=destination,
                    **runtime,
                    yes=True,
                    into_checkout=True,
                )


def test_other_linked_checkout_and_coordinator_storage_are_guarded(
    runtime: Any, tmp_path: Path, git_run: Any
) -> None:
    sibling = tmp_path / "sibling"
    git_run(runtime["checkout"], "worktree", "add", "--detach", str(sibling), "HEAD")
    with pytest.raises(LlmCoordError, match="--into-checkout"):
        export.export_runtime(
            relative="build", destination=sibling / "new.local", **runtime, yes=True
        )
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(LlmCoordError, match="coordinator storage"):
        export.export_runtime(
            relative="build",
            destination=state / "new",
            **runtime,
            yes=True,
            protected_roots=(state,),
        )


def test_case_aliases_cannot_bypass_checkout_or_coordinator_policy(
    runtime: Any,
    tmp_path: Path,
) -> None:
    checkout = runtime["checkout"]
    alias = checkout.with_name(checkout.name.upper())
    if not alias.exists() or not os.path.samefile(alias, checkout):
        pytest.skip("requires a case-insensitive native filesystem")
    with pytest.raises(LlmCoordError, match="--into-checkout"):
        export.export_runtime(
            relative="build",
            destination=alias / "new-source",
            **runtime,
            yes=True,
        )
    with pytest.raises(LlmCoordError, match="ignored destination"):
        export.export_runtime(
            relative="build",
            destination=alias / "new-source",
            **runtime,
            yes=True,
            into_checkout=True,
        )
    assert (
        export.export_runtime(
            relative="build",
            destination=alias / "new.local",
            **runtime,
            yes=True,
            into_checkout=True,
        )["state"]
        == "exported"
    )
    state = tmp_path / "coordinator-state"
    state.mkdir()
    for protected, target in (
        (state, state.with_name(state.name.upper()) / "out"),
        (runtime["root"], runtime["root"].with_name("PRIVATE") / "out"),
    ):
        with pytest.raises(LlmCoordError, match="coordinator storage"):
            export.export_runtime(
                relative="build",
                destination=target,
                **runtime,
                yes=True,
                into_checkout=True,
                protected_roots=(protected,),
            )


@pytest.mark.parametrize(
    "relative", ["../outside", "/outside", "a.txt", "tracked", ".git", "build/"]
)
def test_export_cannot_escape_runtime_or_copy_source(
    runtime: Any, tmp_path: Path, relative: str
) -> None:
    with pytest.raises((LlmCoordError, ValueError)):
        export.export_runtime(
            relative=relative, destination=tmp_path / "out", **runtime, yes=True
        )
    assert not (tmp_path / "out").exists()


def test_new_ignored_runtime_is_eligible_but_new_source_is_not(
    runtime: Any, tmp_path: Path
) -> None:
    (runtime["root"] / "new.local").write_text("new output")
    (runtime["root"] / "new-source").write_text("new source")
    assert (
        export.export_runtime(
            relative="new.local", destination=tmp_path / "out", **runtime, yes=True
        )["state"]
        == "exported"
    )
    with pytest.raises(LlmCoordError, match="ignored or reserved"):
        export.export_runtime(
            relative="new-source",
            destination=tmp_path / "source-out",
            **runtime,
            yes=True,
        )


@pytest.mark.parametrize(
    "budgets",
    [
        HydrationBudgets(max_file_bytes=1),
        HydrationBudgets(max_total_bytes=1),
        HydrationBudgets(max_entries=1),
        HydrationBudgets(max_metadata_bytes=1),
        HydrationBudgets(max_depth=1),
    ],
)
def test_hydration_budgets_fail_without_partial_destination(
    runtime: Any, tmp_path: Path, budgets: HydrationBudgets
) -> None:
    with pytest.raises(LlmCoordError, match="budget"):
        export.export_runtime(
            relative="build",
            destination=tmp_path / "out",
            **runtime,
            yes=True,
            budgets=budgets,
        )
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".loupe-runtime-export-*"))


def test_source_symlinks_and_destination_ancestor_symlinks_are_refused(
    runtime: Any, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("outside secret")
    (runtime["root"] / "build/link").symlink_to(outside / "secret")
    with pytest.raises(LlmCoordError, match="links"):
        export.export_runtime(
            relative="build", destination=tmp_path / "out", **runtime, yes=True
        )
    (tmp_path / "destination-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LlmCoordError, match="unsafe"):
        export.export_runtime(
            relative="build/result",
            destination=tmp_path / "destination-link/out",
            **runtime,
            yes=True,
        )
    assert not (outside / "out").exists()


@pytest.mark.parametrize("directory", [False, True])
def test_destination_race_never_overwrites_new_file_or_directory(
    runtime: Any, tmp_path: Path, monkeypatch: Any, directory: bool
) -> None:
    original = export._rename_new
    destination = tmp_path / "out"

    def race(parent: int, stage: str, leaf: str) -> None:
        if directory:
            os.mkdir(leaf, dir_fd=parent)
        else:
            destination.write_bytes(b"competing user bytes")
        original(parent, stage, leaf)

    monkeypatch.setattr(export, "_rename_new", race)
    with pytest.raises(LlmCoordError, match="already exists"):
        export.export_runtime(
            relative="build" if directory else "build/result",
            destination=destination,
            **runtime,
            yes=True,
        )
    assert (
        list(destination.iterdir()) == []
        if directory
        else destination.read_bytes() == b"competing user bytes"
    )
    assert not list(tmp_path.glob(".loupe-runtime-export-*"))


@pytest.mark.parametrize("failure", ["io", "changed", "link"])
def test_partial_copy_or_source_race_removes_stage_and_never_publishes(
    runtime: Any, tmp_path: Path, monkeypatch: Any, failure: str
) -> None:
    original = export._copy_file
    calls = 0
    outside = tmp_path / "outside-value"
    outside.write_text("must never be copied")

    def fail(
        source: int,
        path: str,
        parent: int,
        name: str,
        before: os.stat_result,
        deadline: float,
    ) -> None:
        nonlocal calls
        calls += 1
        if failure == "link":
            (runtime["root"] / path).unlink()
            (runtime["root"] / path).symlink_to(outside)
        original(source, path, parent, name, before, deadline)
        if calls == 1:
            if failure == "io":
                raise OSError("sensitive filename or value must not escape")
            (runtime["root"] / path).write_text("changed")

    monkeypatch.setattr(export, "_copy_file", fail)
    with pytest.raises(LlmCoordError) as caught:
        export.export_runtime(
            relative="build", destination=tmp_path / "out", **runtime, yes=True
        )
    assert "sensitive filename" not in str(caught.value)
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".loupe-runtime-export-*"))


def test_destination_ancestor_swap_is_detected_and_stage_is_removed(
    runtime: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    parent = tmp_path / "destination-parent"
    parent.mkdir()
    moved = tmp_path / "moved-parent"
    original = export._copy_file

    def race(*args: Any) -> None:
        original(*args)
        parent.rename(moved)
        parent.mkdir()

    monkeypatch.setattr(export, "_copy_file", race)
    with pytest.raises(LlmCoordError, match="destination changed"):
        export.export_runtime(
            relative="build/result", destination=parent / "out", **runtime, yes=True
        )
    assert list(parent.iterdir()) == list(moved.iterdir()) == []


def test_final_ancestor_swap_rolls_back_only_the_published_export(
    runtime: Any,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    parent = tmp_path / "destination-parent"
    parent.mkdir()
    moved = tmp_path / "moved-parent"
    original = export._rename_new

    def race(*args: Any) -> None:
        original(*args)
        parent.rename(moved)
        parent.mkdir()

    monkeypatch.setattr(export, "_rename_new", race)
    with pytest.raises(LlmCoordError, match="destination changed"):
        export.export_runtime(
            relative="build",
            destination=parent / "out",
            **runtime,
            yes=True,
        )
    assert list(parent.iterdir()) == list(moved.iterdir()) == []


def test_checkout_ignore_policy_is_rechecked_before_publication(
    runtime: Any,
    monkeypatch: Any,
) -> None:
    original = export._copy_file
    destination = runtime["checkout"] / "new.local"

    def changed_policy(*args: Any) -> None:
        original(*args)
        (runtime["checkout"] / ".gitignore").write_text(".env\nbuild/\n")

    monkeypatch.setattr(export, "_copy_file", changed_policy)
    with pytest.raises(LlmCoordError, match="ignored destination"):
        export.export_runtime(
            relative="build/result",
            destination=destination,
            **runtime,
            yes=True,
            into_checkout=True,
        )
    assert not destination.exists()
    assert not list(runtime["checkout"].glob(".loupe-runtime-export-*"))


def test_checkout_staging_is_invisible_to_real_source_capture(
    runtime: Any,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    original = export._rename_new
    canary = (runtime["root"] / ".env").read_bytes()
    captured = tmp_path / "captured-source"
    captured.mkdir(mode=0o700)

    def capture_during_stage(*args: Any, **kwargs: Any) -> None:
        manifest = capture_source(runtime["checkout"], captured)
        assert all("loupe-runtime-export" not in path for path in manifest)
        assert all(canary not in path.read_bytes() for path in captured.iterdir())
        original(*args, **kwargs)

    monkeypatch.setattr(export, "_rename_new", capture_during_stage)
    assert (
        export.export_runtime(
            relative=".env",
            destination=runtime["checkout"] / "kept.local",
            **runtime,
            yes=True,
            allow_secret=True,
            into_checkout=True,
        )["state"]
        == "exported"
    )


def test_checkout_staging_refuses_cross_filesystem_before_creating_bytes(
    runtime: Any,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original = export.os.fstat

    def other_device(fd: int) -> Any:
        if fd == descriptor:
            return SimpleNamespace(st_dev=-1)
        return original(fd)

    monkeypatch.setattr(export.os, "fstat", other_device)
    try:
        with (
            pytest.raises(LlmCoordError, match="same filesystem"),
            export._staging_parent(descriptor, runtime["staging_root"]),
        ):
            pytest.fail("cross-filesystem staging must not open")
    finally:
        os.close(descriptor)
    assert not list(runtime["staging_root"].glob("loupe-runtime-export-*"))


def test_fsync_failure_after_publication_rolls_back_complete_export(
    runtime: Any,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    original = export._rename_new

    def failed_fsync(fd: int) -> None:
        raise OSError("full")

    def publish_then_fail(*args: Any) -> None:
        original(*args)
        monkeypatch.setattr(export.os, "fsync", failed_fsync)

    monkeypatch.setattr(export, "_rename_new", publish_then_fail)
    with pytest.raises(LlmCoordError):
        export.export_runtime(
            relative="build",
            destination=tmp_path / "out",
            **runtime,
            yes=True,
        )
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".loupe-runtime-export-*"))


def test_cli_resolves_symlinked_destination_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # macOS /tmp and $TMPDIR are symlinks; the daemon refuses linked ancestors.
    from typing import cast

    from llm_cli.cli import app
    from llm_cli.protocol.client import DaemonClient

    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    sent: list[dict[str, Any]] = []

    class Client:
        paths = None

        def call(self, method: str, params: dict[str, Any], **_: Any) -> Any:
            sent.append(params)
            return {
                "state": "confirmation_required",
                "kind": "file",
                "bytes": 1,
                "entries": 1,
                "sensitivity": "unclassified",
                "requires_yes": True,
                "requires_allow_secret": False,
            }

    monkeypatch.setattr(app, "session_resume_secret", lambda paths, session: "s")
    arguments = app.build_parser().parse_args(
        [
            "workspace",
            "runtime",
            "export",
            ".env",
            "--session",
            "session",
            "--to",
            str(tmp_path / "link" / "new.env"),
        ]
    )
    app.dispatch(arguments, cast(DaemonClient, Client()))
    assert sent[0]["destination"] == str(real.resolve() / "new.env")
