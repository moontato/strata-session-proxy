"""Strict, generic configuration. No network access while loading configuration."""

import math
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

import yaml


@dataclass
class Server:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8082
    backend_url: str = "http://127.0.0.1:8081"
    connect_timeout_seconds: float = 10
    read_timeout_seconds: float = 0  # zero: unlimited, including SSE idle time
    shutdown_timeout_seconds: float = 3600
    detection_max_bytes: int = 8 * 1024 * 1024


@dataclass
class Snapshots:
    enabled: bool = False
    exclusive_backend: bool = False
    compatibility_key: str = ""  # operator-controlled deployment revision; never a credential
    directory: str = "/var/lib/strata-session-proxy/snapshots"
    state_directory: str = "/var/lib/strata-session-proxy/state"
    required_mount: str = ""  # exact mountpoint, checked on EVERY operation
    save_threshold_percent: float = 30
    auto_save: bool = True
    max_snapshots: int = 5
    eviction_policy: str = "lru"
    min_free_disk_gib: float = 10
    operation_timeout_seconds: float = 7200
    idle_timeout_seconds: float = 120
    poll_interval_seconds: float = 0.1
    admin_token_env: str = "STRATA_PROXY_BACKEND_TOKEN"


@dataclass
class Estimates:
    power_watts: float = 0
    prefill_tokens_per_second: float = 0
    electricity_price_per_kwh: float = 0


@dataclass
class Config:
    server: Server = field(default_factory=Server)
    snapshots: Snapshots = field(default_factory=Snapshots)
    estimates: Estimates = field(default_factory=Estimates)

    def validate(self):
        for section in (self.server, self.snapshots, self.estimates):
            for setting in fields(section):
                name, kind = setting.name, setting.type
                value = getattr(section, name)
                if kind is bool:
                    good = type(value) is bool
                elif kind in (float, int):
                    good = type(value) in (int, float) and math.isfinite(value)
                    if kind is int:
                        good = good and type(value) is int
                else:
                    good = isinstance(value, str)
                if not good:
                    raise ValueError(f"invalid type/value for {name}")
        s, p = self.server, self.snapshots
        try:
            url = urlsplit(s.backend_url)
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError()
        except ValueError:
            raise ValueError("invalid backend_url authority or port") from None
        if (
            s.backend_url != s.backend_url.strip()
            or url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("backend_url must be an http(s) origin without credentials, path or query")
        if not 1 <= s.listen_port <= 65535 or s.detection_max_bytes < 0:
            raise ValueError("invalid listen_port or detection_max_bytes")
        if not 0 <= p.save_threshold_percent <= 100 or p.max_snapshots < 1 or p.eviction_policy != "lru":
            raise ValueError("invalid threshold, snapshot limit or eviction policy")
        if any(v < 0 for v in asdict(self.estimates).values()) or p.min_free_disk_gib < 0:
            raise ValueError("estimates and disk reserve must be nonnegative")
        if s.connect_timeout_seconds <= 0 or s.read_timeout_seconds < 0 or s.shutdown_timeout_seconds <= 0:
            raise ValueError("invalid HTTP timeout")
        if min(p.operation_timeout_seconds, p.idle_timeout_seconds, p.poll_interval_seconds) <= 0:
            raise ValueError("snapshot timeouts must be positive")
        if p.enabled:
            if not p.exclusive_backend or not p.compatibility_key.strip():
                raise ValueError("managed mode requires exclusive_backend and a compatibility_key")
            if not all(Path(v).is_absolute() for v in (p.directory, p.state_directory)):
                raise ValueError("storage directories must be absolute")
            if Path(p.directory).resolve() == Path(p.state_directory).resolve():
                raise ValueError("use separate snapshot and state directories")
            if p.required_mount and (
                not Path(p.required_mount).is_absolute()
                or not Path(p.directory).is_relative_to(p.required_mount)
            ):
                raise ValueError("required_mount must be an absolute ancestor of directory")
        return self


def parse_yaml(value):
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        # YAML exceptions include source lines: never echo private configuration contents.
        raise ValueError("invalid YAML configuration syntax") from None


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("STRATA_PROXY_CONFIG")
    data = {}
    if path:
        with open(path, encoding="utf-8") as f:
            data = parse_yaml(f) or {}
    if not isinstance(data, dict) or set(data) - {"server", "snapshots", "estimates"}:
        raise ValueError("unknown configuration section")
    result = Config()
    for name in ("server", "snapshots", "estimates"):
        section = getattr(result, name)
        values = data.get(name, {})
        if not isinstance(values, dict) or set(values) - set(asdict(section)):
            raise ValueError(f"unknown setting in {name}")
        for key in asdict(section):
            env = f"STRATA_PROXY__{name}__{key}".upper()
            value = (
                parse_yaml(os.environ[env]) if env in os.environ else values.get(key, getattr(section, key))
            )
            setattr(section, key, value)
    return result.validate()
