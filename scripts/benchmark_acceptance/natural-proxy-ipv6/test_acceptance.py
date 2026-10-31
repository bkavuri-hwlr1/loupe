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
    import io

    class Socket:
        def __init__(self):
            self.sent = bytearray()

        def sendall(self, data):
            self.sent.extend(data)

        def makefile(self, *args, **kwargs):
            return io.BytesIO(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        def close(self):
            pass

    class TLS:
        def __init__(self):
            self.names = []

        def wrap_socket(self, sock, *, server_hostname):
            self.names.append(server_hostname)
            return sock

    def connect(client):
        client.sock = Socket()
        client._tunnel()

    original = subject.http.client.HTTPConnection.connect
    subject.http.client.HTTPConnection.connect = connect
    try:
        for address, authority in (
            ("2001:db8::1", "[2001:db8::1]"),
            ("192.0.2.1", "192.0.2.1"),
        ):
            for port in (443, 8443):
                tls = TLS()
                client = subject._TunnelConnection(
                    "https",
                    "docs.example.invalid",
                    address,
                    port,
                    3,
                    tls,
                    ("127.0.0.1", 8080),
                )
                client.connect()
                lines = client.sock.sent.decode("ascii").split("\r\n")
                method, target, version = lines[0].split()
                assert method == "CONNECT" and target == f"{authority}:{port}"
                headers = {
                    key.lower(): value.strip()
                    for line in lines[1:]
                    if ":" in line
                    for key, value in [line.split(":", 1)]
                }
                if ":" in address or version == "HTTP/1.1" or "host" in headers:
                    assert headers.get("host") == f"{authority}:{port}"
                assert tls.names == ["docs.example.invalid"]
                assert client.host == "127.0.0.1" and client.port == 8080
                client.close()
    finally:
        subject.http.client.HTTPConnection.connect = original
