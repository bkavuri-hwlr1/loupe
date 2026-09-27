"""ChatGPT OAuth and private, profile-local credentials for the Codex provider.

Protocol references (public OAuth client, not a client secret):
https://github.com/openai/codex/tree/main/codex-rs/login/src
https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/plugin/openai/codex.ts
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import secrets
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths, reject_symlink_components

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
REDIRECT_URI = "http://localhost:1455/auth/callback"
_MAX_CREDENTIAL_BYTES = 64 * 1024
_LOGIN_TIMEOUT = 15 * 60


def _failure(message: str) -> LlmCoordError:
    return LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, message)


def _text(value: object) -> str | None:
    if isinstance(value, str) and value and all(33 <= ord(c) < 127 for c in value):
        return value
    return None


def _claims(token: str) -> dict[str, Any]:
    """Read routing/expiry hints only; the remote service authenticates the JWT."""
    try:
        encoded = token.split(".")[1]
        value = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, UnicodeError):
        return {}


@dataclass(frozen=True)
class Credentials:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float
    account_id: str = field(repr=False)
    residency: str | None = None

    @classmethod
    def from_tokens(
        cls,
        tokens: Mapping[str, Any],
        *,
        prior: Credentials | None = None,
        account_id: str | None = None,
    ) -> Credentials:
        access = _text(tokens.get("access_token"))
        if access is None:
            raise _failure("ChatGPT returned malformed credentials; sign in again")
        access_claims = _claims(access)
        identity = _claims(str(tokens.get("id_token", "")))
        for claims in (identity, access_claims):
            auth = claims.get("https://api.openai.com/auth", {})
            if not isinstance(auth, dict):
                auth = {}
            account_id = (
                account_id
                or _text(auth.get("chatgpt_account_id"))
                or _text(claims.get("chatgpt_account_id"))
            )
        if not account_id:
            account_id = prior.account_id if prior else None
        if not _text(account_id):
            raise _failure(
                "ChatGPT credentials have no account identity; sign in again"
            )
        assert account_id is not None
        if prior and account_id != prior.account_id:
            raise _failure("ChatGPT account changed during refresh; sign in again")
        expires_in = tokens.get("expires_in", 3600)
        if (
            not isinstance(expires_in, (int, float))
            or isinstance(expires_in, bool)
            or not math.isfinite(expires_in)
            or expires_in <= 0
        ):
            raise _failure("ChatGPT returned an invalid token lifetime")
        expiry = time.time() + expires_in
        jwt_expiry = access_claims.get("exp")
        if (
            isinstance(jwt_expiry, (int, float))
            and not isinstance(jwt_expiry, bool)
            and math.isfinite(jwt_expiry)
        ):
            expiry = min(expiry, jwt_expiry)
        auth = access_claims.get("https://api.openai.com/auth", {})
        residency = _text(
            (auth.get("chatgpt_compute_residency") if isinstance(auth, dict) else None)
            or access_claims.get("chatgpt_compute_residency")
        )
        return cls(
            access,
            _text(tokens.get("refresh_token"))
            or (prior.refresh_token if prior else None),
            expiry,
            account_id,
            None if residency == "no_constraint" else residency,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        # Never forward OAuth secrets through an HTTP redirect.
        return None


def _post(path: str, payload: dict[str, str], *, form: bool = False) -> dict[str, Any]:
    if path not in {
        "/oauth/token",
        "/api/accounts/deviceauth/usercode",
        "/api/accounts/deviceauth/token",
    }:
        raise ValueError("unrecognized ChatGPT authentication endpoint")
    body = urllib.parse.urlencode(payload) if form else json.dumps(payload)
    request = urllib.request.Request(
        ISSUER + path,
        data=body.encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
            if form
            else "application/json",
            "User-Agent": "llm-coord",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(_MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > _MAX_CREDENTIAL_BYTES:
            raise ValueError("oversized response")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("invalid response")
        return data
    except urllib.error.HTTPError as exc:
        raise LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"ChatGPT authentication returned HTTP {exc.code}; try signing in again",
            details={"status_code": exc.code},
        ) from None
    except (OSError, ValueError):
        raise _failure(
            "ChatGPT authentication could not complete; check connectivity and retry"
        ) from None


def _read_private(path: Path) -> dict[str, Any]:
    reject_symlink_components(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise _failure(
                "credential file must be an owner-only regular file (mode 600)"
            )
        raw = handle.read(_MAX_CREDENTIAL_BYTES + 1)
    if len(raw) > _MAX_CREDENTIAL_BYTES:
        raise _failure("credential file is too large")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except ValueError:
        raise _failure("credential file is malformed; sign in again") from None


class CredentialStore:
    def __init__(self, paths: AppPaths) -> None:
        self.directory = paths.state_dir / "auth"
        self.path = self.directory / "codex.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        reject_symlink_components(self.directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        lock_path = self.directory / "codex.lock"
        reject_symlink_components(lock_path)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise _failure("credential lock is not a private regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _load(self) -> Credentials:
        try:
            data = _read_private(self.path)
            expiry = data.get("expires_at")
            if (
                not _text(data.get("access_token"))
                or not _text(data.get("account_id"))
                or not isinstance(expiry, (int, float))
                or isinstance(expiry, bool)
                or not math.isfinite(expiry)
                or (
                    data.get("refresh_token") is not None
                    and not _text(data["refresh_token"])
                )
                or (data.get("residency") is not None and not _text(data["residency"]))
                or set(data) != set(Credentials.__dataclass_fields__)
            ):
                raise ValueError("invalid stored credentials")
            return Credentials(**data)
        except FileNotFoundError:
            raise _failure(
                "no ChatGPT login for this profile; run 'llm-coord auth login codex'"
            ) from None
        except (OSError, ValueError, TypeError):
            raise _failure(
                "cannot read ChatGPT credentials; run 'llm-coord auth login codex'"
            ) from None

    def _save(self, credentials: Credentials) -> None:
        reject_symlink_components(self.path)
        descriptor, temporary = tempfile.mkstemp(prefix=".codex-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(asdict(credentials), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save(self, credentials: Credentials) -> None:
        with self._locked():
            self._save(credentials)

    def credentials(self) -> Credentials:
        # Both CLI processes and concurrent daemon workers serialize token rotation.
        with self._locked():
            current = self._load()
            if current.expires_at > time.time() + 60:
                return current
            if current.refresh_token is None:
                raise _failure(
                    "ChatGPT access token expired; run 'llm-coord auth login codex'"
                )
            tokens = _post(
                "/oauth/token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": current.refresh_token,
                    "client_id": CLIENT_ID,
                },
                form=True,
            )
            refreshed = Credentials.from_tokens(tokens, prior=current)
            if refreshed.expires_at <= time.time() + 60:
                raise _failure("ChatGPT returned an expired token; sign in again")
            self._save(refreshed)
            return refreshed

    def status(self) -> dict[str, object]:
        if not self.path.exists() and not self.path.is_symlink():
            return {
                "provider": "codex",
                "billing": "subscription",
                "authenticated": False,
            }
        current = self._load()
        return {
            "provider": "codex",
            "billing": "subscription",
            "authenticated": current.expires_at > time.time(),
            "expired": current.expires_at <= time.time(),
            "refreshable": current.refresh_token is not None,
        }

    def logout(self) -> dict[str, object]:
        with self._locked():
            reject_symlink_components(self.path)
            self.path.unlink(missing_ok=True)
        return {"provider": "codex", "authenticated": False}

    def import_codex_access(self, path: Path) -> dict[str, object]:
        """Explicit one-time reuse. Never copy/rotate another app's refresh token."""
        try:
            data = _read_private(path)
        except OSError:
            raise _failure(
                "cannot read Codex's file credentials; use 'auth login codex' instead"
            ) from None
        tokens = data.get("tokens")
        if data.get("auth_mode") not in {None, "chatgpt"} or not isinstance(
            tokens, dict
        ):
            raise _failure("the Codex credential file has no ChatGPT login")
        credentials = Credentials.from_tokens(
            {
                "access_token": tokens.get("access_token"),
                "id_token": tokens.get("id_token"),
            },
            account_id=_text(tokens.get("account_id")),
        )
        if credentials.expires_at <= time.time() + 60:
            raise _failure(
                "Codex's access token expired; use 'llm-coord auth login codex'"
            )
        self.save(credentials)
        return {**self.status(), "source": "codex_access_token", "refreshable": False}


