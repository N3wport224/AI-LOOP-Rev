"""Cheap network reachability probe, so offline periods pause the loop instead of tripping breakers."""

from __future__ import annotations

import os
import socket
from typing import Callable
from urllib.parse import urlsplit


def _targets(hosts: list[str]) -> list[tuple[str, int]]:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        # Behind a proxy the proxy is the only thing we need to reach.
        p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        if p.hostname:
            return [(p.hostname, p.port or 80)]
    out = []
    for h in hosts:
        host, _, port = h.rpartition(":") if ":" in h else (h, "", "443")
        out.append((host or h, int(port or 443)))
    return out


def is_online(hosts: list[str], timeout: float = 3.0,
              connect: Callable[..., socket.socket] = socket.create_connection) -> bool:
    """True if any host accepts a TCP connection. An empty host list disables the check."""
    targets = _targets(hosts)
    if not targets:
        return True
    for host, port in targets:
        try:
            connect((host, port), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False
