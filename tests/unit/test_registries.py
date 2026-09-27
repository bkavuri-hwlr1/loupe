from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pytest

from llm_cli.agent.driver import DriverCapabilities, RunRequest, RunResult
from llm_cli.agent.registry import DriverRegistry
from llm_cli.agent.tools import ToolBroker
from llm_cli.providers.base import ModelTurn, ToolCallResult
from llm_cli.providers.registry import ProviderRegistry


@dataclass
class _Provider:
    model: str
    name: str = "example"

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> _Provider:
        del system, tools, state
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {}

    def send_user(self, text: str) -> ModelTurn:
        del text
        return ModelTurn(text="done")

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        del results
        return ModelTurn(text="done")

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        del results


class _Driver:
    name = "example_driver"
    capabilities = DriverCapabilities(resumable=True)

    def run(self, request: RunRequest, tools: ToolBroker) -> RunResult:
        del request, tools
        return RunResult(summary="done", answer="done")


def test_provider_registry_selects_an_adapter_and_model() -> None:
    registry = ProviderRegistry()
    registry.register("example", lambda model: _Provider(model or "default"))

    provider = registry.create("example", "code-model")

    assert provider.name == "example"
    assert provider.model == "code-model"


def test_provider_registry_rejects_an_unknown_adapter() -> None:
    registry = ProviderRegistry()
    registry.register("example", lambda model: _Provider(model or "default"))

    with pytest.raises(ValueError, match="not installed"):
        registry.create("missing")


def test_driver_registry_resolves_capabilities_without_constructing() -> None:
    registry = DriverRegistry()
    registry.register(
        _Driver.name,
        capabilities=_Driver.capabilities,
        factory=lambda _parameters: _Driver(),
    )

    assert registry.capabilities(_Driver.name) == _Driver.capabilities
    assert registry.create(_Driver.name, {}).name == _Driver.name


def test_driver_registry_rejects_a_factory_identity_mismatch() -> None:
    registry = DriverRegistry()
    registry.register(
        "other_name",
        capabilities=_Driver.capabilities,
        factory=lambda _parameters: _Driver(),
    )

    with pytest.raises(ValueError, match="mismatched"):
        registry.create("other_name", {})
