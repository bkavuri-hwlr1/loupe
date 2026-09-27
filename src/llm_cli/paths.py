"""Platform paths and owner-only local state initialization."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from llm_cli.errors import ErrorCode, LlmCoordError

_APP_DIR = "llm-coord"
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _base_path(env: Mapping[str, str], override: str, xdg: str, fallback: Path) -> Path:
    raw = env.get(override) or env.get(xdg)
    return Path(raw).expanduser() if raw else fallback


@dataclass(frozen=True, slots=True)
class AppPaths:
    profile_id: str
    config_dir: Path
    data_dir: Path
    state_dir: Path
    runtime_dir: Path

    @classmethod
    def resolve(
        cls,
        profile_id: str = "default",
        *,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> AppPaths:
        if not _PROFILE.fullmatch(profile_id):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                "profile must contain only letters, digits, '.', '_' or '-'",
            )
        env = os.environ if environ is None else environ
        user_home = Path.home() if home is None else home
        config_base = _base_path(
            env, "LLM_COORD_CONFIG_HOME", "XDG_CONFIG_HOME", user_home / ".config"
        )
        data_base = _base_path(
            env,
            "LLM_COORD_DATA_HOME",
            "XDG_DATA_HOME",
            user_home / ".local" / "share",
        )
        state_base = _base_path(
            env,
            "LLM_COORD_STATE_HOME",
            "XDG_STATE_HOME",
            user_home / ".local" / "state",
        )
        runtime_override = env.get("LLM_COORD_RUNTIME_DIR") or env.get(
            "XDG_RUNTIME_DIR"
        )
        runtime_base = (
            Path(runtime_override).expanduser()
            if runtime_override
            else state_base / "run"
        )
        return cls(
            profile_id=profile_id,
            config_dir=config_base / _APP_DIR,
            data_dir=data_base / _APP_DIR / "profiles" / profile_id,
            state_dir=state_base / _APP_DIR / "profiles" / profile_id,
            runtime_dir=runtime_base / _APP_DIR / profile_id,
        )

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def control_db(self) -> Path:
        return self.data_dir / "control.sqlite3"

    @property
    def knowledge_db(self) -> Path:
        return self.data_dir / "knowledge.sqlite3"

    @property
    def vectors_db(self) -> Path:
        return self.data_dir / "vectors.sqlite3"

    @property
    def socket(self) -> Path:
        return self.runtime_dir / "llm-coordd.sock"

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / "llm-coordd.pid.json"

    @property
    def lock_file(self) -> Path:
        return self.runtime_dir / "llm-coordd.lock"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "logs" / "llm-coordd.jsonl"

    @property
    def session_dir(self) -> Path:
        """Owner-only wrapper-held session resume secrets."""

        return self.state_dir / "sessions"

    @property
    def candidate_dir(self) -> Path:
        """Private content-addressed bodies for unpublished shared candidates."""

        return self.data_dir / "candidates"

    def session_secret_file(self, session_id: str) -> Path:
        """Map an opaque ID to a safe filename without trusting path spelling."""

        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.session_dir / f"{digest}.secret"

    def ensure(self) -> None:
        for directory in (
            self.config_dir,
            self.data_dir,
            self.state_dir,
            self.runtime_dir,
            self.log_file.parent,
            self.session_dir,
            self.candidate_dir,
            self.data_dir / "backups",
            self.data_dir / "worktrees",
            self.data_dir / "artifacts",
        ):
            _ensure_private_directory(directory)


def _existing_components(path: Path) -> tuple[Path, ...]:
    absolute = path.absolute()
    components: list[Path] = []
    current = absolute
    while current != current.parent:
        components.append(current)
        current = current.parent
    components.append(current)
    return tuple(reversed(components))


def reject_symlink_components(path: Path) -> None:
    for component in _existing_components(path):
        if component.exists() and component.is_symlink():
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"security-sensitive path contains a symlink: {component}",
            )


def _ensure_private_directory(path: Path) -> None:
    reject_symlink_components(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    reject_symlink_components(path)
    if not path.is_dir():
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID, f"state path is not a directory: {path}"
        )
    path.chmod(0o700)


def make_private_file(path: Path) -> None:
    reject_symlink_components(path)
    if path.exists():
        path.chmod(0o600)


__all__ = ["AppPaths", "make_private_file", "reject_symlink_components"]
