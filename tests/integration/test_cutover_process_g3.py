"""G3 cutover survives killed processes and fences a competing legacy queue."""

from __future__ import annotations

import json
import os
import select
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import ClaimState
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.git.inspect import inspect_repository
from llm_cli.storage.control import ControlStore
from llm_cli.storage.migrations import CONTROL_MIGRATIONS, apply_migrations
from llm_cli.workspace.authority import PhysicalAuthority, PhysicalCheckout
from llm_cli.workspace.installation import private_read
from llm_cli.workspace.regime import CutoverManager


def _store(directory: Path, repository: Path, profile: str) -> ControlStore:
    directory.mkdir(mode=0o700, exist_ok=True)
    store = ControlStore(directory / "control.sqlite3")
    store.initialize()
    info = inspect_repository(repository, profile_id=profile)
    registered = store.register_repository(
        repo_key=info.repo_key,
        display_name=profile,
        git_common_dir=str(info.common_git_dir),
        main_worktree_path=str(info.main_worktree),
        target_ref=info.target_ref,
        profile_id=profile,
    )
    checkout = store.ensure_checkout(
        repository=registered,
        canonical_path=str(repository.resolve()),
        git_common_dir=str(info.common_git_dir),
    )
    store.ensure_shared_workspace(checkout)
    return store


def _authority(base: Path, repository: Path, profile: str) -> PhysicalAuthority:
    directory = base / profile
    store = _store(directory, repository, profile)
    authority = PhysicalAuthority(
        store,
        profile,
        f"{profile}-{os.getpid()}",
        directory / "socket",
        root=base / "authority" / "v1",
    )
    store.authority = authority
    return authority


def _snapshot(base: Path, repository: Path) -> dict[str, Any]:
    physical = PhysicalCheckout.inspect(repository)
    try:
        marker = private_read(
            base / "authority" / "v1" / "checkouts" / physical.key / "marker.json"
        )
    finally:
        physical.close()
    database = base / "owner" / "control.sqlite3"
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        intent = connection.execute("SELECT * FROM cutover_intents").fetchall()
        regime = connection.execute("SELECT * FROM checkout_regimes").fetchall()
    assert len(intent) == len(regime) == 1
    return {"marker": marker, "intent": dict(intent[0]), "regime": dict(regime[0])}


def _process_entry() -> None:
    base, repository = Path(sys.argv[1]), Path(sys.argv[2])
    action, boundary, identity, checkpoint = sys.argv[3:]
    profile = (
        "peer" if action.startswith("foreign") or action == "activate" else "owner"
    )
    authority = _authority(base, repository, profile)

    def pause(stage: str) -> None:
        if stage == boundary:
            print(json.dumps({"stage": stage, "pid": os.getpid()}), flush=True)
            assert sys.stdin.readline() == "continue\n"

    manager = CutoverManager(authority.store, authority, fault=pause)
    advance = authority.advance_regime

    def after_marker(*args: Any, **kwargs: Any) -> None:
        advance(*args, **kwargs)
        pause(f"{kwargs['regime']}_marker_written")

    authority.advance_regime = after_marker  # type: ignore[method-assign]
    try:
        if action == "begin":
            result = manager.begin(repository, checkpoint, intent_id=identity)
        elif action == "resume":
            result = manager.resume(repository, identity, checkpoint=checkpoint)
        elif action.startswith("foreign"):
            with authority.pin(repository):
                authority.assert_regime(repository, allowed=("legacy",))
                if action == "foreign-source":
                    (repository / "source.py").write_text("VALUE = 2\n")
                elif action == "foreign-index":
                    blob = run_git(
                        repository,
                        ["hash-object", "-w", "--stdin"],
                        input_data="VALUE = 2\n",
                    ).stdout.strip()
                    run_git(
                        repository,
                        ["update-index", "--cacheinfo", "100644", blob, "source.py"],
                    )
            result = {"code": "legacy_acquired"}
        elif action == "activate":
            coordinator = RepositoryCoordinator(authority.store)
            reconciled = coordinator.reconcile_expired()
            waiter = authority.store.get_claim(identity)
            assert waiter is not None
            result = {
                "activated": [claim.claim_id for claim in reconciled.activated],
                "waiter_state": waiter.state.value,
                "fencing_token": waiter.fencing_token,
            }
        else:
            raise AssertionError(action)
        print(json.dumps(result), flush=True)
    except LlmCoordError as exc:
        print(json.dumps({"code": exc.code.value}), flush=True)
    finally:
        authority.close()


