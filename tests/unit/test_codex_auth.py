from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.providers import codex_auth as auth


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )


def _token(
    *,
    expires: float | None = None,
    account: str = "account-test",
    residency: str = "eu",
) -> str:
    claims = {
        "exp": expires or time.time() + 7200,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account,
            "chatgpt_compute_residency": residency,
        },
    }
    return (
        "header."
        + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        + ".signature"
    )


def _tokens(**kwargs: Any) -> dict[str, Any]:
    return {
        "access_token": _token(**kwargs),
        "refresh_token": "refresh-private",
        "expires_in": 3600,
    }


def test_login_status_and_logout_are_local_and_never_emit_credentials(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    client = DaemonClient(paths)
    args = build_parser().parse_args(["auth", "status", "codex"])
    assert dispatch(args, client)["authenticated"] is False
    assert not paths.state_dir.exists()
    store = auth.CredentialStore(paths)
    credentials = auth.Credentials.from_tokens(_tokens())
    store.save(credentials)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
    status = dispatch(args, client)
    assert status["authenticated"] is True
    for secret in (
        credentials.access_token,
        credentials.refresh_token,
        credentials.account_id,
    ):
        assert secret not in json.dumps(status)
        assert secret not in repr(credentials)
    dispatch(build_parser().parse_args(["auth", "logout"]), client)
    assert not store.path.exists()


@pytest.mark.parametrize("unsafe", ["symlink", "permissions", "hardlink", "malformed"])
def test_rejects_unsafe_credential_files(tmp_path: Path, unsafe: str) -> None:
    store = auth.CredentialStore(_paths(tmp_path))
    store.save(auth.Credentials.from_tokens(_tokens()))
    if unsafe == "symlink":
        original = store.path.with_suffix(".other")
        store.path.rename(original)
        store.path.symlink_to(original)
    elif unsafe == "permissions":
        store.path.chmod(0o644)
    elif unsafe == "hardlink":
        store.path.with_suffix(".other").hardlink_to(store.path)
    else:
        store.path.write_text("{malformed private data")
    with pytest.raises(LlmCoordError):
        store.credentials()


def test_concurrent_workers_refresh_once_and_persist_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    store = auth.CredentialStore(paths)
    store.save(auth.Credentials.from_tokens(_tokens(expires=time.time() - 1)))
    calls = []

    def post(path: str, payload: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        calls.append(payload)
        assert path == "/oauth/token" and kwargs == {"form": True}
        assert payload["refresh_token"] == "refresh-private"
        return {**_tokens(), "refresh_token": "rotated-private"}

    monkeypatch.setattr(auth, "_post", post)
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(
            executor.map(lambda _: auth.CredentialStore(paths).credentials(), range(8))
        )
    assert len(calls) == 1
    assert all(item.refresh_token == "rotated-private" for item in values)
    assert auth.CredentialStore(paths).credentials().refresh_token == "rotated-private"


def test_rejected_refresh_keeps_previous_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = auth.CredentialStore(_paths(tmp_path))
    store.save(auth.Credentials.from_tokens(_tokens(expires=time.time() - 1)))
    before = store.path.read_bytes()
    monkeypatch.setattr(
        auth, "_post", lambda *args, **kwargs: _tokens(account="different")
    )
    with pytest.raises(LlmCoordError, match="account changed"):
        store.credentials()
    assert store.path.read_bytes() == before


def test_explicit_codex_import_does_not_copy_or_rotate_its_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "auth.json"
    source.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {**_tokens(), "account_id": "account-test"},
            }
        )
    )
    source.chmod(0o600)
    before = source.read_bytes()
    store = auth.CredentialStore(_paths(tmp_path))
    assert store.import_codex_access(source)["refreshable"] is False
    assert "refresh-private" not in store.path.read_text()
    assert source.read_bytes() == before
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 10000)
    monkeypatch.setattr(
        auth,
        "_post",
        lambda *args, **kwargs: pytest.fail("must not rotate imported token"),
    )
    with pytest.raises(LlmCoordError, match="expired"):
        store.credentials()


def test_authorize_url_uses_pkce_and_state() -> None:
    url = urllib.parse.urlsplit(auth.authorization_url("state-abc", "verifier-abc"))
    params = urllib.parse.parse_qs(url.query)
    assert url.scheme == "https" and url.netloc == "auth.openai.com"
    assert params["state"] == ["state-abc"]
    assert params["code_challenge_method"] == ["S256"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(b"verifier-abc").digest())
        .decode()
        .rstrip("=")
    )
    assert params["code_challenge"] == [expected]
    assert "verifier-abc" not in url.query
    assert params["redirect_uri"] == [auth.REDIRECT_URI]


def test_browser_flow_ignores_forged_state_then_exchanges_bound_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the actual HTTP callback parser using memory sockets, no listener."""
    requests = []
    issued = {}

    class Socket:
        def __init__(self, path: str) -> None:
            self.path = path

        def settimeout(self, _: int) -> None:
            pass

        def makefile(self, *args: Any) -> io.BytesIO:
            return io.BytesIO(f"GET {self.path} HTTP/1.0\r\n\r\n".encode())

        def sendall(self, data: bytes) -> None:
            pass

    class Server:
        def __init__(self, address: Any, handler: Any) -> None:
            assert address == ("127.0.0.1", 1455)
            self.handler = handler

        def __enter__(self) -> Server:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def handle_request(self) -> None:
            index = len(requests)
            state = "forged" if index == 0 else issued["state"][0]
            path = f"/auth/callback?code=code-{index}&state={state}"
            requests.append(path)
            self.handler(Socket(path), ("127.0.0.1", 1234), self)

    def opened(url: str) -> bool:
        issued.update(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
        return True

    def post(path: str, payload: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        assert len(requests) == 2 and payload["code"] == "code-1"
        challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(payload["code_verifier"].encode()).digest()
            )
            .decode()
            .rstrip("=")
        )
        assert issued["code_challenge"] == [challenge]
        assert payload["redirect_uri"] == auth.REDIRECT_URI
        return _tokens()

    monkeypatch.setattr(auth, "HTTPServer", Server)
    monkeypatch.setattr(auth.webbrowser, "open", opened)
    monkeypatch.setattr(auth, "_post", post)
    assert auth.browser_login(auth.CredentialStore(_paths(tmp_path)), lambda _: None)[
        "authenticated"
    ]


def test_device_flow_handles_pending_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def post(path: str, payload: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        if path.endswith("usercode"):
            return {
                "device_auth_id": "device",
                "user_code": "ABCD-EFGH",
                "interval": "1",
            }
        if len(calls) == 2:
            raise LlmCoordError(
                auth.ErrorCode.PROVIDER_UNAVAILABLE, "pending", {"status_code": 403}
            )
        if path.endswith("deviceauth/token"):
            return {"authorization_code": "code", "code_verifier": "verifier"}
        assert payload["code_verifier"] == "verifier"
        assert payload["redirect_uri"] == auth.ISSUER + "/deviceauth/callback"
        return _tokens()

    monkeypatch.setattr(auth, "_post", post)
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    notices = []
    assert auth.device_login(auth.CredentialStore(_paths(tmp_path)), notices.append)[
        "authenticated"
    ]
    assert "ABCD-EFGH" in notices[0]
    assert len(calls) == 4
