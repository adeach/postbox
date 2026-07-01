import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


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

    @property
    def public_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_settings(data_dir: str | None = None) -> Settings:
    base = Path(data_dir or os.environ.get("POSTBOX_DATA_DIR", "~/.postbox")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=base,
        db_path=base / "postbox.db",
        host=os.environ.get("POSTBOX_HOST", "127.0.0.1"),
        port=_int("POSTBOX_PORT", 8765),
        observer_token=os.environ.get("POSTBOX_OBSERVER_TOKEN") or None,
        max_concurrent=_int("POSTBOX_MAX_CONCURRENT", 5),
        reconcile_interval=_int("POSTBOX_RECONCILE_INTERVAL", 20),
        agent_cooldown=_int("POSTBOX_AGENT_COOLDOWN", 5),
        max_runtime=_int("POSTBOX_MAX_RUNTIME", 900),
        auto_disable_after=_int("POSTBOX_AUTO_DISABLE_AFTER", 5),
    )
