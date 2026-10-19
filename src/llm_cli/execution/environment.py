"""Ephemeral, role-filtered terminal environments for project commands.

Values live only in this registry and child-process memory. Inspection exposes
names and rewrite decisions, never values or a hash suitable for guessing them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from llm_cli.errors import ErrorCode, LlmCoordError

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,255}\Z")
_SECRET_NAME = re.compile(
    r"secret|password|passwd|token|api_?key|credential|authorization|private_?key|"
    r"access_?key|(?:database|db|redis|mongo(?:db)?|postgres|mysql|amqp)_(?:url|uri|dsn)",
    re.I,
)
# Registered credential names are supplied by provider setup; this list also
# strips common provider credentials when their adapter has never been selected.
PROVIDER_CREDENTIAL_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "OPENAI_ACCESS_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_AD_TOKEN",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "COHERE_API_KEY",
        "TOGETHER_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
    }
)
# Sandbox-owned locations cannot be selected by a terminal or check override.
SANDBOX_VARIABLES = frozenset({"HOME", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME"})


def filtered_environment(
    value: object, *, registered: tuple[str, ...] = ()
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Validate without including input values in errors or exception reprs."""
    if not isinstance(value, dict) or len(value) > 4096:
        raise ValueError("session environment must be a bounded variable map")
    kept: dict[str, str] = {}
    removed = []
    total = 0
    reserved = PROVIDER_CREDENTIAL_NAMES | {name.upper() for name in registered}
    for name, body in value.items():
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or not isinstance(body, str)
            or "\0" in body
        ):
            raise ValueError("session environment has an invalid variable")
        try:
            total += len(name.encode()) + len(os.fsencode(body))
        except UnicodeError:
            raise ValueError("session environment has an invalid encoding") from None
        if total > 1024 * 1024:
            raise ValueError("session environment exceeds the transport limit")
        upper = name.upper()
        if upper.startswith(("LOUPE_", "LLM_COORD_")) or upper in reserved:
            removed.append(name)
        else:
            kept[name] = body
    return kept, tuple(sorted(removed))


def _screening_values(value: str) -> set[str]:
    # The supervisor screens complete output lines before emitting them. A
    # multiline value therefore also needs its nonempty logical lines screened;
    # never split into arbitrary read-sized prefixes or alter the child value.
    return {part for part in (value, *value.splitlines()) if part}


def unavailable() -> LlmCoordError:
    return LlmCoordError(
        ErrorCode.SESSION_NOT_ACTIVE,
        "Commands are awaiting_session_environment; reconnect with session resume "
        "or chat --resume from the originating terminal.",
        {"state": "awaiting_session_environment"},
    )


