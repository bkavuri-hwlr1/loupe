"""Execution contracts at the physical checkout boundary."""

from pathlib import Path

from llm_cli.coordination.models import ExecutionRecord, WorkspaceExecutionRecord
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.storage.control import ControlStore

Execution = ExecutionRecord | WorkspaceExecutionRecord


def assert_execution_regime(
    store: ControlStore, root: Path, execution: Execution | None = None
) -> None:
    authority = store.authority
    if authority is None:
        if isinstance(execution, WorkspaceExecutionRecord):
            raise ValueError("workspace execution requires physical checkout authority")
        return
    if isinstance(execution, WorkspaceExecutionRecord):
        regime = authority.regime(root)
        if regime["physical_key"] != execution.physical_key:
            raise LlmCoordError(
                ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                "workspace execution physical binding changed",
            )
        authority.assert_regime(
            root, allowed=("workspace",), generation=execution.regime_generation
        )
    else:
        authority.assert_regime(root, allowed=("legacy",))
