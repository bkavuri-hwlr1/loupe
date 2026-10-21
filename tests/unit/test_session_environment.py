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
from llm_cli.workspace.runtime import private_output_values


@pytest.mark.parametrize(
    "name",
    [
        "DB_PASS",
        "SMTP_PASS",
        "MAIL_PWD",
        "SENTRY_DSN",
        "SLACK_WEBHOOK_URL",
        "ENCRYPTION_KEY",
        "SIGNING_KEY",
        "JWT_KEY",
        "STRIPE_SK",
        "DOCKER_AUTH_CONFIG",
        "mail_PwD",
    ],
)
def test_common_project_credential_names_screen_signature_free_values(
    tmp_path: Path, name: str
) -> None:
    canary = "opaque-project-credential-a8265"
    assert not contains_secret_material(canary)
    environment = CommandEnvironment({name: canary})
    values, protected = environment.capture(tmp_path)
    assert values == {name: canary}
    assert canary in protected
    assert environment.plan()["screened_names"] == [name]
    output_events: list[str] = []
    status, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ[os.environ['PRINT_NAME']]); print('safe')",
        ],
        cwd=tmp_path,
        env={**values, "PRINT_NAME": name},
        deadline=time.monotonic() + 10,
        remaining=65536,
        cancelled=lambda: False,
        on_output=output_events.append,
        sensitive_values=protected,
    )
    assert status == 0 and stopped is None and not clipped
    assert output == "".join(output_events) == "[private value]\nsafe\n"
    assert canary not in repr(environment.plan())


@pytest.mark.parametrize(
    "name", ["PIP_INDEX_URL", "CELERY_BROKER_URL", "Mixed_Endpoint"]
)
@pytest.mark.parametrize("password", ["opaque:pw@fragment-a162", "pw"])
def test_arbitrary_url_names_screen_decoded_passwords(
    tmp_path: Path, name: str, password: str
) -> None:
    from urllib.parse import quote

    body = f"hTtPs://usr:{quote(password, safe='')}@packages.invalid/simple"
    assert not contains_secret_material(body)
    environment = CommandEnvironment({name: body})
    values, protected = environment.capture(tmp_path)
    assert values == {name: body}
    assert {body, password} <= set(protected)
    assert "usr" not in protected
    assert environment.plan()["screened_names"] == [name]
    assert body not in repr(environment.plan()) and password not in repr(environment)
    output_events: list[str] = []
    status, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "import os; from urllib.parse import unquote, urlsplit; "
            "value = os.environ[os.environ['PRINT_NAME']]; "
            "print(value); print(urlsplit(value).password); "
            "print(unquote(urlsplit(value).password)); print('safe')",
        ],
        cwd=tmp_path,
        env={**values, "PRINT_NAME": name},
        deadline=time.monotonic() + 10,
        remaining=65536,
        cancelled=lambda: False,
        on_output=output_events.append,
        sensitive_values=protected,
    )
    assert status == 0 and stopped is None and not clipped
    assert output == "".join(output_events) == "[private value]\n" * 3 + "safe\n"


def test_url_overrides_keep_original_and_effective_passwords_private(
    tmp_path: Path,
) -> None:
    original = "https://usr:origin%3Apw-a627@packages.invalid/simple"
    replacement = "amqp://usr:override%40pw-b173@queue.invalid/app"
    environment = CommandEnvironment({"PIP_INDEX_URL": original})
    values, protected = environment.capture(
        tmp_path, overrides={"PIP_INDEX_URL": replacement}
    )
    assert values == {"PIP_INDEX_URL": replacement}
    assert environment.values == {"PIP_INDEX_URL": original}
    assert {original, replacement, "origin:pw-a627", "override@pw-b173"} <= set(
        protected
    )


@pytest.mark.parametrize("separator", [" ", "\n", "\r\n", "\t"])
def test_each_extra_index_url_and_password_is_screened(
    tmp_path: Path, separator: str
) -> None:
    first = "https://usr:first%3Apw-a627@first.invalid/simple"
    second = "https://usr:second%40pw-b173@second.invalid/simple"
    body = separator.join((first, second))
    assert not contains_secret_material(body)
    environment = CommandEnvironment({"PIP_EXTRA_INDEX_URL": body})
    values, protected = environment.capture(tmp_path)
    assert values == {"PIP_EXTRA_INDEX_URL": body}
    assert {body, first, second, "first:pw-a627", "second@pw-b173"} <= set(protected)
    assert environment.plan()["screened_names"] == ["PIP_EXTRA_INDEX_URL"]
    output_events: list[str] = []
    status, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "import os\nfrom urllib.parse import unquote, urlsplit\n"
            "for value in os.environ['PIP_EXTRA_INDEX_URL'].split():\n"
            " print(value)\n print(urlsplit(value).password)\n"
            " print(unquote(urlsplit(value).password))\n"
            "print('safe')",
        ],
        cwd=tmp_path,
        env=values,
        deadline=time.monotonic() + 10,
        remaining=65536,
        cancelled=lambda: False,
        on_output=output_events.append,
        sensitive_values=protected,
    )
    assert status == 0 and stopped is None and not clipped
    assert output == "".join(output_events) == "[private value]\n" * 6 + "safe\n"


