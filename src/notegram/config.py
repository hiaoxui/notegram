"""Configuration discovery shared by the dependency-free client and server."""

import math
import os
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


def redact_config(value):
    """Return a diagnostic copy with credentials masked, including nested settings."""
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if any(
                part in key.lower()
                for part in (
                    "token",
                    "secret",
                    "password",
                    "passwd",
                    "key",
                    "credential",
                    "authorization",
                )
            )
            else redact_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


@dataclass(frozen=True)
class ConfigSource:
    kind: str
    name: str
    values: dict


def config_source(kind, name, values):
    return ConfigSource(kind, str(name), redact_config(values))


def _merge(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _files(path: Path):
    path = path.expanduser()
    if path.is_dir():
        return sorted(
            p
            for p in path.glob("*notegram*.toml")
            if not p.name.endswith((".example.toml", ".sample.toml"))
        )
    return [path] if path.is_file() else []


def load_config(path=None, *, sources: list[ConfigSource] | None = None) -> dict:
    """Merge system, user, working-directory, selected-file, then environment config.

    An explicit path must exist. Legacy WALLESS locations remain supported.
    """
    home = Path.home()
    if sources is not None:
        discovery = ["XDG_CONFIG_HOME", "WALLESS_ROOT", "WALLESS_CONFIG"]
        if path is None:
            discovery.append("NOTEGRAM_CONFIG")
        for variable in discovery:
            if os.environ.get(variable):
                sources.append(
                    config_source(
                        "discovery environment", variable, {variable: os.environ[variable]}
                    )
                )
    locations = [
        Path("/etc/notegram.toml"),
        Path("/etc/notegram"),
        home / "walless.config.d",
        home / ".config",
        Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "notegram",
    ]
    if os.environ.get("WALLESS_ROOT"):
        locations.append(Path(os.environ["WALLESS_ROOT"]) / "walless.config.d")
    if os.environ.get("WALLESS_CONFIG"):
        locations.append(Path(os.environ["WALLESS_CONFIG"]))
    locations.append(Path.cwd())
    selected = path if path is not None else os.environ.get("NOTEGRAM_CONFIG")
    if selected is not None:
        selected = Path(selected).expanduser()
        if not selected.exists():
            raise ConfigError(f"Configuration path does not exist: {selected}")
        locations.append(selected)
    result: dict = {}
    for location in locations:
        for filename in _files(location):
            try:
                with filename.open("rb") as stream:
                    document = tomllib.load(stream)
                section = document.get("notegram", {})
                if not isinstance(section, dict):
                    raise ConfigError(f"Expected a [notegram] table in {filename}")
                _merge(result, section)
                if sources is not None:
                    sources.append(config_source("file", filename.resolve(), section))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ConfigError(f"Cannot read configuration: {filename}") from exc
    for key in ("client", "server", "telegram"):
        if key in result and not isinstance(result[key], dict):
            raise ConfigError(f"Expected a [notegram.{key}] table")
    env = {
        "NOTEGRAM_URL": ("url",),
        "NOTEGRAM_TOKEN": ("token",),
        "NOTEGRAM_HOST": ("server", "host"),
        "NOTEGRAM_PORT": ("server", "port"),
        "NOTEGRAM_DATABASE": ("server", "database"),
        "NOTEGRAM_TELEGRAM_TOKEN": ("telegram", "token"),
    }
    for variable, keys in env.items():
        if variable in os.environ:
            current = result
            for key in keys[:-1]:
                current = current.setdefault(key, {})
            current[keys[-1]] = os.environ[variable]
            if sources is not None:
                sources.append(
                    config_source("environment", variable, {".".join(keys): os.environ[variable]})
                )
    return result


def positive(value, name: str, cast=float):
    try:
        parsed = cast(value)
        if isinstance(value, bool) or (
            cast is int and isinstance(value, float) and value != parsed
        ):
            raise ValueError
        valid = math.isfinite(parsed) and parsed > 0
    except (ValueError, TypeError, OverflowError):
        raise ConfigError(f"{name} must be positive") from None
    if not valid:
        raise ConfigError(f"{name} must be positive")
    return parsed


def shared_token(config: dict) -> str:
    # Old configurations used this secret for /link. It now authenticates both APIs.
    token = config.get("token", config.get("telegram", {}).get("secret", ""))
    if not isinstance(token, str) or any(not 33 <= ord(c) <= 126 for c in token):
        raise ConfigError("NOTEGRAM_TOKEN must contain only visible ASCII characters")
    return token
