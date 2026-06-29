import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8765


def load_settings(data_dir: str | None = None) -> Settings:
    base = Path(data_dir or os.environ.get("COURIER_DATA_DIR", "~/.courier")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return Settings(data_dir=base, db_path=base / "courier.db")