def test_malformed_url_component_does_not_hide_later_credentials(
    tmp_path: Path,
) -> None:
    endpoint = "https://usr:later%3Apw-c932@packages.invalid/simple"
    environment = CommandEnvironment(
        {"PROJECT_ENDPOINTS": "https://[malformed " + endpoint}
    )
    _, protected = environment.capture(tmp_path)
    assert {endpoint, "later:pw-c932"} <= set(protected)
    assert environment.plan()["screened_names"] == ["PROJECT_ENDPOINTS"]


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("COMPASS", "north"),
        ("BYPASS_FLAG", "true"),
        ("MONKEY", "ordinary-value"),
        ("STRIPE_SKU", "product-number"),
        ("WEBHOOKS_ENABLED", "true"),
        ("PIP_INDEX_URL", "https://packages.invalid/simple"),
        ("PROJECT_ENDPOINT", "https://[malformed"),
        ("PROJECT_ENDPOINT", "not-a-url"),
    ],
)
def test_nonsecret_names_and_uncredentialed_urls_are_not_screened(
    tmp_path: Path, name: str, body: str
) -> None:
    environment = CommandEnvironment({name: body})
    values, protected = environment.capture(tmp_path)
    assert values == {name: body} and protected == ()
    assert environment.plan()["screened_names"] == []


def test_runtime_url_credentials_preserve_short_password_and_username_floor(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "FIRST=https://usr:pw@host.invalid/a\n"
        "SECOND=https://longusername:p%40ss@host.invalid/b\n"
        "THIRD=https://long%75sername:%70%77%64@host.invalid/c\n"
    )
    values = set(private_output_values(tmp_path, (".env",)))
    assert {"pw", "p@ss", "p%40ss", "longusername", "long%75sername"} <= values
    assert "usr" not in values
    source_values = set(private_output_values(tmp_path, (), source_paths=(".env",)))
    assert not {"pw", "p@ss", "pwd", "usr", "p%40ss"} & source_values
    assert {"longusername", "long%75sername", "%70%77%64"} <= source_values


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


@pytest.mark.parametrize("source_exists", [False, True])
def test_shared_rewrites_snapshot_paths_including_new_proposal_files(
    tmp_path: Path, source_exists: bool
) -> None:
    primary, snapshot = tmp_path / "primary", tmp_path / "snapshot"
    primary.mkdir()
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "new-config").write_text("proposal")
    if source_exists:
        (primary / "src").mkdir()
        (primary / "new-config").write_text("checkout")
    alias = tmp_path / "logical-checkout"
    alias.symlink_to(primary, target_is_directory=True)
    environment = CommandEnvironment(
        {
            "PWD": str(alias),
            "PYTHONPATH": os.pathsep.join((str(alias / "src"), "", "/usr/lib")),
            "PROJECT_CONFIG": str(primary / "new-config"),
        },
        source_root=primary,
    )
    values, _ = environment.capture(snapshot)
    assert values == {
        "PWD": str(snapshot),
        "PYTHONPATH": os.pathsep.join((str(snapshot / "src"), "", "/usr/lib")),
        "PROJECT_CONFIG": str(snapshot / "new-config"),
    }
    assert environment.plan()["unresolved_names"] == []


@pytest.mark.parametrize(
    "case", ["missing", "live-source", "no-read-grant", "sibling", "dependency-parent"]
)
def test_shared_refuses_paths_without_an_exact_permitted_snapshot_equivalent(
    tmp_path: Path, case: str
) -> None:
    primary, snapshot = tmp_path / "primary", tmp_path / "snapshot"
    (primary / ".venv/bin").mkdir(parents=True)
    (primary / ".venv/other/bin").mkdir(parents=True)
    (primary / "config").write_text("live source")
    snapshot.mkdir()
    readable: tuple[Path, ...] = ()
    component = primary / "config"
    if case == "live-source":
        (snapshot / "config").symlink_to(component)
        readable = (primary / ".venv",)
    elif case in {"no-read-grant", "sibling"}:
        component = primary / ".venv/bin"
        link = primary / ".venv/other" if case == "sibling" else primary / ".venv"
        (snapshot / ".venv").symlink_to(link, target_is_directory=True)
        if case == "sibling":
            readable = (primary / ".venv",)
    elif case == "dependency-parent":
        component = primary / ".venv/../config"
        (snapshot / ".venv").symlink_to(primary / ".venv", target_is_directory=True)
        readable = (primary / ".venv",)
    environment = CommandEnvironment(
        {"CUSTOM_PATH": str(component)}, source_root=primary
    )
    with pytest.raises(LlmCoordError, match="no exact snapshot equivalent"):
        environment.capture(snapshot, readable_roots=readable)
    assert environment.plan()["unresolved_names"] == ["CUSTOM_PATH"]


def test_shared_dependency_path_uses_only_its_exact_readable_link(
    tmp_path: Path,
) -> None:
    primary, snapshot = tmp_path / "primary", tmp_path / "snapshot"
    (primary / ".venv/bin").mkdir(parents=True)
    snapshot.mkdir()
    (snapshot / ".venv").symlink_to(primary / ".venv", target_is_directory=True)
    environment = CommandEnvironment(
        {"PATH": os.pathsep.join((str(primary / ".venv/bin"), "/usr/bin", ""))},
        source_root=primary,
    )
    values, _ = environment.capture(snapshot, readable_roots=(primary / ".venv",))
    assert values["PATH"] == os.pathsep.join(
        (str(snapshot / ".venv/bin"), "/usr/bin", "")
    )
    assert environment.plan()["rewritten_names"] == ["PATH"]
