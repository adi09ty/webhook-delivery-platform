"""Checks on the URLs customers ask us to deliver to.

Without these, a customer could register http://169.254.169.254/ or http://10.0.0.5/admin and make the platform
call your internal services for them (this attack is called SSRF).
"""

import ipaddress
import socket
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048


class UnsafeTargetError(ValueError):
    """The URL points at something the platform must not call."""


def ensure_public_host(hostname: str, port: int | None = None) -> None:
    """Raise unless the host is, or resolves only to, public internet addresses."""
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise UnsafeTargetError(f"cannot resolve host {hostname!r}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for address in addresses:
        if not address.is_global:
            raise UnsafeTargetError(f"{hostname!r} points to a non-public address ({address})")


def validate_target_url(url: str, *, allow_private: bool) -> str:
    """Return the url if it is acceptable, otherwise raise ValueError (UnsafeTargetError is a ValueError)."""
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"URL is longer than {MAX_URL_LENGTH} characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("URL must contain a host name")
    if parts.username or parts.password:
        raise ValueError("URL must not contain a username or password")
    if not allow_private:
        if parts.scheme != "https":
            raise ValueError("URL must use https")
        ensure_public_host(parts.hostname, parts.port)
    return url
