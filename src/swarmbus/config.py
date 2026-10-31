"""Optional INI config file of defaults for the `swarmbus` CLI's host settings.

Precedence is flag > env var > config file > built-in default. Most of that is
Click's own `default_map` order; the outbox path is resolved by hand in cli.py.

No message raised or warned from here ever contains a value from the file:
configparser quotes the offending line in its own messages, and a line may hold
a password, so those messages are never used.
"""
from __future__ import annotations

import configparser
import math
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """A config file that cannot be used. The message always names the file."""


@dataclass
class LoadedConfig:
    path: Path | None = None
    params: dict[str, object] = field(default_factory=dict)
    outbox_path: str | None = None
    warnings: list[str] = field(default_factory=list)


# (section, key) -> (Click param name, value kind)
_KEYS: dict[tuple[str, str], tuple[str, str]] = {
    ("broker", "host"): ("broker", "string"),
    ("broker", "port"): ("port", "port"),
    ("broker", "username"): ("username", "string"),
    ("broker", "password"): ("password", "string"),
    ("broker", "ca_cert"): ("ca_cert", "path"),
    ("broker", "client_cert"): ("client_cert", "path"),
    ("broker", "client_key"): ("client_key", "path"),
    ("broker", "tls"): ("tls", "boolean"),
    ("agent", "id"): ("agent_id", "string"),
    ("agent", "state_dir"): ("state_dir", "path"),
    ("agent", "inbox"): ("inbox", "path"),
    ("agent", "invoke"): ("invoke_cmd", "string"),
    ("agent", "invoke_timeout"): ("invoke_timeout", "number"),
    ("agent", "on_trip"): ("on_trip", "string"),
    ("limits", "hop_limit"): ("hop_limit", "integer"),
    ("limits", "rate_limit"): ("rate_limit", "integer"),
    ("limits", "rate_cooldown"): ("rate_cooldown", "number"),
    # [outbox] path is not a Click default: it goes to LoadedConfig.outbox_path.
    ("outbox", "path"): ("", "path"),
    ("outbox", "max_bytes"): ("outbox_max_bytes", "integer"),
    ("outbox", "keep"): ("outbox_keep", "integer"),
}
_SECTIONS = {section for section, _ in _KEYS}

_KIND_WORDS = {
    "port": "an integer from 1 to 65535",
    "integer": "an integer >= 0",
    "number": "a number >= 0",
    "boolean": "one of 1/yes/true/on or 0/no/false/off",
}


def default_config_path() -> Path:
    return Path.home() / ".swarmbus" / "config.ini"


def _convert(kind: str, value: str):
    """Convert one value; raise ValueError when it has the wrong type or range."""
    if kind == "string":
        return value
    if kind == "path":
        return os.path.expanduser(value)
    if kind == "integer":
        number = int(value)
        if number < 0:
            raise ValueError
        return number
    if kind == "port":
        number = int(value)
        if not 1 <= number <= 65535:
            raise ValueError
        return number
    if kind == "number":
        real = float(value)
        if not math.isfinite(real) or real < 0:
            raise ValueError
        return real
    if kind == "boolean":
        return configparser.ConfigParser.BOOLEAN_STATES[value.lower()]
    raise AssertionError(kind)


def _parse_error_lines(exc: configparser.Error) -> str:
    lineno = getattr(exc, "lineno", None)
    if lineno is not None:
        return str(lineno)
    return ", ".join(str(n) for n, _ in getattr(exc, "errors", ()))


def load_config(explicit: str | None) -> LoadedConfig:
    """Read the config file named by `explicit`, else the default path if it exists."""
    path = Path(explicit).expanduser() if explicit else default_config_path()
    if not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return LoadedConfig()

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from None

    parser = configparser.ConfigParser(interpolation=None, default_section="")
    try:
        parser.read_string(text, source=str(path))
    except configparser.Error as exc:
        raise ConfigError(
            f"cannot read config {path}: {type(exc).__name__} at line {_parse_error_lines(exc)}"
        ) from None

    params: dict[str, object] = {}
    outbox_path: str | None = None
    warnings: list[str] = []
    for section in parser.sections():
        if section not in _SECTIONS:
            warnings.append(f"config {path}: unknown section [{section}] (ignored)")
            continue
        for key, value in parser.items(section):
            if (section, key) not in _KEYS:
                warnings.append(f"config {path}: unknown key [{section}] {key} (ignored)")
                continue
            if value == "":
                continue
            param, kind = _KEYS[(section, key)]
            try:
                converted = _convert(kind, value)
            except (ValueError, KeyError):
                raise ConfigError(
                    f"config {path}: [{section}] {key} must be {_KIND_WORDS[kind]}"
                ) from None
            if (section, key) == ("outbox", "path"):
                outbox_path = converted
            else:
                params[param] = converted

    if "password" in params:
        mode = os.stat(path).st_mode
        if mode & 0o077:
            raise ConfigError(
                f"config {path} holds [broker] password but group or others can read it "
                f"(mode {oct(mode & 0o777)}); chmod 600 {path}, "
                "or move the password to SWARMBUS_BROKER_PASSWORD"
            )

    return LoadedConfig(path=path, params=params, outbox_path=outbox_path, warnings=warnings)
