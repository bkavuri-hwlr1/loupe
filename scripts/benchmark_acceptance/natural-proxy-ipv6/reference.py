from __future__ import annotations

import http.client
import ssl


class _TunnelConnection(http.client.HTTPConnection):
    """Connect through an explicitly configured proxy to the pinned target IP."""

    def __init__(
        self,
        scheme: str,
        host: str,
        address: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
        proxy: tuple[str, int],
    ) -> None:
        super().__init__(proxy[0], proxy[1], timeout=timeout)
        self._scheme = scheme
        self._target_host = host
        self._tls = context
        # Some supported Python releases strip IPv6 brackets in set_tunnel or
        # omit them in its synthesized Host header. Keep both authorities exact.
        tunnel_host = f"[{address}]" if ":" in address else address
        self.set_tunnel(tunnel_host, port, headers={"Host": f"{tunnel_host}:{port}"})
        self._tunnel_host = tunnel_host

    def connect(self) -> None:
        super().connect()
        if self._scheme == "https":
            assert self.sock is not None
            try:
                self.sock = self._tls.wrap_socket(
                    self.sock, server_hostname=self._target_host
                )
            except BaseException:
                self.close()
                raise
