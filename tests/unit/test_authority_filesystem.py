"""Filesystem detection must select Darwin's ABI before trusting its type name."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.storage.control import ControlStore
from llm_cli.workspace import authority as module


@pytest.mark.skipif(sys.platform != "darwin", reason="native Darwin statfs regression")
def test_native_macos_home_filesystem_is_supported() -> None:
    # This must execute natively in both the Apple Silicon and Intel CI jobs.
    assert module._filesystem(Path.home()) in module.SUPPORTED_FILESYSTEMS


class _Statfs:
    def __init__(self, offset: int, result: int = 0) -> None:
        self.offset, self.result = offset, result
        self.calls = 0
        self.argtypes: list[type[ctypes.c_char_p] | type[ctypes.c_void_p]] | None = None
        self.restype: type[ctypes.c_int] | None = None

    def __call__(self, path: bytes, buffer: ctypes.Array[ctypes.c_char]) -> int:
        assert path == b"/checkout"
        self.calls += 1
        ctypes.memmove(ctypes.addressof(buffer) + self.offset, b"apfs\0", 5)
        return self.result


@pytest.mark.parametrize("architecture", ["x86_64", "arm64"])
def test_darwin_uses_the_symbol_matching_the_modern_layout(
    monkeypatch: pytest.MonkeyPatch, architecture: str
) -> None:
    legacy = _Statfs(104 if architecture == "x86_64" else 72)
    modern = _Statfs(72)
    library = SimpleNamespace(statfs=legacy)
    if architecture == "x86_64":
        setattr(library, "statfs$INODE64", modern)
        # Negative control: the former dlsym call succeeds but reads an empty
        # name, so a test merely checking its return code would miss the bug.
        buffer = ctypes.create_string_buffer(4096)
        assert legacy(b"/checkout", buffer) == 0
        assert buffer.raw[72:88].split(b"\0", 1)[0] == b""
        assert buffer.raw[104:120].split(b"\0", 1)[0] == b"apfs"
        legacy.calls = 0
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: architecture)
    monkeypatch.setattr(module.ctypes, "CDLL", lambda *args, **kwargs: library)

    assert module._filesystem(Path("/checkout")) == "apfs"
    selected = modern if architecture == "x86_64" else legacy
    assert selected.calls == 1
    assert selected.argtypes == [ctypes.c_char_p, ctypes.c_void_p]
    assert selected.restype is ctypes.c_int
    assert (legacy if architecture == "x86_64" else modern).calls == 0


def test_darwin_intel_does_not_fall_back_to_the_legacy_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _Statfs(104)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        module.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(statfs=legacy)
    )
    with pytest.raises(LlmCoordError, match="filesystem cannot be verified") as refused:
        module._filesystem(Path("/checkout"))
    assert refused.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
    assert legacy.calls == 0


@pytest.mark.parametrize("architecture", ["x86_64", "arm64"])
def test_darwin_statfs_error_refuses_an_unverified_name(
    monkeypatch: pytest.MonkeyPatch, architecture: str
) -> None:
    function = _Statfs(72, result=-1)
    symbol = "statfs$INODE64" if architecture == "x86_64" else "statfs"
    library = SimpleNamespace(**{symbol: function})
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: architecture)
    monkeypatch.setattr(module.ctypes, "CDLL", lambda *args, **kwargs: library)
    with pytest.raises(LlmCoordError, match="filesystem cannot be verified"):
        module._filesystem(Path("/checkout"))


@pytest.mark.parametrize(
    "filesystem", ["9p", "drvfs", "virtiofs", "exfat", "ecryptfs", "fuse"]
)
def test_unsupported_authority_filesystem_is_named_and_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filesystem: str
) -> None:
    store = ControlStore(tmp_path / "control.sqlite3")
    store.initialize()
    owner = module.PhysicalAuthority(
        store,
        "test",
        "boot-test",
        tmp_path / "socket",
        root=tmp_path / "authority",
    )
    monkeypatch.setattr(module, "_filesystem", lambda path: filesystem)
    try:
        with pytest.raises(LlmCoordError, match=repr(filesystem)) as refused:
            owner._prepare_root()
        assert refused.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
        assert refused.value.details["filesystem"] == filesystem
        assert filesystem not in module.SUPPORTED_FILESYSTEMS
        assert not (owner.root / "checkouts").exists()
    finally:
        owner.close()