@dataclass(slots=True, repr=False)
class CommandEnvironment:
    values: dict[str, str] = field(repr=False)
    removed: tuple[str, ...] = ()
    source_root: Path | None = None
    hydrated_root: Path | None = None
    hydrated_paths: tuple[str, ...] = ()
    credential_names: tuple[str, ...] = ()
    active: bool = True
    rewritten: set[str] = field(default_factory=set)
    rewrite_components: set[tuple[str, int]] = field(default_factory=set)
    unresolved: set[str] = field(default_factory=set)
    _lock: RLock = field(default_factory=RLock, repr=False)
    _binding_lock: RLock | None = field(default=None, repr=False)

    @property
    def sensitive_values(self) -> tuple[str, ...]:
        # Short values still require protection; screen only original output so
        # substitutions never recursively redact the placeholder itself.
        with self._lock:
            self.require()
            return tuple(
                {
                    protected
                    for name, body in self.values.items()
                    if _SECRET_NAME.search(name)
                    for protected in _screening_values(body)
                }
            )

    def capture(
        self,
        snapshot: Path,
        *,
        overrides: dict[str, str] | None = None,
        runtime_root: Path | None = None,
        runtime_paths: tuple[str, ...] = (),
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        # Freeze the child map and its redaction material under the same lock.
        # An authenticated reconnect may invalidate us immediately afterward,
        # but can never clear the launched child's output protection.
        with self._lock:
            values = self.prepare(
                snapshot,
                overrides=overrides,
                runtime_root=runtime_root,
                runtime_paths=runtime_paths,
            )
            protected = set(self.sensitive_values)
            if overrides:
                protected.update(
                    protected
                    for name, body in overrides.items()
                    if _SECRET_NAME.search(name)
                    for protected in _screening_values(body)
                )
            protected.update(
                protected
                for name, body in values.items()
                if _SECRET_NAME.search(name)
                for protected in _screening_values(body)
            )
            return values, tuple(protected)

    @contextmanager
    def accepted_run(self) -> Iterator[None]:
        # Final check evidence and reconnect invalidation share one binding
        # lock. A late successful write cannot resurrect old-environment proof.
        with self._binding_lock or self._lock, self._lock:
            self.require()
            yield

    def invalidate(self) -> None:
        with self._lock:
            self.active = False
            self.values.clear()

    def require(self) -> None:
        if not self.active:
            raise unavailable()

    def prepare(
        self,
        snapshot: Path,
        *,
        overrides: dict[str, str] | None = None,
        runtime_root: Path | None = None,
        runtime_paths: tuple[str, ...] = (),
    ) -> dict[str, str]:
        self.require()
        result = dict(self.values)
        if overrides:
            result.update(
                filtered_environment(overrides, registered=self.credential_names)[0]
            )
        for name in SANDBOX_VARIABLES:
            result.pop(name, None)
        if self.hydrated_root is None or self.source_root is None:
            return result
        source = self.source_root.resolve()
        hydrated = self.hydrated_root.resolve()
        target = snapshot.resolve()
        runtime = runtime_root.resolve() if runtime_root is not None else hydrated
        for name, value in result.items():
            if name in SANDBOX_VARIABLES:
                continue
            # PATH-like values are lists of exact path components, not string
            # substitutions into arbitrary command text, URLs, or scripts.
            components = value.split(os.pathsep) if name.endswith("PATH") else [value]
            rewritten = []
            for component_index, component in enumerate(components):
                path = Path(component)
                if path.is_absolute() and not path.is_relative_to(source):
                    # Shells can retain a logical checkout alias in PWD/PATH.
                    # Resolve only the path component, never arbitrary text.
                    try:
                        canonical = path.resolve()
                    except (OSError, RuntimeError, ValueError):
                        canonical = path
                    if canonical.is_relative_to(source):
                        path = canonical
                if not path.is_absolute() or not path.is_relative_to(source):
                    rewritten.append(component)
                    continue
                relative = path.relative_to(source)
                frozen_runtime = any(
                    relative == Path(item) or relative.is_relative_to(Path(item))
                    for item in runtime_paths
                )
                equivalent_root = runtime if frozen_runtime else hydrated
                private = equivalent_root / relative
                destination = target / relative
                known = (
                    relative == Path(".")
                    or frozen_runtime
                    or any(
                        relative.as_posix() == item
                        or relative.as_posix().startswith(item + "/")
                        or item.startswith(relative.as_posix().rstrip("/") + "/")
                        for item in self.hydrated_paths
                    )
                )
                try:
                    safe = (
                        known
                        and private.exists()
                        and destination.exists()
                        and private.resolve(strict=True).is_relative_to(equivalent_root)
                        and destination.resolve(strict=True).is_relative_to(target)
                    )
                except (OSError, RuntimeError, ValueError):
                    safe = False
                if not safe:
                    self.unresolved.add(name)
                    raise LlmCoordError(
                        ErrorCode.CONFIG_INVALID,
                        "Session environment path has no exact hydrated equivalent; "
                        f"adjust {name} in the terminal and reconnect.",
                        {"variable": name, "state": "unresolved_environment_path"},
                    )
                self.rewritten.add(name)
                self.rewrite_components.add((name, component_index))
                rewritten.append(str(destination))
            result[name] = os.pathsep.join(rewritten)
        return result

    def matches(self, values: dict[str, str]) -> bool:
        with self._lock:
            return self.active and self.values == values

    def plan(self) -> dict[str, object]:
        with self._lock:
            return self._plan()

    def _plan(self) -> dict[str, object]:
        return {
            "policy": "session",
            "state": "ready" if self.active else "awaiting_session_environment",
            "included_names": sorted(set(self.values) - SANDBOX_VARIABLES),
            "removed_names": list(self.removed),
            "sandbox_override_names": sorted(set(self.values) & SANDBOX_VARIABLES),
            "screened_names": sorted(
                name for name in self.values if _SECRET_NAME.search(name)
            ),
            "rewritten_names": sorted(self.rewritten),
            "rewrites": [
                {"variable": name, "component_index": component}
                for name, component in sorted(self.rewrite_components)
            ],
            "unresolved_names": sorted(self.unresolved),
        }


EnvironmentProvider = Callable[[str], CommandEnvironment | None]
EnvironmentSupplier = Callable[[], CommandEnvironment | None]


class SessionEnvironments:
    def __init__(self) -> None:
        self._sessions: dict[str, CommandEnvironment] = {}
        self._lock = RLock()

    def replace(
        self,
        session_id: str,
        value: CommandEnvironment,
        *,
        before_replace: Callable[[], None] | None = None,
    ) -> None:
        with self._lock:
            # Commit invalidation before exposing a new binding. Existing
            # verification consumers therefore never see old successes under
            # an already-installed new terminal, even outside this registry.
            if before_replace is not None:
                before_replace()
            self.discard(session_id)
            value._binding_lock = self._lock
            self._sessions[session_id] = value

    def discard(self, session_id: str) -> None:
        with self._lock:
            prior = self._sessions.pop(session_id, None)
            if prior is not None:
                prior.invalidate()

    def clear(self) -> None:
        with self._lock:
            for session_id in tuple(self._sessions):
                self.discard(session_id)

    def get(self, session_id: str) -> CommandEnvironment:
        with self._lock:
            value = self._sessions.get(session_id)
            if value is None:
                raise unavailable()
            value.require()
            return value

    def plan(self, session_id: str) -> dict[str, object]:
        with self._lock:
            value = self._sessions.get(session_id)
            return (
                value.plan()
                if value
                else {
                    "policy": "session",
                    "state": "awaiting_session_environment",
                    "included_names": [],
                    "removed_names": [],
                    "sandbox_override_names": [],
                    "screened_names": [],
                    "rewritten_names": [],
                    "rewrites": [],
                    "unresolved_names": [],
                }
            )