def authorization_url(state: str, verifier: str) -> str:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return (
        ISSUER
        + "/oauth/authorize?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "scope": "openid profile email offline_access",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "originator": "llm-coord",
            }
        )
    )


def browser_login(
    store: CredentialStore, notify: Callable[[str], None]
) -> dict[str, object]:
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    outcome: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        timeout = 2  # A stalled local connection must not defeat the login timeout.

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Callback URLs contain authorization codes.

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            values = urllib.parse.parse_qs(parsed.query)
            valid = (
                parsed.path == "/auth/callback"
                and len(values.get("state", [])) == 1
                and _text(values["state"][0]) is not None
                and secrets.compare_digest(values["state"][0], state)
            )
            if valid:
                codes = values.get("code", [])
                if "error" not in values and len(codes) == 1 and _text(codes[0]):
                    outcome["code"] = codes[0]
                else:
                    outcome["error"] = "ChatGPT login was not authorized; try again"
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                b"Return to your terminal to check sign-in status."
                if valid
                else b"Invalid login callback."
            )

    try:
        server = HTTPServer(("127.0.0.1", 1455), Callback)
    except OSError:
        raise _failure(
            "cannot listen on login port 1455; use 'auth login codex --device'"
        ) from None
    with server:
        server.timeout = 1
        url = authorization_url(state, verifier)
        notify("Sign in with ChatGPT in your browser:\n" + url)
        webbrowser.open(url)
        deadline = time.monotonic() + _LOGIN_TIMEOUT
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in outcome:
        raise _failure(outcome.get("error", "ChatGPT login timed out; try again"))
    tokens = _post(
        "/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": outcome["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
        form=True,
    )
    store.save(Credentials.from_tokens(tokens))
    return store.status()


def device_login(
    store: CredentialStore, notify: Callable[[str], None]
) -> dict[str, object]:
    device = _post("/api/accounts/deviceauth/usercode", {"client_id": CLIENT_ID})
    device_id, code = (
        _text(device.get("device_auth_id")),
        _text(device.get("user_code")),
    )
    if device_id is None or code is None:
        raise _failure("ChatGPT returned an invalid device code")
    try:
        interval = max(1, min(30, int(device.get("interval", 5)))) + 3
    except (TypeError, ValueError, OverflowError):
        raise _failure("ChatGPT returned an invalid polling interval") from None
    notify(f"Open {ISSUER}/codex/device and enter code: {code}")
    deadline = time.monotonic() + _LOGIN_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            grant = _post(
                "/api/accounts/deviceauth/token",
                {"device_auth_id": device_id, "user_code": code},
            )
        except LlmCoordError as exc:
            if (exc.details or {}).get("status_code") in (403, 404):
                continue
            raise
        auth_code, verifier = (
            _text(grant.get("authorization_code")),
            _text(grant.get("code_verifier")),
        )
        if auth_code is None or verifier is None:
            raise _failure("ChatGPT returned an invalid device authorization")
        tokens = _post(
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": ISSUER + "/deviceauth/callback",
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
            form=True,
        )
        store.save(Credentials.from_tokens(tokens))
        return store.status()
    raise _failure("ChatGPT device login timed out; try again")
