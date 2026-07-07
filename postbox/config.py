import os
from dataclasses import dataclass
from pathlib import Path

import yaml


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _yaml_config(base: Path) -> dict:
    path = base / "config.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}


def _value(name: str, config: dict, key: str, default):
    if name in os.environ:
        return os.environ[name]
    v = config.get(key)
    return v if v is not None else default


def _int_cfg(name: str, config: dict, key: str, default: int) -> int:
    v = config.get(key)
    return _int(name, v if v is not None else default)


def _optional_value(name: str, config: dict, key: str) -> str | None:
    if name in os.environ:
        return os.environ.get(name) or None
    return config.get(key) or None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8765
    # Fleet supervisor knobs (all overridable via env)
    observer_token: str | None = None       # POSTBOX_OBSERVER_TOKEN; guards /observer + /fleet when set
    max_concurrent: int = 5                  # simultaneous headless turns
    reconcile_interval: int = 20             # safety-net rescan seconds
    agent_cooldown: int = 5                  # min seconds between a fleet agent's turns
    max_runtime: int = 900                   # kill a turn after this many seconds
    auto_disable_after: int = 5              # disable an agent after N consecutive failed turns
    backoff_base: int = 5                    # backoff = base * 2^fail_count, capped
    backoff_cap: int = 300
    instance: str | None = None
    peers_seed: tuple[dict, ...] = ()
    terminal_cmd: str | None = None    # override the copilot launch command for spawned terminals
    spawn_wait: int = 25               # seconds to wait for a spawned agent to register
    password: str | None = None        # UI login password (auth.password); guards /observer + /fleet + /peers

    @property
    def public_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_settings(data_dir: str | None = None) -> Settings:
    base = Path(data_dir or os.environ.get("POSTBOX_DATA_DIR", "~/.postbox")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    config = _yaml_config(base)
    fleet = config.get("fleet") if isinstance(config.get("fleet"), dict) else {}
    auth = config.get("auth") if isinstance(config.get("auth"), dict) else {}
    peers = config.get("peers") if isinstance(config.get("peers"), list) else []
    return Settings(
        data_dir=base,
        db_path=base / "postbox.db",
        host=_value("POSTBOX_HOST", config, "host", "127.0.0.1"),
        port=_int_cfg("POSTBOX_PORT", config, "port", 8765),
        observer_token=_optional_value("POSTBOX_OBSERVER_TOKEN", config, "observer_token"),
        max_concurrent=_int_cfg("POSTBOX_MAX_CONCURRENT", fleet, "max_concurrent", 5),
        reconcile_interval=_int_cfg("POSTBOX_RECONCILE_INTERVAL", fleet, "reconcile_interval", 20),
        agent_cooldown=_int_cfg("POSTBOX_AGENT_COOLDOWN", fleet, "agent_cooldown", 5),
        max_runtime=_int_cfg("POSTBOX_MAX_RUNTIME", fleet, "max_runtime", 900),
        auto_disable_after=_int_cfg("POSTBOX_AUTO_DISABLE_AFTER", fleet, "auto_disable_after", 5),
        backoff_base=_int_cfg("POSTBOX_BACKOFF_BASE", fleet, "backoff_base", 5),
        backoff_cap=_int_cfg("POSTBOX_BACKOFF_CAP", fleet, "backoff_cap", 300),
        instance=_optional_value("POSTBOX_INSTANCE", config, "instance"),
        peers_seed=tuple(peer for peer in peers if isinstance(peer, dict)),
        terminal_cmd=_optional_value("POSTBOX_TERMINAL_CMD", config, "terminal_cmd"),
        spawn_wait=_int_cfg("POSTBOX_SPAWN_WAIT", config, "spawn_wait", 25),
        password=_optional_value("POSTBOX_PASSWORD", auth, "password"),
    )
