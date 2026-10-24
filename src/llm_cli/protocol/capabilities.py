"""Additive capabilities for explicit checkout cutover and retained history.

The versioned RPC envelope is unchanged. A capability advertises an implemented
contract; the physical checkout's current regime still gates every mutation.
"""

WORKSPACE_CAPABILITIES = (
    "workspace.authority.maintenance.v1",
    "workspace.cutover.recovery.v1",
    "workspace.cutover.v1",
    "workspace.execution.claim_free.v1",
    "legacy.history.read.v1",
)

__all__ = ["WORKSPACE_CAPABILITIES"]
