"""Runtime recovery retains exact domain proof until physical acknowledgment."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_isolated_creation_safety import manager
from test_runtime_refresh import prepare
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env


class ReceiptAuthority:
    """Authority protocol witness; actual physical locking is covered by G2."""

    def __init__(self, env: BatchEnvironment) -> None:
        self.env = env
        self.dirty = False
        self.depth = 0
        self.confirmations = 0

    @contextmanager
    def pin(self, root: Path, *, recovery: bool = False) -> Iterator[None]:
        assert root == self.env.repository
        if self.dirty and not recovery and not self.depth:
            raise ValueError("owning journal requires recovery")
        yield

    @contextmanager
    def mutation(
        self, root: Path, kind: str, reference: str, *, recovery: bool = False
    ) -> Iterator[Any]:
        with self.pin(root, recovery=recovery):
            authority = self

            class Operation:
                delegated = bool(authority.depth)

                def confirm(self) -> None:
                    if self.delegated:
                        return
                    # Cleaning physical authority without its exact runtime
                    # proof would silently authorize unrecoverable state.
                    with authority.env.store.connection() as con:
                        receipt = con.execute(
                            "SELECT state FROM isolated_runtime_journal "
                            "WHERE workspace_id=?",
                            (reference,),
                        ).fetchone()
                    assert receipt is not None and receipt[0] == "installed"
                    authority.dirty = False
                    authority.confirmations += 1

            assert kind == "runtime"
            self.dirty = True
            operation = Operation()
            self.depth += 1
            try:
                yield operation
            finally:
                self.depth -= 1


@pytest.mark.parametrize(
    "stage", ["runtime_committed", "runtime_cleaned", "runtime_authority_confirmed"]
)
def test_completed_runtime_receipt_survives_until_authority_acknowledges(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    authority = ReceiptAuthority(env)
    monkeypatch.setattr(env.store, "authority", authority, raising=False)
    (env.repository / "a.local").write_text("after a\n")

    class Crash(BaseException):
        pass

    def fault(point: str) -> None:
        if point == stage:
            raise Crash()

    isolate.runtime.fault = fault
    with pytest.raises(Crash):
        isolate.runtime.refresh(workspace.workspace_id)
    with env.store.connection() as con:
        assert (
            con.execute("SELECT state FROM isolated_runtime_journal").fetchone()[0]
            == "installed"
        )
    restarted = manager(env, tmp_path)
    restarted.runtime.recover(workspace.workspace_id)
    assert (private / "a.local").read_text() == "after a\n"
    assert not authority.dirty and authority.confirmations >= 1
    with authority.pin(env.repository):
        pass
    with env.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM isolated_runtime_journal").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("stage", ["runtime_committed", "runtime_cleaned"])
def test_late_private_write_cannot_clean_pending_runtime_authority(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    authority = ReceiptAuthority(env)
    monkeypatch.setattr(env.store, "authority", authority, raising=False)
    (env.repository / "a.local").write_text("after a\n")

    class Crash(BaseException):
        pass

    def fault(point: str) -> None:
        if point == stage:
            raise Crash()

    isolate.runtime.fault = fault
    with pytest.raises(Crash):
        isolate.runtime.refresh(workspace.workspace_id)
    (private / "a.local").write_text("late private edit\n")
    with pytest.raises(Exception, match="changed before authority acknowledgment"):
        manager(env, tmp_path).runtime.recover(workspace.workspace_id)
    assert (private / "a.local").read_text() == "late private edit\n"
    assert authority.dirty and authority.confirmations == 0
    with env.store.connection() as con:
        assert (
            con.execute("SELECT state FROM isolated_runtime_journal").fetchone()[0]
            == "installed"
        )


def test_nested_runtime_keeps_proof_until_parent_authority_confirms(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    isolate, workspace, _ = prepare(env, tmp_path)
    authority = ReceiptAuthority(env)
    monkeypatch.setattr(env.store, "authority", authority, raising=False)
    (env.repository / "a.local").write_text("after a\n")
    with authority.mutation(
        env.repository, "runtime", workspace.workspace_id
    ) as parent:
        assert isolate.runtime.refresh(workspace.workspace_id) == (
            "Runtime refreshed: a.local",
        )
        assert authority.dirty and authority.confirmations == 0
        with env.store.connection() as con:
            assert (
                con.execute("SELECT state FROM isolated_runtime_journal").fetchone()[0]
                == "installed"
            )
        parent.confirm()
    assert not authority.dirty
    isolate.runtime.recover(workspace.workspace_id)
    with env.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM isolated_runtime_journal").fetchone()[0]
            == 0
        )
