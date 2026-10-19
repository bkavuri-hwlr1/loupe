"""Ephemeral command environments preserve project variables without leaking values."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.config.loader import load_settings
from llm_cli.errors import LlmCoordError
from llm_cli.execution.checks import supervised_run
from llm_cli.execution.environment import (
    CommandEnvironment,
    SessionEnvironments,
    filtered_environment,
)
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import session_environment_params


@pytest.mark.parametrize("value", ["minimal", "session"])
def test_environment_setting(tmp_path: Path, value: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[agent]\ncommand_environment = "{value}"\n')
    assert load_settings(config).agent_command_environment == value


@pytest.mark.parametrize("value", ['"all"', "false", "[]", "{}", "42"])
def test_environment_setting_rejects_invalid_values(tmp_path: Path, value: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f"[agent]\ncommand_environment = {value}\n")
    with pytest.raises(LlmCoordError, match=r"agent\.command_environment"):
        load_settings(config)


def test_client_capture_opt_in_and_known_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.resolve("test", home=tmp_path, environ={})
    paths.ensure()
    monkeypatch.setenv("PROJECT_FEATURE", "enabled")
    monkeypatch.setenv("OPENAI_API_KEY", "opaque-provider-value")
    monkeypatch.setenv("LOUPE_INTERNAL", "internal-value")
    assert session_environment_params(paths) == {}
    paths.config_file.write_text('[agent]\ncommand_environment="session"\n')
    result = session_environment_params(paths)["command_environment"]
    assert isinstance(result, dict)
    assert result["PROJECT_FEATURE"] == "enabled"
    assert "OPENAI_API_KEY" not in result
    assert "LOUPE_INTERNAL" not in result


@pytest.mark.parametrize(
    "value", [[], {"bad=name": "private-canary"}, {"OK": 1}, {"OK": "a\0b"}]
)
def test_invalid_map_errors_do_not_echo_values(value: object) -> None:
    with pytest.raises(ValueError) as caught:
        filtered_environment(value)
    assert "private-canary" not in str(caught.value)


def test_registry_filters_registered_names_and_inspects_names_only() -> None:
    canary = "c9-opaque-private-value-58392"
    assert not contains_secret_material(canary)
    values, removed = filtered_environment(
        {
            "PROJECT_FEATURE": "enabled",
            "PROJECT_PASSWORD": canary,
            "CUSTOM_PROVIDER": "billing-value",
            "ANTHROPIC_AUTH_TOKEN": "known-value",
            "LLM_COORD_STATE_HOME": "daemon-value",
            "HOME": "terminal-home",
        },
        registered=("CUSTOM_PROVIDER",),
    )
    environment = CommandEnvironment(values, removed)
    registry = SessionEnvironments()
    registry.replace("one", environment)
    registry.replace("two", CommandEnvironment({"PROJECT_FEATURE": "other"}))
    assert registry.get("one").values["PROJECT_FEATURE"] == "enabled"
    assert registry.get("two").values["PROJECT_FEATURE"] == "other"
    plan = registry.plan("one")
    assert set(plan["removed_names"]) == {
        "CUSTOM_PROVIDER",
        "ANTHROPIC_AUTH_TOKEN",
        "LLM_COORD_STATE_HOME",
    }
    assert plan["screened_names"] == ["PROJECT_PASSWORD"]
    assert plan["sandbox_override_names"] == ["HOME"]
    assert canary not in repr(plan) + repr(environment) + repr(registry)
    registry.discard("one")
    assert environment.values == {}
    assert registry.plan("one")["state"] == "awaiting_session_environment"
    with pytest.raises(LlmCoordError, match="awaiting_session_environment"):
        registry.get("one")
    assert registry.get("two").values == {"PROJECT_FEATURE": "other"}


def test_isolated_rewrites_only_exact_hydrated_paths_and_root(tmp_path: Path) -> None:
    primary, private, snapshot = (
        tmp_path / name for name in ("primary", "private", "snapshot")
    )
    for root in (primary, private, snapshot):
        (root / "node_modules" / "bin").mkdir(parents=True)
        (root / "node_modules" / "bin" / "test").write_text("test")
    environment = CommandEnvironment(
        {
            "PWD": str(primary),
            "PATH": str(primary / "node_modules" / "bin") + os.pathsep + "/usr/bin",
            "PROJECT_SECRET_PATH": str(primary / "node_modules" / "bin" / "test"),
        },
        source_root=primary,
        hydrated_root=private,
        hydrated_paths=("node_modules",),
    )
    values, redactions = environment.capture(snapshot)
    assert values["PWD"] == str(snapshot)
    assert (
        values["PATH"]
        == str(snapshot / "node_modules" / "bin") + os.pathsep + "/usr/bin"
    )
    assert values["PROJECT_SECRET_PATH"] == str(
        snapshot / "node_modules" / "bin" / "test"
    )
    assert str(primary / "node_modules" / "bin" / "test") in redactions
    assert values["PROJECT_SECRET_PATH"] in redactions
    assert environment.plan()["rewritten_names"] == [
        "PATH",
        "PROJECT_SECRET_PATH",
        "PWD",
    ]
    # A concurrent reconnect may clear the registry; the launched child and its
    # redaction tuple stay protected by the atomic immutable capture.
    environment.invalidate()
    assert values["PROJECT_SECRET_PATH"] in redactions
    assert environment.values == {}
    with pytest.raises(LlmCoordError):
        environment.capture(snapshot)


@pytest.mark.parametrize("case", ["missing", "unregistered", "escape", "parent"])
def test_isolated_refuses_unresolved_paths(tmp_path: Path, case: str) -> None:
    primary, private, snapshot = (
        tmp_path / name for name in ("primary", "private", "snapshot")
    )
    for root in (primary, private, snapshot):
        root.mkdir()
        (root / "runtime").mkdir()
    target = primary / "runtime" / "test"
    if case == "missing":
        pass
    elif case == "escape":
        (private / "runtime" / "test").symlink_to(tmp_path)
        (snapshot / "runtime" / "test").mkdir()
    else:
        (private / "runtime" / "test").write_text("private")
        (snapshot / "runtime" / "test").write_text("private")
    if case == "parent":
        target = primary / "runtime" / ".." / "outside"
    environment = CommandEnvironment(
        {"CUSTOM_PATH": str(target)},
        source_root=primary,
        hydrated_root=private,
        hydrated_paths=() if case == "unregistered" else ("runtime",),
    )
    with pytest.raises(LlmCoordError, match="no exact hydrated equivalent"):
        environment.capture(snapshot)
    assert environment.plan()["unresolved_names"] == ["CUSTOM_PATH"]


def test_check_overrides_share_role_filters_and_sandbox_overrides(
    tmp_path: Path,
) -> None:
    environment = CommandEnvironment(
        {"PROJECT_MODE": "initial", "PROJECT_PASSWORD": "initial-private-value"},
        credential_names=("CUSTOM_BILLING",),
    )
    values, redactions = environment.capture(
        tmp_path,
        overrides={
            "PROJECT_MODE": "override",
            "DATABASE_URL": "opaque-db-private-value",
            "HOME": "untrusted-home",
            "TMP": "untrusted-temp",
            "TEMP": "untrusted-temp",
            "XDG_CACHE_HOME": "untrusted-cache",
            "CUSTOM_BILLING": "billing-private",
            "OPENAI_API_KEY": "known-provider-private",
            "LOUPE_SENTINEL": "loupe-private",
        },
    )
    assert values == {
        "PROJECT_MODE": "override",
        "PROJECT_PASSWORD": "initial-private-value",
        "DATABASE_URL": "opaque-db-private-value",
    }
    assert "initial-private-value" in redactions
    assert "opaque-db-private-value" in redactions


def test_held_runtime_paths_rewrite_against_frozen_generation(tmp_path: Path) -> None:
    primary, private, frozen, snapshot = (
        tmp_path / name for name in ("primary", "private", "frozen", "snapshot")
    )
    for root in (primary, private, frozen, snapshot):
        root.mkdir()
    # Current private runtime changed after hold; only the retained generation
    # and final check snapshot contain the original exact runtime equivalent.
    for root in (frozen, snapshot):
        (root / "runtime").mkdir()
        (root / "runtime" / "config").write_text("frozen")
    environment = CommandEnvironment(
        {
            "PROJECT_SECRET_PATH": str(primary / "runtime" / "config"),
            "PWD": str(primary),
        },
        source_root=primary,
        hydrated_root=private,
    )
    values, redactions = environment.capture(
        snapshot,
        runtime_root=frozen,
        runtime_paths=("runtime",),
    )
    assert values["PROJECT_SECRET_PATH"] == str(snapshot / "runtime" / "config")
    assert values["PWD"] == str(snapshot)
    assert values["PROJECT_SECRET_PATH"] in redactions


def test_primary_checkout_aliases_rewrite_to_exact_private_paths(
    tmp_path: Path,
) -> None:
    primary, private, snapshot = (
        tmp_path / name for name in ("primary", "private", "snapshot")
    )
    for root in (primary, private, snapshot):
        (root / "dep" / "bin").mkdir(parents=True)
    alias = tmp_path / "logical-checkout"
    alias.symlink_to(primary, target_is_directory=True)
    environment = CommandEnvironment(
        {"PWD": str(alias), "PATH": str(alias / "dep" / "bin")},
        source_root=primary,
        hydrated_root=private,
        hydrated_paths=("dep",),
    )
    values, _ = environment.capture(snapshot)
    assert values == {"PWD": str(snapshot), "PATH": str(snapshot / "dep" / "bin")}


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\u2028"])
def test_screening_uses_line_fragments_without_mutating_environment(
    tmp_path: Path, separator: str
) -> None:
    original = separator.join(("opaque-origin-a-f2e7", "opaque-origin-b-83ad"))
    override = separator.join(("opaque-override-a-d791", "opaque-override-b-e204"))
    environment = CommandEnvironment({"PROJECT_PASSWORD": original})
    values, redactions = environment.capture(
        tmp_path, overrides={"DATABASE_URL": override}
    )
    assert environment.values == {"PROJECT_PASSWORD": original}
    assert values == {"PROJECT_PASSWORD": original, "DATABASE_URL": override}
    assert {original, override, *original.splitlines(), *override.splitlines()} <= set(
        redactions
    )
    assert set(original.splitlines()) <= set(environment.sensitive_values)


@pytest.mark.parametrize(
    "case", ["lf", "crlf-read-boundary", "long-read-boundary", "capped", "lf-capped"]
)
def test_session_secret_screening_across_output_boundaries(
    tmp_path: Path, case: str
) -> None:
    first = "opaque-first-line-370dab"
    second = "opaque-second-line-c8e129"
    prefix = ""
    canary = first + "\n" + second
    cap = 65536
    if case == "crlf-read-boundary":
        canary = first + "\r\n" + second
        # CR ends the first possible 4096-byte read; LF starts the next.
        prefix = "p" * (4095 - len(first))
    elif case in {"long-read-boundary", "capped"}:
        canary = first + "z" * 9000 + second
        if case == "capped":
            cap = 4097
    elif case == "lf-capped":
        cap = len(first) + 1 + len(second) // 2
    assert not contains_secret_material(canary)
    environment = CommandEnvironment({"PROJECT_PASSWORD": canary})
    values, redactions = environment.capture(tmp_path)
    output_events: list[str] = []
    exit_code, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "import os; "
            "os.write(1, os.environ['PREFIX'].encode() + "
            "os.environ['PROJECT_PASSWORD'].encode() + b'\\nsafe-complete\\n')",
        ],
        cwd=tmp_path,
        env={**values, "PREFIX": prefix},
        deadline=time.monotonic() + 10,
        remaining=cap,
        cancelled=lambda: False,
        on_output=output_events.append,
        sensitive_values=redactions,
    )
    assert exit_code == 0 and stopped is None
    assert output == "".join(output_events)
    assert canary not in output
    for line in canary.splitlines():
        assert line not in output
    if case == "capped":
        # Never emit the prefix of a private value cut by the raw byte cap.
        assert output == "" and clipped
    elif case == "lf-capped":
        assert output == "[private value]\n" and clipped
        assert second[: len(second) // 2] not in output
    else:
        assert not clipped
        assert output.startswith(prefix + "[private value]")
        assert output.endswith("safe-complete\n")
    assert environment.values == {"PROJECT_PASSWORD": canary}


def test_rewritten_secret_paths_screen_original_and_effective_lines(
    tmp_path: Path,
) -> None:
    primary, private, snapshot = (
        tmp_path / name for name in ("primary", "private", "snapshot")
    )
    relative = Path("runtime") / "opaque-first-375db\nopaque-second-4ad81"
    for root in (primary, private, snapshot):
        (root / "runtime").mkdir(parents=True)
        (root / relative).write_text("runtime")
    original = str(primary / relative)
    environment = CommandEnvironment(
        {"PROJECT_SECRET_PATH": original},
        source_root=primary,
        hydrated_root=private,
        hydrated_paths=("runtime",),
    )
    values, redactions = environment.capture(snapshot)
    effective = str(snapshot / relative)
    assert values == {"PROJECT_SECRET_PATH": effective}
    assert environment.values == {"PROJECT_SECRET_PATH": original}
    expected = {original, effective, *original.splitlines(), *effective.splitlines()}
    assert expected <= set(redactions)
