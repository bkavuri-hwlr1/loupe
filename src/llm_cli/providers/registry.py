"""Registry for model-provider adapters used by the built-in harness."""

from __future__ import annotations

import inspect
from collections.abc import Callable

from llm_cli.coordination.models import EFFORT_LEVELS
from llm_cli.providers.base import ChatProvider

ProviderFactory = Callable[..., ChatProvider]


class ProviderRegistry:
    """Construct providers from durable, user-selectable adapter identities."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        if not name or len(name) > 64:
            raise ValueError("provider registry name is invalid")
        if name in self._factories:
            raise ValueError(f"provider {name!r} is already registered")
        self._factories[name] = factory

    def replace(self, name: str, factory: ProviderFactory) -> None:
        """Replace a known adapter, primarily for embedding and tests."""

        if name not in self._factories:
            raise ValueError(f"provider {name!r} is not registered")
        self._factories[name] = factory

    def create(
        self, name: str, model: str | None = None, *, effort: str | None = None
    ) -> ChatProvider:
        if effort is not None and (
            not isinstance(effort, str) or effort not in EFFORT_LEVELS
        ):
            raise ValueError("model effort is invalid")
        factory = self._factories.get(name)
        if factory is None:
            available = ", ".join(sorted(self._factories)) or "none"
            raise ValueError(
                f"model provider {name!r} is not installed; available: {available}"
            )
        if effort is None:
            # Retain compatibility with installed one-argument adapters.
            provider = factory(model)
        else:
            try:
                inspect.signature(factory).bind(model, effort=effort)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"provider {name!r} does not support selecting effort"
                ) from exc
            provider = factory(model, effort=effort)
        if provider.name != name:
            raise ValueError("provider factory returned a mismatched adapter")
        return provider

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


__all__ = ["ProviderFactory", "ProviderRegistry"]
