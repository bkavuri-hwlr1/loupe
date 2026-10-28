#!/usr/bin/env python3
"""Measure production outside-edit and isolated safe-point scans without billing.

Caps are never raised for the benchmark. By default each size is measured with
native file watchers off and on. A rejected 100,000-file isolated
workspace is reported as unsupported, not as a completed fast scan.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request
from llm_cli.workspace.external import MAX_PATHS
from llm_cli.workspace.identity import ObjectKind, content_identity
from llm_cli.workspace.isolated import (
    SourceCaptureCache,
    capture_source,
    capture_watched_source,
)

BODY = b"def fixture_value():\n    return 42\n"


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        timeout=120,
    )


def fixture(root: Path, count: int) -> list[str]:
    root.mkdir()
    paths = []
    for number in range(count):
        relative = f"source/{number // 1000:03}/{number:06}.py"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(BODY)
        paths.append(relative)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(
        root,
        "-c",
        "user.name=Scan fixture",
        "-c",
        "user.email=scan@example.invalid",
        "commit",
        "-qm",
        "synthetic source",
    )
    return paths


def timing(function: Any, repeats: int) -> dict[str, Any]:
    samples = []
    result: Any = None
    for _ in range(repeats):
        before = time.perf_counter()
        result = function()
        samples.append(1000 * (time.perf_counter() - before))
    return {
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "result": result,
    }


async def measure(
    count: int, repeats: int, *, watchers: str = "auto"
) -> dict[str, Any]:
    root = Path(
        tempfile.mkdtemp(
            prefix="loupe-scan-",
            dir="/private/tmp" if sys.platform == "darwin" else None,
        )
    ).resolve()
    os.chmod(root, 0o700)
    service: DaemonService | None = None
    record: dict[str, Any] = {
        "files": count,
        "file_bytes": len(BODY),
        "repeats": repeats,
        "watchers": watchers,
        "outside_path_cap": MAX_PATHS,
        "isolated_path_cap": 20000,
    }
    try:
        before = time.perf_counter()
        repository = root / "repository"
        paths = fixture(repository, count)
        record["fixture_seconds"] = time.perf_counter() - before
        app_paths = AppPaths.resolve(
            "scan",
            environ={
                "LLM_COORD_CONFIG_HOME": str(root / "config"),
                "LLM_COORD_DATA_HOME": str(root / "data"),
                "LLM_COORD_STATE_HOME": str(root / "state"),
                "LLM_COORD_RUNTIME_DIR": str(root / "run"),
            },
            home=root,
        )
        service = DaemonService(
            app_paths,
            Settings(profile_id="scan", workspace_watchers=watchers),
            asyncio.Event(),
            boot_id="scan-benchmark",
            authority_root=root / "authority" / "v1",
        )
        service.providers.register(
            "unused-scripted",
            lambda model: SimpleNamespace(name="unused-scripted", model=model),
        )
        service.initialize()

        async def rpc(method: str, **params: Any) -> Any:
            assert service is not None
            return await service.handle(
                Request.create(
                    request_id="scan-" + method,
                    method=method,
                    params=params,
                    profile_id="scan",
                )
            )

        await rpc("repo.add", path=str(repository))
        secret = "scan-disposable-resume"
        opened = await rpc(
            "session.open",
            path=str(repository),
            session_id="reader",
            resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
            provider="unused-scripted",
            model="unused",
            workspace="shared",
        )
        await rpc(
            "session.ack",
            session_id="reader",
            resume_secret=secret,
            sequence=opened["bootstrap_sequence"],
        )
        # Bulk-seed genuine read identities so fixture construction does not
        # dominate measurement with one transaction per synthetic model read.
        # The timed scanner uses the unchanged production code and SQL cap.
        identity = content_identity(ObjectKind.REGULAR, "100644", BODY)
        with service.store.connection() as connection:
            workspace = connection.execute(
                "SELECT workspace_id FROM sessions WHERE session_id='reader'"
            ).fetchone()[0]
            connection.executemany(
                "INSERT INTO workspace_read_observations("
                "session_id,workspace_id,relative_path,"
                "observed_kind,observed_mode,observed_digest,observed_at) "
                "VALUES ('reader',?,?,"
                "'regular','100644',?,?)",
                [
                    (workspace, path, identity, index)
                    for index, path in enumerate(paths)
                ],
            )
        # The daemon's own scanner, which keeps watcher cursors when enabled.
        scanner = service._external_scanner
        record["outside_cold"] = timing(lambda: scanner.scan_session("reader"), 1)
        record["outside_warm"] = timing(lambda: scanner.scan_session("reader"), repeats)
        with service.store.connection() as connection:
            record["outside_observed_paths"] = connection.execute(
                "SELECT count(*) FROM workspace_external_observations "
                "WHERE session_id='reader'"
            ).fetchone()[0]
        # Modify the most recently observed path, within the production window.
        (repository / paths[-1]).write_bytes(BODY + b"# external edit\n")
        record["outside_one_change"] = timing(lambda: scanner.scan_session("reader"), 1)
        # Capture is the scanner component of the safe point. Report it
        # separately even when the private-workspace hydration inventory cannot
        # admit this repository; it is never relabelled an end-to-end result.
        capture_cache = SourceCaptureCache()

        def capture() -> dict[str, int]:
            assert service is not None
            result = (
                capture_watched_source(
                    repository,
                    app_paths.candidate_dir,
                    protected_paths=(),
                    cache=capture_cache,
                    watch=service.watchers,
                )
                if watchers == "auto"
                else capture_source(
                    repository,
                    app_paths.candidate_dir,
                    cache=capture_cache,
                    defer_changed=True,
                )
            )
            return {
                "captured_paths": len(result),
                "deferred_paths": len(result.deferred),
            }

        try:
            record["isolated_capture_cold"] = timing(capture, 1)
            record["isolated_capture_warm"] = timing(capture, repeats)
        except LlmCoordError as exc:
            if "20,000-path budget" not in exc.message:
                raise
            record["isolated_capture"] = {
                "status": "unsupported",
                "error": exc.message,
                "scanned_paths": 0,
            }
        before = time.perf_counter()
        try:
            isolated = await rpc(
                "session.open",
                path=str(repository),
                session_id="isolated",
                resume_token_hash=hashlib.sha256(
                    (secret + "-isolated").encode()
                ).hexdigest(),
                provider="unused-scripted",
                model="unused",
                workspace="isolated",
            )
        except LlmCoordError as exc:
            if (
                "20,000-path budget" not in exc.message
                and "isolated hydration inventory is incomplete:" not in exc.message
            ):
                raise
            record["isolated"] = {
                "status": "unsupported",
                "error": exc.message,
                "open_seconds": time.perf_counter() - before,
                "scanned_paths": 0,
            }
        else:
            record["isolated_open_seconds"] = time.perf_counter() - before
            workspace_id = isolated["session"]["workspace_id"]
            service.shared_runner._prepare_isolated(workspace_id)

            # This is the full safe-point synchronization used by the coding
            # runner: source enumeration/cache, incoming batches and manifest.
            def synchronize() -> dict[str, Any]:
                assert service is not None
                result = service.shared_runner.isolated_sync.synchronize(workspace_id)
                return {
                    "applied": len(result.applied),
                    "deferred": len(result.deferred),
                    "conflicted": len(result.conflicted),
                }

            record["isolated_cold_safe_point"] = timing(synchronize, 1)
            record["isolated_warm_safe_point"] = timing(synchronize, repeats)
            (repository / paths[-1]).write_bytes(BODY + b"# second external edit\n")
            record["isolated_one_change"] = timing(synchronize, 1)
            record["isolated"] = {"status": "complete", "scanned_paths": count}
            await rpc(
                "session.close",
                session_id="isolated",
                resume_secret=secret + "-isolated",
            )
        await rpc("session.close", session_id="reader", resume_secret=secret)
    finally:
        if service:
            service.close()
        shutil.rmtree(root)
    return record


async def measure_growth(
    count: int = 20000, repeats: int = 5, *, initial_files: int = 7000
) -> dict[str, Any]:
    """Reach a real large safe point by ordinary post-admission source growth."""
    root = Path(
        tempfile.mkdtemp(
            prefix="loupe-scan-growth-",
            dir="/private/tmp" if sys.platform == "darwin" else None,
        )
    ).resolve()
    os.chmod(root, 0o700)
    initial = min(initial_files, count)
    repository = root / "repository"
    fixture(repository, initial)
    paths = AppPaths.resolve(
        "growth",
        environ={
            "LLM_COORD_CONFIG_HOME": str(root / "config"),
            "LLM_COORD_DATA_HOME": str(root / "data"),
            "LLM_COORD_STATE_HOME": str(root / "state"),
            "LLM_COORD_RUNTIME_DIR": str(root / "run"),
        },
        home=root,
    )
    service = DaemonService(
        paths,
        Settings(profile_id="growth"),
        asyncio.Event(),
        boot_id="scan-growth",
        authority_root=root / "authority" / "v1",
    )
    service.providers.register(
        "unused-scripted",
        lambda model: SimpleNamespace(name="unused-scripted", model=model),
    )
    service.initialize()

    async def rpc(method: str, **params: Any) -> Any:
        return await service.handle(
            Request.create(
                request_id="growth-" + method,
                method=method,
                params=params,
                profile_id="growth",
            )
        )

    record: dict[str, Any] = {
        "initial_files": initial,
        "target_files": count,
        "growth_step_files": 45,
        "status": "failed",
    }
    try:
        await rpc("repo.add", path=str(repository))
        secret = "growth-disposable-resume"
        opened = await rpc(
            "session.open",
            path=str(repository),
            session_id="growth",
            resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
            provider="unused-scripted",
            model="unused",
            workspace="isolated",
        )
        await rpc(
            "session.ack",
            session_id="growth",
            resume_secret=secret,
            sequence=opened["bootstrap_sequence"],
        )
        workspace_id = opened["session"]["workspace_id"]
        private = Path(opened["workspace"]["canonical_path"])
        service.shared_runner._prepare_isolated(workspace_id)

        def synchronize() -> dict[str, int]:
            report = service.shared_runner.isolated_sync.synchronize(workspace_id)
            if report.deferred or report.conflicted:
                raise RuntimeError("growth safe point deferred or conflicted")
            return {"applied": len(report.applied), "deferred": 0, "conflicted": 0}

        started = time.monotonic()
        steps = 0
        for first in range(initial, count, 45):
            for number in range(first, min(first + 45, count)):
                path = repository / f"source/{number // 1000:03}/{number:06}.py"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(BODY)
            synchronize()
            steps += 1
            if steps % 25 == 0:
                print(
                    json.dumps(
                        {
                            "growth_files": min(first + 45, count),
                            "steps": steps,
                            "elapsed_seconds": time.monotonic() - started,
                        }
                    ),
                    flush=True,
                )
        record["growth_seconds"] = time.monotonic() - started
        record["growth_steps"] = steps
        with service.store.connection() as connection:
            manifest = json.loads(
                connection.execute(
                    "SELECT manifest_json FROM isolated_sync_state "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()[0]
            )
        accepted = sum(entry["kind"] == "regular" for entry in manifest.values())
        if accepted != count:
            raise RuntimeError(f"growth accepted only {accepted} of {count} paths")
        record["accepted_manifest_paths"] = accepted
        record["accepted_manifest_entries"] = len(manifest)
        record["full_safe_point_warm"] = timing(synchronize, repeats)
        last = f"source/{(count - 1) // 1000:03}/{count - 1:06}.py"
        (repository / last).write_bytes(BODY + b"# growth external edit\n")
        record["full_safe_point_one_change"] = timing(synchronize, 1)
        if (private / last).read_bytes() != BODY + b"# growth external edit\n":
            raise RuntimeError("growth safe point did not install the external edit")
        # The same admitted checkout with watchers off: every safe point scans
        # the whole primary source, as before C12.
        service.shared_runner.watchers = None
        record["unwatched_safe_point_warm"] = timing(synchronize, repeats)
        first = "source/000/000000.py"
        (repository / first).write_bytes(BODY + b"# unwatched external edit\n")
        record["unwatched_safe_point_one_change"] = timing(synchronize, 1)
        if (private / first).read_bytes() != BODY + b"# unwatched external edit\n":
            raise RuntimeError("unwatched safe point did not install the edit")
        record["database_bytes"] = sum(
            path.stat().st_size for path in paths.data_dir.glob("*.sqlite3*")
        )
        record["status"] = "complete"
        await rpc("session.close", session_id="growth", resume_secret=secret)
    except (LlmCoordError, ValueError, RuntimeError) as exc:
        record["failure"] = str(exc)
    finally:
        service.close()
        shutil.rmtree(root)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, nargs="+", default=[20000, 100000])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--watchers",
        choices=("auto", "off", "both"),
        default="both",
        help="measure with native file watchers, without them, or both",
    )
    parser.add_argument("--output", type=Path, default=Path("scan-scale-result.json"))
    options = parser.parse_args()
    if any(count < 1 for count in options.files) or options.repeats < 1:
        parser.error("file counts and repeats must be positive")
    modes = ("off", "auto") if options.watchers == "both" else (options.watchers,)
    record = {"platform": platform.platform(), "python": sys.version, "results": []}
    for count in options.files:
        for mode in modes:
            result = asyncio.run(measure(count, options.repeats, watchers=mode))
            record["results"].append(result)
            options.output.parent.mkdir(parents=True, exist_ok=True)
            options.output.write_text(json.dumps(record, indent=2) + "\n")
            print(json.dumps(result, indent=2), flush=True)
    if 20000 in options.files:
        record["growth_20000"] = asyncio.run(measure_growth(20000, options.repeats))
        options.output.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record["growth_20000"], indent=2), flush=True)
        if record["growth_20000"]["status"] != "complete":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
