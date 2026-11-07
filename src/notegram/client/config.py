"""Client configuration and endpoint validation."""

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..config import ConfigError, ConfigSource, config_source, load_config, positive, shared_token


def validate_url(url: str) -> str:
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise ConfigError("Invalid Notegram URL")
    try:
        parts = urlsplit(url)
        _ = parts.port  # Access validates malformed port numbers.
        host = parts.hostname
    except (ValueError, TypeError):
        raise ConfigError("Invalid Notegram URL") from None
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ConfigError("Notegram URL must be an HTTP(S) base URL without credentials or query")
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if not loopback:
            raise ConfigError("Remote Notegram endpoints require HTTPS")
    return url.rstrip("/")


@dataclass(frozen=True)
class ClientConfig:
    url: str
    token: str = field(repr=False)
    queue_size: int = 1000
    timeout: float = 5.0
    retries: int = 3
    flush_timeout: float = 5.0
    sources: tuple[ConfigSource, ...] = field(default=(), repr=False, compare=False)

    @classmethod
    def load(cls, path=None, **overrides):
        sources = []
        raw = load_config(path, sources=sources)
        values = raw.get("client", {}).copy()
        values.update(url=raw.get("url", "http://127.0.0.1:8765"), token=shared_token(raw))
        for key, value in overrides.items():
            if value is not None:
                values[key] = value
                sources.append(config_source("argument", key, {key: value}))
        url = validate_url(values["url"])
        token = shared_token({"token": values["token"]})
        queue_size = positive(values.get("queue_size", 1000), "queue_size", int)
        retries = values.get("retries", 3)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ConfigError("retries must be a nonnegative integer")
        return cls(
            url,
            token,
            queue_size,
            positive(values.get("timeout", 5), "timeout"),
            retries,
            positive(values.get("flush_timeout", 5), "flush_timeout"),
            tuple(sources),
        )
