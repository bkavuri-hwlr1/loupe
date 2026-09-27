"""Local construction registry for durable coding-agent harnesses.

Launch rows persist a stable driver name plus JSON-safe parameters.  The daemon
resolves that data through this registry on every boot, which keeps scheduling
and recovery independent of any concrete harness or model provider.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from llm_cli.agent.driver import AgentDriver, DriverCapabilities

DriverFactory = Callable[[Mapping[str, object]], AgentDriver]


@dataclass(frozen=True, slots=True)
class DriverRegistration:
    """One durable driver identity and the factory able to reconstruct it."""

    capabilities: DriverCapabilities
    factory: DriverFactory


class DriverRegistry:
    """Resolve persisted driver identities without provider-specific branches."""

    def __init__(self) -> None:
        self._registrations: dict[str, DriverRegistration] = {}

    def register(
        self,
        name: str,
        *,
        capabilities: DriverCapabilities,
        factory: DriverFactory,
    ) -> None:
        if not name or len(name) > 64:
            raise ValueError("driver registry name is invalid")
        if name in self._registrations:
            raise ValueError(f"driver {name!r} is already registered")
        self._registrations[name] = DriverRegistration(
            capabilities=capabilities,
            factory=factory,
        )

    def capabilities(self, name: str) -> DriverCapabilities | None:
        registration = self._registrations.get(name)
        return registration.capabilities if registration is not None else None

    def create(self, name: str, parameters: Mapping[str, object]) -> AgentDriver:
        registration = self._registrations.get(name)
        if registration is None:
            raise ValueError(f"durable launch names unknown driver {name!r}")
        driver = registration.factory(parameters)
        if driver.name != name or driver.capabilities != registration.capabilities:
            raise ValueError("driver factory returned a mismatched registration")
        return driver


__all__ = ["DriverFactory", "DriverRegistration", "DriverRegistry"]
