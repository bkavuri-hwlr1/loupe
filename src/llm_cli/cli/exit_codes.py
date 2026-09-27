from __future__ import annotations

from llm_cli.errors import ErrorCode

EXIT_BY_ERROR: dict[ErrorCode, int] = {
    ErrorCode.CONFIG_INVALID: 2,
    ErrorCode.DAEMON_UNAVAILABLE: 69,
    ErrorCode.PROTOCOL_MISMATCH: 76,
    ErrorCode.REPOSITORY_NOT_FOUND: 66,
    ErrorCode.REPOSITORY_UNSAFE: 65,
    ErrorCode.CLAIM_QUEUED: 75,
    ErrorCode.CLAIM_STALE: 75,
    ErrorCode.SCOPE_VIOLATION: 77,
    ErrorCode.TASK_NOT_MUTABLE: 65,
    ErrorCode.WORKTREE_DIRTY: 65,
    ErrorCode.TARGET_MOVED: 75,
    ErrorCode.INTEGRATION_CONFLICT: 75,
    ErrorCode.PROVIDER_UNAVAILABLE: 69,
    ErrorCode.PROVIDER_AMBIGUOUS: 65,
    ErrorCode.INDEX_UNAVAILABLE: 69,
    ErrorCode.APPROVAL_REQUIRED: 77,
    ErrorCode.INTERNAL_RECOVERABLE: 70,
}


__all__ = ["EXIT_BY_ERROR"]
