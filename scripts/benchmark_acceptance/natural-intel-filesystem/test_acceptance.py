import importlib.util
import os
import sys
import uuid
from pathlib import Path


def load_subject():
    path = Path(os.environ["BENCHMARK_REPOSITORY"]) / "src" / "subject.py"
    name = "natural_subject_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parents[1]))
    spec.loader.exec_module(module)
    return module


def test_contract():
    subject = load_subject()
    from types import SimpleNamespace

    subject.sys = SimpleNamespace(platform="darwin")
    for machine in ("arm64", "x86_64"):
        calls = []

        class Function:
            def __init__(self, modern, machine=machine, calls=calls):
                self.modern = modern
                self.machine = machine
                self.calls = calls

            def __call__(self, path, buffer):
                self.calls.append(self.modern)
                offset = 72 if self.modern or self.machine == "arm64" else 104
                buffer[offset : offset + 5] = b"apfs\0"
                return 0

        library = SimpleNamespace(statfs=Function(False))
        setattr(library, "statfs$INODE64", Function(True))
        subject.platform = SimpleNamespace(machine=lambda machine=machine: machine)
        subject.ctypes = SimpleNamespace(
            create_string_buffer=__import__("ctypes").create_string_buffer,
            CDLL=lambda *a, library=library, **k: library,
            c_char_p=__import__("ctypes").c_char_p,
            c_void_p=__import__("ctypes").c_void_p,
            c_int=__import__("ctypes").c_int,
        )
        assert subject._filesystem(subject.Path("/")) == "apfs"
        assert calls == [machine == "x86_64"]
    subject.platform = SimpleNamespace(machine=lambda: "x86_64")
    subject.ctypes.CDLL = lambda *a, **k: SimpleNamespace()
    try:
        subject._filesystem(subject.Path("/"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("missing ABI symbol did not fail closed")
