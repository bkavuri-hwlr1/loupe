"""Git state is checkout-local and notices cannot expose source bodies."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.cli.render import checkout_event_text
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace.gitstate import preflight


def test_linked_worktree_checks_its_own_gitdir(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    git_run: Callable[..., str],
) -> None:
    root = repository_factory(tmp_path, {"file.txt": "base\n"})
    linked = tmp_path / "linked"
    git_run(root, "worktree", "add", "-b", "linked", str(linked))
    admin = Path(git_run(linked, "rev-parse", "--absolute-git-dir"))
    (admin / "index.lock").touch()
    with pytest.raises(LlmCoordError) as refusal:
        preflight(linked, ("file.txt",))
    assert refusal.value.code is ErrorCode.GIT_OPERATION_ACTIVE
    assert preflight(root, ("file.txt",))[1] == ()


def test_git_environment_cannot_redirect_preflight(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    root = repository_factory(first, {"file.txt": "base\n"})
    other = repository_factory(second, {"file.txt": "other\n"})
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    (root / ".git" / "index.lock").touch()
    with pytest.raises(LlmCoordError):
        preflight(root, ("file.txt",))


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        ("git.staged_paths_notice", "staged versions are retained"),
        ("git.concurrent_operation_attention", "attention and rescan required"),
        ("git.checkout_changed", "re-read files"),
    ],
)
def test_git_notices_show_only_paths_and_fixed_guidance(
    kind: str, fragment: str
) -> None:
    text = checkout_event_text(
        {
            "event_type": kind,
            "payload": {
                "paths": ["safe.txt"],
                "affected_session_id": "mine",
                "message": "private source and secret value must never render",
                "body": "private source and secret value must never render",
            },
        },
        own_session_id="mine",
    )
    assert text is not None and fragment in text
    assert "private source" not in text
    if kind != "git.checkout_changed":
        assert "safe.txt" in text


def test_checkout_notification_is_delivered_once_per_affected_session() -> None:
    assert (
        checkout_event_text(
            {
                "event_type": "git.checkout_changed",
                "payload": {"affected_session_id": "other"},
            },
            own_session_id="mine",
        )
        is None
    )


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.GIT_OPERATION_ACTIVE,
        ErrorCode.INDEX_PATH_UNMERGED,
        ErrorCode.INDEX_PATH_STAGED,
    ],
)
def test_git_refusals_have_retryable_cli_exit_code(code: ErrorCode) -> None:
    from llm_cli.cli.exit_codes import EXIT_BY_ERROR

    assert EXIT_BY_ERROR[code] == 75
