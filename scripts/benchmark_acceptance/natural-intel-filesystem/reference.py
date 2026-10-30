from __future__ import annotations

import ctypes
import os
import platform
import sys
from pathlib import Path


def recovery_error(message):
    return RuntimeError(message)

def _filesystem(path: Path) -> str:
    if sys.platform == "darwin":
        buffer = ctypes.create_string_buffer(4096)
        library = ctypes.CDLL(None, use_errno=True)
        # Darwin's x86_64 dlsym("statfs") exposes the legacy ABI, whose
        # f_fstypename starts at 104. Match the modern SDK layout (offset 72)
        # explicitly there; arm64 exposes that layout through plain statfs.
        symbol = "statfs$INODE64" if platform.machine() == "x86_64" else "statfs"
        try:
            statfs = getattr(library, symbol)
        except AttributeError as exc:
            raise recovery_error("authority filesystem cannot be verified") from exc
        statfs.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        statfs.restype = ctypes.c_int
        if statfs(os.fsencode(path), buffer) != 0:
            raise recovery_error("authority filesystem cannot be verified")
        return buffer.raw[72:88].split(b"\0", 1)[0].decode("ascii")
    if sys.platform.startswith("linux"):
        matches: list[tuple[int, str]] = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            mount = Path(before.split()[4].replace("\\040", " "))
            if path == mount or path.is_relative_to(mount):
                matches.append((len(mount.parts), after.split()[0]))
        if matches:
            return max(matches)[1]
    raise recovery_error("authority filesystem semantics are unknown")
