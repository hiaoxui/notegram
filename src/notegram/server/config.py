"""Server-only configuration."""

from dataclasses import dataclass, field
from pathlib import Path

from ..config import ConfigError, load_config, positive, shared_token


@dataclass(frozen=True)
class ServerConfig:
    token: str = field(repr=False)
    database: str = "notegram.sqlite3"
    host: str = "127.0.0.1"
    port: int = 8765
    telegram_token: str = field(default="", repr=False)

    @classmethod
    def load(cls, path=None):
        raw = load_config(path)
        token = shared_token(raw)
        if not token:
            raise ConfigError("Set NOTEGRAM_TOKEN or [notegram].token before starting the server")
        server = raw.get("server", {})
        port = positive(server.get("port", 8765), "port", int)
        if port > 65535:
            raise ConfigError("port must be at most 65535")
        database = str(Path(server.get("database", "notegram.sqlite3")).expanduser())
        return cls(
            token,
            database,
            server.get("host", "127.0.0.1"),
            port,
            raw.get("telegram", {}).get("token", ""),
        )
