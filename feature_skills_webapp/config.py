import os
from pathlib import Path

DEFAULT_PORT = 8800
DEFAULT_HOST = "127.0.0.1"


class ConfigError(Exception):
    pass


def host() -> str:
    """The interface the server binds to.

    Defaults to localhost. Set ``FEATURE_SKILLS_WEBAPP_HOST=0.0.0.0`` (or a
    specific interface address) to reach the service from other machines on a
    trusted network.
    """
    raw = os.environ.get("FEATURE_SKILLS_WEBAPP_HOST")
    if not raw:
        return DEFAULT_HOST
    return raw


def db_path() -> Path:
    override = os.environ.get("FEATURE_SKILLS_WEBAPP_DB")
    if override:
        return Path(override)
    xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return xdg / "feature-skills-webapp" / "db.sqlite"


DEFAULT_WAIT_TIMEOUT = 240.0


def wait_timeout() -> float:
    raw = os.environ.get("FEATURE_SKILLS_WEBAPP_WAIT_TIMEOUT")
    if not raw:
        return DEFAULT_WAIT_TIMEOUT
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"FEATURE_SKILLS_WEBAPP_WAIT_TIMEOUT must be a float, got {raw!r}") from e


def port() -> int:
    raw = os.environ.get("FEATURE_SKILLS_WEBAPP_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"FEATURE_SKILLS_WEBAPP_PORT must be an integer, got {raw!r}") from e
    if not (1 <= value <= 65535):
        raise ConfigError(f"FEATURE_SKILLS_WEBAPP_PORT out of range: {value}")
    return value
