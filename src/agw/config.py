"""Load, merge, validate and render agw configuration (defaults, user TOML, project TOML, env, CLI)."""
from __future__ import annotations

import getpass
import json
import os
import pathlib
import tempfile
import tomllib
from typing import Any, Callable

from agw.models import Config

CONFIG_KEYS: tuple[str, ...] = (
    "model", "max_parallel", "timeout_seconds", "sandbox", "retry_limit", "accept",
    "accept_timeout_seconds", "setup", "deny_write", "agy_path", "worktree_dir",
)

PROJECT_CONFIG_NAME = ".agw.toml"


class ConfigError(Exception):
    """Raised for an unreadable or invalid configuration. Message: '<file>: <key>: <problem>'."""


def user_config_path() -> pathlib.Path:
    """Where the user config lives: $AGW_CONFIG, else the platform default."""
    explicit = os.environ.get("AGW_CONFIG")
    if explicit:
        return pathlib.Path(os.path.expanduser(explicit))
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return pathlib.Path(appdata) / "agw" / "config.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return pathlib.Path(xdg) / "agw" / "config.toml"
    return pathlib.Path.home() / ".config" / "agw" / "config.toml"


def default_worktree_dir() -> str:
    """The default parent directory for worker worktrees: <system temp>/agw-<user>."""
    try:
        user = getpass.getuser() or "user"
    except Exception:  # noqa: BLE001 - getuser can raise KeyError/OSError/ImportError on odd systems
        user = "user"
    return os.path.realpath(os.path.join(tempfile.gettempdir(), f"agw-{user}"))


# ---- validation --------------------------------------------------------------------------------

def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return "must be a string"
    return None


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return "must be a string"
    if not value:
        return "must not be empty"
    return None


def _int_range(lo: int, hi: int) -> Callable[[object], str | None]:
    def check(value: object) -> str | None:
        if not _is_int(value):
            return "must be an integer"
        if not lo <= value <= hi:  # type: ignore[operator]
            return f"must be between {lo} and {hi}"
        return None
    return check


def _boolean(value: object) -> str | None:
    return None if isinstance(value, bool) else "must be a boolean"


def _string_list(value: object) -> str | None:
    return None if _is_str_list(value) else "must be a list of strings"


def _agy_path(value: object) -> str | None:
    if isinstance(value, str):
        return None if value else "must not be empty"
    if _is_str_list(value):
        if not value or not value[0]:  # type: ignore[index]
            return "must not be empty"
        return None
    return "must be a string or a list of strings"


_VALIDATORS: dict[str, Callable[[object], str | None]] = {
    "model": _nonempty_string,
    "max_parallel": _int_range(1, 8),
    "timeout_seconds": _int_range(60, 7200),
    "sandbox": _boolean,
    "retry_limit": _int_range(0, 5),
    "accept": _string_list,
    "accept_timeout_seconds": _int_range(10, 7200),
    "setup": _string_list,
    "deny_write": _string_list,
    "agy_path": _agy_path,
    "worktree_dir": _string,
}
assert tuple(_VALIDATORS) == CONFIG_KEYS


def _validate_layer(label: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate one layer's keys and values; return a copy. Raises ConfigError on the first problem."""
    for key, value in data.items():
        validator = _VALIDATORS.get(key)
        if validator is None:
            raise ConfigError(f"{label}: {key}: unknown key")
        problem = validator(value)
        if problem:
            raise ConfigError(f"{label}: {key}: {problem}")
    return dict(data)


def _read_toml(path: pathlib.Path) -> dict[str, Any] | None:
    """Parse a TOML file. None when the file does not exist."""
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror or exc}") from exc
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc


def _normalize_agy_path(value: object) -> list[str]:
    return [value] if isinstance(value, str) else list(value)  # type: ignore[call-overload]


def load_config(main_root: str | None, overrides: dict[str, object] | None = None) -> Config:
    """Merge defaults, the user file, the project file, $AGW_AGY and `overrides`; validate every layer."""
    values: dict[str, Any] = {k: getattr(Config(), k) for k in CONFIG_KEYS}
    sources: dict[str, str] = {k: "default" for k in CONFIG_KEYS}

    def apply(label: str, layer: dict[str, Any]) -> None:
        for key, value in _validate_layer(label, layer).items():
            values[key] = value
            sources[key] = label

    layers: list[tuple[str, pathlib.Path]] = [(str(user_config_path()), user_config_path())]
    if main_root:
        project = pathlib.Path(main_root) / PROJECT_CONFIG_NAME
        layers.append((str(project), project))
    for label, path in layers:
        data = _read_toml(path)
        if data is not None:
            apply(label, data)

    env_agy = os.environ.get("AGW_AGY")
    if env_agy:   # an empty value is treated as unset
        values["agy_path"] = [env_agy]
        sources["agy_path"] = "env"

    if overrides:
        apply("cli", {k: v for k, v in overrides.items() if v is not None})

    values["agy_path"] = _normalize_agy_path(values["agy_path"])
    worktree_dir = values["worktree_dir"]
    values["worktree_dir"] = (os.path.realpath(os.path.expanduser(worktree_dir))
                              if worktree_dir else default_worktree_dir())
    values["deny_write"] = [os.path.realpath(os.path.expanduser(p)) for p in values["deny_write"]]
    values["accept"] = list(values["accept"])
    values["setup"] = list(values["setup"])
    return Config(**values, sources=sources)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)   # JSON strings and string arrays are valid TOML


def render_config(cfg: Config) -> str:
    """One `key = value  # source` line per key, in CONFIG_KEYS order."""
    lines = [f"{key} = {_toml_value(getattr(cfg, key))}  # {cfg.sources.get(key, 'default')}"
             for key in CONFIG_KEYS]
    return "\n".join(lines) + "\n"