def _arguments(
    base: Path,
    repository: Path,
    action: str,
    boundary: str = "",
    identity: str = "process-cutover",
    checkpoint: str = "",
) -> list[str]:
    return [
        sys.executable,
        "-u",
        "-c",
        "from test_cutover_process_g3 import _process_entry; _process_entry()",
        str(base),
        str(repository),
        action,
        boundary,
        identity,
        checkpoint,
    ]


def _environment() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (str(Path(__file__).parent), str(Path(__file__).parents[2] / "src"))
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


@contextmanager
def _paused(arguments: list[str], boundary: str) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        arguments,
        env=_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 15)[0], "cutover did not pause"
        line = process.stdout.readline()
        assert line, process.communicate(timeout=5)
        assert json.loads(line) == {"stage": boundary, "pid": process.pid}
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def _run(arguments: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        arguments, env=_environment(), capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def _same_transition(before: dict[str, Any], after: dict[str, Any]) -> None:
    assert after["intent"] == before["intent"]
    assert after["regime"] == before["regime"]
    if before["marker"]["state"] != "clean":
        assert after["marker"] == before["marker"]
    else:
        # Reacquisition may advance clean ownership diagnostics. It must retain
        # the exact regime/intent proof instead of restarting or rolling back.
        for key in (
            "state",
            "version",
            "physical_key",
            "binding",
            "execution_contract",
            "regime",
            "regime_generation",
            "regime_checkpoint",
            "regime_intent_id",
        ):
            assert after["marker"].get(key) == before["marker"].get(key)
        assert after["marker"]["generation"] >= before["marker"]["generation"]


@pytest.mark.parametrize(
    "boundary",
    [
        "intent_saved",
        "transition_marker_written",
        "transition_marked",
        "checkpoint_saved",
        "workspace_marker_written",
        "workspace_marked",
        "intent_completed",
        "marker_clean",
    ],
)
def test_killed_cutover_resumes_exact_intent_and_generation(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    git_run: Callable[..., str],
    boundary: str,
) -> None:
    repository = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
    original_refs = git_run(repository, "show-ref")
    authority = _authority(tmp_path, repository, "owner")
    plan = CutoverManager(authority.store, authority).preview(repository)
    authority.close()
    with _paused(
        _arguments(
            tmp_path, repository, "begin", boundary, checkpoint=plan["checkpoint"]
        ),
        boundary,
    ) as process:
        before = _snapshot(tmp_path, repository)
        process.kill()
        assert process.wait(timeout=5) < 0

    # Once transition is globally durable, another profile cannot revive claims
    # or clear the owner's interrupted cutover journal after kernel-lock release.
    if boundary != "intent_saved":
        foreign = _run(_arguments(tmp_path, repository, "foreign"))
        expected = (
            ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
            if boundary == "marker_clean"
            else ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
        )
        assert foreign == {"code": expected.value}
        _same_transition(before, _snapshot(tmp_path, repository))

    wrong = _run(_arguments(tmp_path, repository, "resume", checkpoint="0" * 64))
    assert wrong == {"code": ErrorCode.CUTOVER_CHECKPOINT_STALE.value}
    _same_transition(before, _snapshot(tmp_path, repository))
    resumed = _run(
        _arguments(tmp_path, repository, "resume", checkpoint=plan["checkpoint"])
    )
    assert resumed["regime"] == "workspace" and resumed["generation"] == 2
    assert resumed["intent_id"] == "process-cutover"
    assert resumed["checkpoint"] == plan["checkpoint"]
    after = _snapshot(tmp_path, repository)
    assert after["intent"]["intent_id"] == before["intent"]["intent_id"]
    assert after["intent"]["plan_json"] == before["intent"]["plan_json"]
    assert after["intent"]["checkpoint"] == plan["checkpoint"]
    assert after["intent"]["stage"] == "complete"
    assert after["regime"]["regime"] == "workspace"
    assert after["regime"]["generation"] >= before["regime"]["generation"]
    assert after["marker"]["generation"] >= before["marker"]["generation"]
    assert after["marker"]["regime_generation"] == 2
    assert after["marker"]["state"] == "clean"
    assert (
        _run(_arguments(tmp_path, repository, "resume", checkpoint=plan["checkpoint"]))[
            "intent_id"
        ]
        == "process-cutover"
    )
    assert (repository / "source.py").read_text() == "VALUE = 1\n"
    assert git_run(repository, "show-ref") == original_refs


@pytest.mark.parametrize("changed", ["source", "index"])
def test_foreign_change_after_pre_marker_crash_invalidates_saved_checkpoint(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    git_run: Callable[..., str],
    changed: str,
) -> None:
    repository = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
    original_head = git_run(repository, "rev-parse", "HEAD")
    original_tree = git_run(repository, "write-tree")
    authority = _authority(tmp_path, repository, "owner")
    plan = CutoverManager(authority.store, authority).preview(repository)
    authority.close()
    with _paused(
        _arguments(
            tmp_path, repository, "begin", "intent_saved", checkpoint=plan["checkpoint"]
        ),
        "intent_saved",
    ) as process:
        process.kill()
        assert process.wait(timeout=5) < 0
    before = _snapshot(tmp_path, repository)
    assert _run(_arguments(tmp_path, repository, "foreign-" + changed)) == {
        "code": "legacy_acquired"
    }
    resumed = _run(
        _arguments(tmp_path, repository, "resume", checkpoint=plan["checkpoint"])
    )
    assert resumed == {"code": ErrorCode.CUTOVER_CHECKPOINT_STALE.value}
    _same_transition(before, _snapshot(tmp_path, repository))
    assert git_run(repository, "rev-parse", "HEAD") == original_head
    if changed == "source":
        assert (repository / "source.py").read_text() == "VALUE = 2\n"
        assert git_run(repository, "write-tree") == original_tree
    else:
        assert (repository / "source.py").read_text() == "VALUE = 1\n"
        assert git_run(repository, "write-tree") != original_tree


def test_foreign_waiter_cannot_activate_while_cutover_process_is_transitioning(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> None:
    repository = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
    # This older profile has a durable queued claim but no local regime rows.
    # Clearing its blocker makes activation eligible at the cutover barrier.
    peer = _store(tmp_path / "peer", repository, "peer")
    registered = peer.list_repositories()[0]
    for task_id in ("old-holder", "old-waiter"):
        peer.create_task(repository_id=registered.repository_id, task_id=task_id)
    coordinator = RepositoryCoordinator(peer)
    holder = coordinator.request_claim("old-holder", ("source.py",))
    waiter = coordinator.request_claim("old-waiter", ("source.py",))
    assert waiter.state is ClaimState.QUEUED
    with peer.connection() as connection:
        connection.execute(
            "UPDATE claims SET state='cancelled',lease_expires_at=NULL,"
            "terminal_expires_at=9999999999999 WHERE claim_id=?",
            (holder.claim_id,),
        )
    authority = _authority(tmp_path, repository, "owner")
    plan = CutoverManager(authority.store, authority).preview(repository)
    authority.close()
    with _paused(
        _arguments(
            tmp_path,
            repository,
            "begin",
            "transition_marked",
            checkpoint=plan["checkpoint"],
        ),
        "transition_marked",
    ) as process:
        before = _snapshot(tmp_path, repository)
        attempted = _run(
            _arguments(tmp_path, repository, "activate", identity=waiter.claim_id)
        )
        assert attempted == {
            "activated": [],
            "waiter_state": "queued",
            "fencing_token": None,
        }
        assert _snapshot(tmp_path, repository) == before
        with peer.connection() as connection:
            assert (
                connection.execute("SELECT count(*) FROM checkout_regimes").fetchone()[
                    0
                ]
                == 0
            )
        assert process.stdin is not None
        process.stdin.write("continue\n")
        process.stdin.flush()
        output, errors = process.communicate(timeout=15)
        assert process.returncode == 0, errors
        assert json.loads(output)["regime"] == "workspace"
    assert peer.get_claim(waiter.claim_id) == waiter
    assert _run(
        _arguments(tmp_path, repository, "activate", identity=waiter.claim_id)
    ) == {"activated": [], "waiter_state": "queued", "fencing_token": None}


_LEGACY_TABLES = (
    "repositories",
    "repository_heads",
    "checkouts",
    "sessions",
    "tasks",
    "claims",
    "claim_scopes",
    "task_executions",
    "execution_checkpoints",
    "task_workflows",
    "publication_intents",
)


def _legacy_rows(store: ControlStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connection() as connection:
        return {
            name: [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")
            ]
            for name in _LEGACY_TABLES
        }


def test_real_g2_schema_upgrade_preserves_all_legacy_states_and_ref_dispositions(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    git_run: Callable[..., str],
) -> None:
    repository = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
    head = git_run(repository, "rev-parse", "HEAD")
    tree = git_run(repository, "rev-parse", "HEAD^{tree}")
    (tmp_path / "upgrade").mkdir(mode=0o700)
    store = ControlStore(tmp_path / "upgrade" / "control.sqlite3")
    with store.connection() as connection:
        assert apply_migrations(connection, CONTROL_MIGRATIONS[:27]) == 27
        for ordinal, (key, target, adapter) in enumerate(
            (
                ("a" * 64, "refs/heads/main", "local"),
                ("b" * 64, "refs/heads/release", "fixture"),
            )
        ):
            connection.execute(
                "INSERT INTO repositories(repository_id,repo_key,profile_id,"
                "display_name,git_common_dir,main_worktree_path,target_ref,"
                "integration_adapter,created_at,updated_at,last_seen_at) "
                "VALUES (?,?,'owner','legacy',?,?,?,?,1,1,1)",
                (
                    f"repo-{ordinal}",
                    key,
                    str(repository / ".git"),
                    str(repository),
                    target,
                    adapter,
                ),
            )
            connection.execute(
                "INSERT INTO repository_heads(repo_key,last_effective_time,"
                "created_at,updated_at) VALUES (?,1,1,1)",
                (key,),
            )
            connection.execute(
                "INSERT INTO checkouts(checkout_id,repository_id,repo_key,"
                "canonical_path,git_common_dir,path_case_insensitive,created_at,"
                "updated_at) VALUES (?,?,?,?,?,0,1,1)",
                (
                    f"checkout-{ordinal}",
                    f"repo-{ordinal}",
                    key,
                    str(repository),
                    str(repository / ".git"),
                ),
            )
        connection.execute(
            "INSERT INTO sessions(session_id,checkout_id,resume_token_hash,provider,"
            "model,state,opened_at,last_heartbeat_at,updated_at) "
            "VALUES ('legacy-session','checkout-0',?,'fixture','fixture',"
            "'disconnected',1,1,1)",
            ("c" * 64,),
        )
        states = (
            ("queued", "queued", "queued", None),
            ("active", "active_work", "running", "running"),
            ("expired", "expired", "operator_attention", "running"),
            ("publishing", "publishing", "running", "publishing"),
            ("integrating", "active_integration", "integrating", "published"),
            ("confirmed", "released", "completed", "published"),
            ("held", "released", "reviewing", "failed"),
        )
        for ordinal, (label, claim_state, task_state, execution_state) in enumerate(
            states
        ):
            mapping = ordinal % 2
            key = ("a" if mapping == 0 else "b") * 64
            claim_id, task_id = f"claim-{label}", f"task-{label}"
            token = None if claim_state == "queued" else ordinal + 1
            connection.execute(
                "INSERT INTO tasks(task_id,repository_id,repo_key,title,state,"
                "coordination_state,current_claim_id,current_fencing_token,session_id,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'legacy-session',1,1)",
                (
                    task_id,
                    f"repo-{mapping}",
                    key,
                    "retained legacy prompt",
                    task_state,
                    claim_state,
                    claim_id,
                    token,
                ),
            )
            connection.execute(
                "INSERT INTO claims(claim_id,task_id,task_attempt,repo_key,state,"
                "queue_sequence,fencing_token,lease_expires_at,terminal_expires_at,"
                "created_at,updated_at) VALUES (?,?,1,?,?,?,?,?,?,1,1)",
                (
                    claim_id,
                    task_id,
                    key,
                    claim_state,
                    ordinal + 1,
                    token,
                    9999999999999 if claim_state == "active_work" else None,
                    9999999999999 if claim_state in {"released", "expired"} else None,
                ),
            )
            connection.execute(
                "INSERT INTO claim_scopes(claim_id,ordinal,value,is_directory) "
                "VALUES (?,0,'source.py',0)",
                (claim_id,),
            )
            if execution_state is not None:
                execution_id = f"execution-{label}"
                connection.execute(
                    "INSERT INTO task_executions(execution_id,task_id,task_attempt,"
                    "claim_id,driver,state,worktree_path,base_oid,result_tree_id,"
                    "result_commit_id,created_at,updated_at) "
                    "VALUES (?,?,1,?,'fixture',?,?,?,?,?,1,1)",
                    (
                        execution_id,
                        task_id,
                        claim_id,
                        execution_state,
                        str(repository),
                        head,
                        tree,
                        head,
                    ),
                )
                connection.execute(
                    "INSERT INTO execution_checkpoints(execution_id,driver,"
                    "checkpoint_json,created_at,updated_at) VALUES (?,'fixture',?,1,1)",
                    (
                        execution_id,
                        json.dumps(
                            {"phase": "finished", "session": {"retained": label}}
                        ),
                    ),
                )
            if label in {"publishing", "integrating", "confirmed"}:
                task_ref = f"refs/llm/tasks/upgrade-{label}"
                git_run(repository, "update-ref", task_ref, head)
                connection.execute(
                    "INSERT INTO publication_intents(intent_id,idempotency_key,"
                    "claim_id,task_id,task_attempt,repo_key,fencing_token,patch_hash,"
                    "result_tree_id,result_commit_id,target_ref,task_ref,"
                    "operation_state,started_at,updated_at) "
                    "VALUES (?,?,?,?,1,?,?,?,?,?,'refs/heads/main',?,?,1,1)",
                    (
                        f"intent-{label}",
                        str(ordinal) * 64,
                        claim_id,
                        task_id,
                        key,
                        token,
                        "d" * 64,
                        tree,
                        head,
                        task_ref,
                        "confirmed" if label == "confirmed" else "side_effect_unknown",
                    ),
                )
            if label == "held":
                connection.execute(
                    "INSERT INTO task_workflows(task_id,attempt,execution_id,"
                    "proposal_json,status,created_at) "
                    "VALUES (?,1,?,'[]','awaiting_review',1)",
                    (task_id, "execution-held"),
                )
    before = _legacy_rows(store)
    refs = git_run(repository, "show-ref")
    assert store.initialize() == CONTROL_MIGRATIONS[-1].version
    assert _legacy_rows(store) == before
    with store.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM checkout_regimes").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM workspace_executions").fetchone()[
                0
            ]
            == 0
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        claim_column = next(
            row
            for row in connection.execute("PRAGMA table_info(task_executions)")
            if row["name"] == "claim_id"
        )
        assert claim_column["notnull"] == 1
    authority = PhysicalAuthority(
        store,
        "owner",
        "upgrade-test",
        tmp_path / "socket",
        root=tmp_path / "authority" / "v1",
    )
    store.authority = authority
    try:
        manager = CutoverManager(store, authority)
        plan = manager.preview(repository)
        assert plan["regime"] == "legacy" and plan["generation"] == 0
        assert {m["target_ref"] for m in plan["mappings"]} == {
            "refs/heads/main",
            "refs/heads/release",
        }
        assert {m["integration_adapter"] for m in plan["mappings"]} == {
            "local",
            "fixture",
        }
        assert all(
            any(p["provenance"] == "legacy_snapshot" for p in m["provenance"])
            for m in plan["mappings"]
        )
        dispositions = {
            (d["kind"], d["id"]): d["default"] for d in plan["dispositions"]
        }
        assert dispositions[("claim", "claim-queued")] == "retain_legacy"
        assert dispositions[("claim", "claim-active")] == "drain_or_recover"
        assert dispositions[("claim", "claim-expired")] == "recover"
        assert dispositions[("claim", "claim-publishing")] == "drain_or_recover"
        assert dispositions[("claim", "claim-integrating")] == "drain_or_recover"
        assert dispositions[("publication", "intent-confirmed")] == "retain_history"
        assert dispositions[("workflow", "task-held")] == "retain_history_and_proposal"
        for table, kind, column in (
            ("tasks", "task", 0),
            ("claims", "claim", 0),
            ("task_executions", "execution", 0),
            ("publication_intents", "publication", 0),
        ):
            assert all((kind, row[column]) in dispositions for row in before[table])
        assert {b["id"] for b in plan["blockers"]} >= {
            "claim-active",
            "claim-expired",
            "claim-publishing",
            "claim-integrating",
            "intent-publishing",
            "intent-integrating",
        }
        with pytest.raises(LlmCoordError) as blocked:
            manager.begin(repository, plan["checkpoint"])
        assert blocked.value.code is ErrorCode.CUTOVER_BLOCKED
        assert _legacy_rows(store) == before
        assert git_run(repository, "show-ref") == refs
        assert authority.regime(repository)["regime"] == "legacy"
    finally:
        authority.close()


# Exact G2 compatibility gate from main 1c98b2aa2e1532cc29290560f6052d78160636f0,
# src/llm_cli/workspace/authority.py:613-623. Keep it frozen: a current-reader
# test cannot establish that a deployed G2 writer rejects the new format.
_G2_MARKER_GATE = """
import json
import sys
from pathlib import Path
from llm_cli.errors import LlmCoordError
from llm_cli.workspace.authority import PhysicalCheckout
from llm_cli.workspace.installation import private_read, recovery_error

physical = PhysicalCheckout.inspect(Path(sys.argv[2]))
try:
    marker = private_read(Path(sys.argv[1]))
    try:
        if marker is not None and (
            marker.get("version") != 1
            or marker.get("execution_contract") != "claim_backed_v1"
            or marker.get("physical_key") != physical.key
            or marker.get("binding") != physical.binding
            or marker.get("state") not in {"clean", "dirty", "attention"}
            or type(marker.get("generation")) is not int
        ):
            raise recovery_error(
                "authority marker is unknown or has changed identity"
            )
        print(json.dumps({"code":"accepted"}))
    except LlmCoordError as exc:
        print(json.dumps({"code":exc.code.value}))
finally:
    physical.close()
"""


@pytest.mark.parametrize("boundary", ["transition_marked", "marker_clean"])
def test_frozen_g2_reader_refuses_new_marker_in_another_process(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    boundary: str,
) -> None:
    repository = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
    authority = _authority(tmp_path, repository, "owner")
    plan = CutoverManager(authority.store, authority).preview(repository)
    authority.close()
    with _paused(
        _arguments(
            tmp_path, repository, "begin", boundary, checkpoint=plan["checkpoint"]
        ),
        boundary,
    ):
        before = _snapshot(tmp_path, repository)
        physical = PhysicalCheckout.inspect(repository)
        try:
            marker = (
                tmp_path
                / "authority"
                / "v1"
                / "checkouts"
                / physical.key
                / "marker.json"
            )
        finally:
            physical.close()
        assert _run(
            [sys.executable, "-c", _G2_MARKER_GATE, str(marker), str(repository)]
        ) == {"code": ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED.value}
        assert _snapshot(tmp_path, repository) == before
