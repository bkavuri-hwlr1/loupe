"""Private source stays out of tool results and durable observations."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("read_file", {"path": ".git/private-note"}),
        ("search_text", {"path": ".git/private-note", "pattern": "dummy-private"}),
        ("search_text", {"path": ".git", "pattern": "dummy-private"}),
        ("list_files", {"path": ".git"}),
        ("write_file", {"path": ".git/private-note", "content": "replacement"}),
        (
            "apply_patch",
            {
                "path": ".git/private-note",
                "old_text": "dummy-private",
                "new_text": "replacement",
            },
        ),
        ("delete_file", {"path": ".git/private-note"}),
        ("create_directory", {"path": ".git/new-directory"}),
        (
            "rename_file",
            {"source": ".git/private-note", "destination": "source-note.txt"},
        ),
    ],
)
def test_explicit_git_administration_paths_are_not_source(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    broker_type: type[ToolBroker],
    tool: str,
    arguments: dict[str, object],
) -> None:
    repository = repository_factory(tmp_path, {"source.txt": "ordinary source\n"})
    marker = "dummy-private-git-administration-content"
    metadata = repository / ".git/private-note"
    metadata.write_text(marker)
    broker = broker_type(worktree=repository, scopes=("*",))

    result = broker.invoke(tool, arguments)

    assert result.is_error, result.content
    assert marker not in result.content + json.dumps(broker.usage_snapshot())
    assert metadata.read_text() == marker
    assert not (repository / ".git/new-directory").exists()
    assert not (repository / "source-note.txt").exists()


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
@pytest.mark.parametrize("directory_alias", [False, True])
def test_git_administration_cannot_be_read_through_symlink_aliases(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    broker_type: type[ToolBroker],
    directory_alias: bool,
) -> None:
    repository = repository_factory(tmp_path, {"source.txt": "ordinary source\n"})
    marker = "dummy-private-git-administration-content"
    (repository / ".git/private-note").write_text(marker)
    if directory_alias:
        (repository / "metadata").symlink_to(".git", target_is_directory=True)
        relative = "metadata/private-note"
    else:
        (repository / "metadata-note").symlink_to(".git/private-note")
        relative = "metadata-note"
    broker = broker_type(worktree=repository, scopes=("*",))

    read = broker.invoke("read_file", {"path": relative})
    search = broker.invoke(
        "search_text", {"path": relative, "pattern": "dummy-private"}
    )

    assert read.is_error, read.content
    assert search.is_error, search.content
    for result in (
        read,
        search,
        broker.invoke("search_text", {"pattern": "dummy-private"}),
        broker.invoke("list_files", {}),
    ):
        assert marker not in result.content
    assert marker not in json.dumps(broker.usage_snapshot())


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
def test_user_global_exclusions_apply_to_read_search_and_snapshot(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    broker_type: type[ToolBroker],
) -> None:
    repository = repository_factory(tmp_path, {"source.txt": "ordinary source\n"})
    exclusions = tmp_path / "user-ignore"
    exclusions.write_text("private-notes.txt\n")
    config = tmp_path / "user-gitconfig"
    config.write_text(f'[core]\n    excludesFile = "{exclusions}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    secret = "dummy-private-notes-content"
    (repository / "private-notes.txt").write_text(secret)
    broker = broker_type(worktree=repository, scopes=("*",))
    read = broker.invoke("read_file", {"path": "private-notes.txt"})
    assert read.is_error
    search = broker.invoke("search_text", {"pattern": "dummy-private"})
    listing = broker.invoke("list_files", {})
    assert "private-notes.txt" not in listing.content
    assert secret not in read.content + search.content + json.dumps(
        broker.usage_snapshot()
    )


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
@pytest.mark.parametrize("relative", [".env", "private.pem", "credentials.json"])
def test_common_private_files_are_denied_even_without_gitignore(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    broker_type: type[ToolBroker],
    relative: str,
) -> None:
    repository = repository_factory(tmp_path, {"source.txt": "ordinary source\n"})
    (repository / relative).write_text("dummy-private-content")
    broker = broker_type(worktree=repository, scopes=("*",))
    read = broker.invoke("read_file", {"path": relative})
    assert read.is_error
    search = broker.invoke("search_text", {"pattern": "dummy-private"})
    assert "dummy-private-content" not in read.content + search.content


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
def test_known_secret_material_is_denied_before_persistence(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    broker_type: type[ToolBroker],
) -> None:
    repository = repository_factory(tmp_path, {"source.txt": "ordinary source\n"})
    secret = (
        "-----BEGIN " + "PRIVATE KEY-----\ndummy-private-key\n-----END PRIVATE KEY-----"
    )
    (repository / "notes.txt").write_text(secret)
    broker = broker_type(worktree=repository, scopes=("*",))
    read = broker.invoke("read_file", {"path": "notes.txt"})
    assert read.is_error
    assert "dummy-private-key" not in read.content + json.dumps(broker.usage_snapshot())


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
def test_repository_negation_cannot_override_user_privacy_exclusions(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    broker_type: type[ToolBroker],
) -> None:
    repository = repository_factory(tmp_path, {".gitignore": "!private-notes.txt\n"})
    exclusions = tmp_path / "user-ignore"
    exclusions.write_text("private-notes.txt\n")
    config = tmp_path / "user-gitconfig"
    config.write_text(f'[core]\n    excludesFile = "{exclusions}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    (repository / "private-notes.txt").write_text("dummy-private-content")
    broker = broker_type(worktree=repository, scopes=("*",))
    assert broker.invoke("read_file", {"path": "private-notes.txt"}).is_error
    assert (
        "dummy-private-content"
        not in broker.invoke("search_text", {"pattern": "dummy-private"}).content
    )


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
def test_explicit_descendants_of_private_directories_are_denied(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    broker_type: type[ToolBroker],
) -> None:
    repository = repository_factory(
        tmp_path, {"credentials/personal.txt": "dummy-private-content"}
    )
    broker = broker_type(worktree=repository, scopes=("*",))
    assert broker.invoke("read_file", {"path": "credentials/personal.txt"}).is_error
    search = broker.invoke(
        "search_text", {"path": "credentials", "pattern": "dummy-private"}
    )
    assert search.is_error
    assert "dummy-private-content" not in search.content


def test_isolated_diff_excludes_private_files(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    from llm_cli.git.worktrees import create_managed_worktree

    repository = repository_factory(
        tmp_path, {"source.txt": "base\n", ".env": "old-private\n"}
    )
    managed = create_managed_worktree(
        repository,
        managed_root=tmp_path / "managed",
        task_id="privacy-diff",
        base_oid=git_run(repository, "rev-parse", "HEAD"),
    )
    (managed.path / ".env").write_text("new-private\n")
    (managed.path / "source.txt").write_text("changed\n")
    result = ToolBroker(worktree=managed.path, scopes=("*",)).invoke("read_diff", {})
    assert not result.is_error, result.content
    assert "+changed" in result.content
    assert "old-private" not in result.content and "new-private" not in result.content


def test_shared_diff_and_restore_recheck_new_user_exclusions(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace.broker import WorkspaceBrokerError

    repository = repository_factory(tmp_path, {"notes.txt": "old-private\n"})
    broker = SharedToolBroker(worktree=repository, scopes=("*",))
    assert not broker.invoke("read_file", {"path": "notes.txt"}).is_error
    assert not broker.invoke(
        "write_file", {"path": "notes.txt", "content": "new-private\n"}
    ).is_error
    saved = broker.usage_snapshot()
    exclusions = tmp_path / "user-ignore"
    exclusions.write_text("notes.txt\n")
    config = tmp_path / "user-gitconfig"
    config.write_text(f'[core]\n    excludesFile = "{exclusions}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    result = broker.invoke("read_diff", {})
    assert result.is_error
    assert "old-private" not in result.content and "new-private" not in result.content
    restored = SharedToolBroker(worktree=repository, scopes=("*",))
    with pytest.raises(WorkspaceBrokerError):
        restored.restore_usage(saved)
    assert "old-private" not in json.dumps(restored.usage_snapshot())


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
@pytest.mark.parametrize("private_path", [".env", "private-notes.txt"])
def test_rename_cannot_launder_excluded_source(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    broker_type: type[ToolBroker],
    private_path: str,
) -> None:
    repository = repository_factory(tmp_path, {private_path: "dummy-private-value\n"})
    exclusions = tmp_path / "user-ignore"
    exclusions.write_text("private-notes.txt\n")
    config = tmp_path / "user-gitconfig"
    config.write_text(f'[core]\n    excludesFile = "{exclusions}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    broker = broker_type(worktree=repository, scopes=("*",))

    assert broker.invoke("read_file", {"path": private_path}).is_error
    result = broker.invoke(
        "rename_file", {"source": private_path, "destination": "source.txt"}
    )
    assert result.is_error
    assert (repository / private_path).read_text() == "dummy-private-value\n"
    assert not (repository / "source.txt").exists()
    read = broker.invoke("read_file", {"path": "source.txt"})
    assert read.is_error
    assert "dummy-private-value" not in read.content


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
@pytest.mark.parametrize(
    "tool", ["write_file", "apply_patch", "delete_file", "create_directory"]
)
def test_mutations_cannot_access_excluded_paths(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    broker_type: type[ToolBroker],
    tool: str,
) -> None:
    repository = repository_factory(tmp_path, {".env": "dummy-private-value\n"})
    broker = broker_type(worktree=repository, scopes=("*",))
    arguments: dict[str, object] = {"path": ".env"}
    if tool == "write_file":
        arguments["content"] = "replacement\n"
    elif tool == "apply_patch":
        arguments.update(old_text="dummy-private", new_text="replacement")
    elif tool == "create_directory":
        arguments["path"] = "credentials"
    assert broker.invoke(tool, arguments).is_error
    assert (repository / ".env").read_text() == "dummy-private-value\n"
    assert not (repository / "credentials").exists()


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
@pytest.mark.parametrize("tool", ["apply_patch", "rename_file"])
def test_mutations_cannot_remove_secret_classification(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    broker_type: type[ToolBroker],
    tool: str,
) -> None:
    header = "-----BEGIN " + "PRIVATE KEY-----"
    secret = header + "\ndummy-private-key\n"
    repository = repository_factory(tmp_path, {"notes.txt": secret})
    broker = broker_type(worktree=repository, scopes=("*",))
    arguments = (
        {"path": "notes.txt", "old_text": header, "new_text": "removed"}
        if tool == "apply_patch"
        else {"source": "notes.txt", "destination": "source.txt"}
    )
    assert broker.invoke(tool, arguments).is_error
    assert (repository / "notes.txt").read_text() == secret
    assert not (repository / "source.txt").exists()
