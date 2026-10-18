"""Rebuilds bound effective paths rather than a checkout's event history."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from test_knowledge_index import _index
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.knowledge import indexer
from llm_cli.storage.connection import immediate_transaction
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.identity import REGULAR_MODE, identify_path


def _batch(
    env: BatchEnvironment,
    batch_id: str,
    sequence: int,
    files: list[dict[str, Any]],
    *,
    state: str = "published",
    workspace: str | None = None,
    checkout: str | None = None,
) -> None:
    with env.store.connection() as con:
        owner = con.execute(
            "SELECT checkout_id FROM workspaces WHERE workspace_id=?",
            (env.workspace_id,),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO workspace_batches(batch_id,session_id,workspace_id,"
            "checkout_id,canonical_path,git_common_dir,request_json,state,"
            "workspace_revision,checkout_event_sequence,created_at,updated_at) "
            "VALUES (?,'first',?,?,?,?,?,?,1,?,0,0)",
            (
                batch_id,
                workspace or env.workspace_id,
                checkout or owner,
                str(env.repository),
                str(env.repository / ".git"),
                json.dumps({"files": files}),
                state,
                sequence,
            ),
        )


def _file(
    path: str, label: str, *, kind: str = "regular", mode: str | None = None
) -> dict[str, Any]:
    return {
        "relative_path": path,
        "content_hash": label + "-artifact" if kind == "regular" else None,
        "result": {
            "kind": kind,
            "mode": mode
            if mode is not None
            else REGULAR_MODE
            if kind == "regular"
            else "",
            "digest": label + "-identity",
        },
    }


def _external(
    env: BatchEnvironment,
    checkout: str,
    sequence: int,
    path: str,
    label: str,
    *,
    kind: str = "regular",
    mode: str | None = None,
) -> None:
    identity_mode = (
        mode if mode is not None else REGULAR_MODE if kind == "regular" else ""
    )
    identity = (
        "opaque"
        if kind == "opaque"
        else json.dumps([kind, identity_mode, label + "-identity"])
    )
    version = f"{checkout}:{sequence}:{path}"
    with env.store.connection() as con, immediate_transaction(con):
        con.execute(
            "INSERT INTO knowledge_external_versions VALUES (?,?,?,?,?)",
            (
                version,
                checkout,
                path,
                identity,
                label + "-artifact" if kind == "regular" else None,
            ),
        )
        con.execute(
            "INSERT INTO knowledge_outbox(checkout_id,event_sequence,version_id,"
            "created_at) VALUES (?,?,?,0)",
            (checkout, sequence, version),
        )


@pytest.mark.parametrize("history_kind", ["batch", "external"])
def test_rebuild_after_twenty_thousand_historical_publications(
    batch_env: BatchEnvironment, tmp_path: Path, history_kind: str
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    original = index.build(checkout)
    assert original["state"] == "ready", original
    receipt = BatchFile(
        "a.txt",
        identify_path(env.repository / "a.txt"),
        b"longlivedhistorymarker\n",
        REGULAR_MODE,
    )
    assert env.publisher.publish(
        "history-template", "first", (receipt,), ("*",)
    ).state == ("published")
    with env.store.connection() as con, immediate_transaction(con):
        template = con.execute(
            "SELECT * FROM workspace_batches WHERE batch_id='history-template'"
        ).fetchone()
        start = int(template["checkout_event_sequence"]) + 1
        stop = start + 20_001
        if history_kind == "batch":
            con.executemany(
                "INSERT INTO workspace_batches SELECT ?,session_id,workspace_id,"
                "checkout_id,canonical_path,git_common_dir,request_json,"
                "observations_json,state,workspace_revision,?,created_at,updated_at "
                "FROM workspace_batches WHERE batch_id='history-template'",
                ((f"history-{sequence}", sequence) for sequence in range(start, stop)),
            )
            con.executemany(
                "INSERT INTO knowledge_outbox(checkout_id,event_sequence,batch_id,"
                "created_at) VALUES (?,?,?,0)",
                (
                    (checkout, sequence, f"history-{sequence}")
                    for sequence in range(start, stop)
                ),
            )
        else:
            item = json.loads(template["request_json"])["files"][0]
            identity = json.dumps(["regular", REGULAR_MODE, item["result"]["digest"]])
            con.executemany(
                "INSERT INTO knowledge_external_versions VALUES (?,?,?,?,?)",
                (
                    (
                        f"history-{sequence}",
                        checkout,
                        "a.txt",
                        identity,
                        item["content_hash"],
                    )
                    for sequence in range(start, stop)
                ),
            )
            con.executemany(
                "INSERT INTO knowledge_outbox(checkout_id,event_sequence,version_id,"
                "created_at) VALUES (?,?,?,0)",
                (
                    (checkout, sequence, f"history-{sequence}")
                    for sequence in range(start, stop)
                ),
            )
        con.execute(
            "UPDATE checkout_heads SET next_event_sequence=? WHERE checkout_id=?",
            (stop - 1, checkout),
        )
    started = time.monotonic()
    rebuilt = index.build(checkout, rebuild=True)
    elapsed = time.monotonic() - started
    assert rebuilt["state"] == "ready", rebuilt
    assert elapsed < 30
    assert rebuilt["generation"] != original["generation"]
    result = index.search_lexical(checkout, "longlivedhistorymarker")["results"][0]
    assert result["indexed_revision"] == f"checkout-event:{stop - 1}"
    assert not result["stale"]


def test_receipts_select_latest_path_across_sources_and_cutoff(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    initial_paths = [
        "external-wins.txt",
        "shared-wins.txt",
        "external-deleted.txt",
        "batch-deleted.txt",
        "opaque.txt",
        "cutoff.txt",
        "tie.txt",
        "executable.txt",
        "became-link.txt",
    ]
    _batch(env, "old", 1, [_file(path, "old") for path in initial_paths])
    _external(env, checkout, 2, "shared-wins.txt", "external")
    _external(env, checkout, 3, "external-wins.txt", "external")
    _batch(env, "new-shared", 4, [_file("shared-wins.txt", "shared")])
    _external(env, checkout, 5, "external-deleted.txt", "deleted", kind="absent")
    _batch(env, "delete", 6, [_file("batch-deleted.txt", "deleted", kind="absent")])
    _external(env, checkout, 7, "opaque.txt", "redacted", kind="opaque")
    _batch(env, "cutoff", 8, [_file("cutoff.txt", "before-cutoff")])
    _external(env, checkout, 12, "cutoff.txt", "after-cutoff")
    _batch(env, "executable", 8, [_file("executable.txt", "executable", mode="100755")])
    _external(
        env, checkout, 8, "became-link.txt", "link", kind="symlink", mode="120000"
    )
    _batch(env, "tie", 9, [_file("tie.txt", "shared")])
    _external(env, checkout, 9, "tie.txt", "external")
    # A failed batch and an isolated workspace cannot contribute accepted source.
    _batch(env, "failed", 10, [_file("shared-wins.txt", "failed")], state="diverged")
    with env.store.connection() as con:
        con.execute(
            "INSERT INTO workspaces SELECT 'isolated-receipts',checkout_id,"
            "'linked_worktree','isolated',state,state_version,canonical_path||'-isolated',"
            "git_dir,workspace_id,workspace_epoch,workspace_revision,local_generation,"
            "incoming_sequence,quarantine_reason,created_at,updated_at,"
            "last_verified_at "
            "FROM workspaces WHERE workspace_id=?",
            (env.workspace_id,),
        )
    _batch(
        env,
        "isolated",
        10,
        [_file("shared-wins.txt", "isolated")],
        workspace="isolated-receipts",
    )
    receipts = index._receipts(checkout, sequence=10)
    expected = {
        "external-wins.txt": (
            3,
            "external-artifact",
            "external-identity",
            REGULAR_MODE,
        ),
        "shared-wins.txt": (4, "shared-artifact", "shared-identity", REGULAR_MODE),
        "external-deleted.txt": (5, None, None, ""),
        "batch-deleted.txt": (6, None, None, ""),
        "opaque.txt": (7, None, None, None),
        "cutoff.txt": (
            8,
            "before-cutoff-artifact",
            "before-cutoff-identity",
            REGULAR_MODE,
        ),
        "tie.txt": (9, "external-artifact", "external-identity", REGULAR_MODE),
        "executable.txt": (8, "executable-artifact", "executable-identity", "100755"),
        "became-link.txt": (8, None, None, "120000"),
    }
    assert {
        path: (seq, artifact, identity, mode)
        for seq, path, artifact, identity, mode in receipts
    } == expected
    assert receipts == sorted(receipts, key=lambda item: (item[0], item[1]))
    with env.store.connection() as con:
        selected = con.execute(
            "SELECT * FROM knowledge_outbox WHERE event_sequence IN (3,7,12)"
        ).fetchall()
    assert index._receipts(checkout, outbox_rows=selected) == [
        (
            3,
            "external-wins.txt",
            "external-artifact",
            "external-identity",
            REGULAR_MODE,
        ),
        (7, "opaque.txt", None, None, None),
        (
            12,
            "cutoff.txt",
            "after-cutoff-artifact",
            "after-cutoff-identity",
            REGULAR_MODE,
        ),
    ]


def test_receipt_budget_counts_distinct_latest_paths(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    monkeypatch.setattr(indexer, "MAX_FILES", 2)
    _batch(batch_env, "too-many", 1, [_file(f"{i}.txt", str(i)) for i in range(3)])
    with pytest.raises(ValueError, match="path budget"):
        index._receipts(checkout, sequence=1)


def test_rebuild_preserves_accepted_delete(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    deletion = BatchFile("a.txt", identify_path(env.repository / "a.txt"), None, "")
    assert env.publisher.publish(
        "delete-indexed", "first", (deletion,), ("*",)
    ).state == ("published")
    assert index.build(checkout, rebuild=True)["state"] == "ready"
    with index.store.connection() as con:
        assert (
            con.execute(
                "SELECT 1 FROM knowledge_documents WHERE relative_path='a.txt'"
            ).fetchone()
            is None
        )
    assert not (env.repository / "a.txt").exists()


def test_receipts_never_cross_checkout_or_selected_outbox_boundaries(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    repository = env.store.get_repository_by_key("a" * 64)
    assert repository is not None
    sibling = env.store.ensure_checkout(
        repository=repository,
        canonical_path=str(tmp_path / "other-checkout"),
        git_common_dir=str(env.repository / ".git"),
    )
    other_workspace = env.store.ensure_shared_workspace(sibling)
    _batch(env, "selected", 1, [_file("selected.txt", "selected")])
    _batch(env, "not-selected", 2, [_file("not-selected.txt", "not-selected")])
    _batch(
        env,
        "other-checkout",
        3,
        [_file("other-checkout.txt", "other")],
        checkout=sibling.checkout_id,
        workspace=other_workspace.workspace_id,
    )
    _batch(
        env,
        "wrong-workspace",
        4,
        [_file("wrong-workspace.txt", "wrong")],
        workspace=other_workspace.workspace_id,
    )
    _external(env, sibling.checkout_id, 1, "other-external.txt", "other")
    with env.store.connection() as con, immediate_transaction(con):
        con.execute(
            "INSERT INTO knowledge_outbox(checkout_id,event_sequence,batch_id,"
            "created_at) VALUES (?,1,'selected',0)",
            (checkout,),
        )
        # Even a mismatched immutable version reference cannot cross checkouts.
        con.execute(
            "INSERT INTO knowledge_outbox(checkout_id,event_sequence,version_id,"
            "created_at) SELECT ?,5,version_id,0 FROM knowledge_external_versions "
            "WHERE checkout_id=?",
            (checkout, sibling.checkout_id),
        )
        all_rows = con.execute("SELECT * FROM knowledge_outbox").fetchall()
    assert index._receipts(checkout, sequence=10) == [
        (1, "selected.txt", "selected-artifact", "selected-identity", REGULAR_MODE),
        (
            2,
            "not-selected.txt",
            "not-selected-artifact",
            "not-selected-identity",
            REGULAR_MODE,
        ),
    ]
    assert index._receipts(checkout, outbox_rows=all_rows) == [
        (1, "selected.txt", "selected-artifact", "selected-identity", REGULAR_MODE)
    ]
