from postbox.config import load_settings


RELEVANT_ENV = [
    "POSTBOX_DATA_DIR",
    "POSTBOX_HOST",
    "POSTBOX_PORT",
    "POSTBOX_OBSERVER_TOKEN",
    "POSTBOX_MAX_CONCURRENT",
    "POSTBOX_RECONCILE_INTERVAL",
    "POSTBOX_AGENT_COOLDOWN",
    "POSTBOX_MAX_RUNTIME",
    "POSTBOX_AUTO_DISABLE_AFTER",
    "POSTBOX_BACKOFF_BASE",
    "POSTBOX_BACKOFF_CAP",
    "POSTBOX_INSTANCE",
]


def clear_env(monkeypatch):
    for name in RELEVANT_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_without_config_or_env(tmp_path, monkeypatch):
    clear_env(monkeypatch)

    settings = load_settings(tmp_path)

    assert settings.data_dir == tmp_path
    assert settings.db_path == tmp_path / "postbox.db"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.instance is None
    assert settings.peers_seed == ()


def test_config_yaml_sets_top_level_fleet_and_peers(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        """
host: 0.0.0.0
port: 9999
instance: postbox1
fleet:
  max_concurrent: 12
peers:
  - name: postbox2
    url: http://vm:8080
    token: shared-secret
""".lstrip()
    )

    settings = load_settings(tmp_path)

    assert settings.host == "0.0.0.0"
    assert settings.port == 9999
    assert settings.instance == "postbox1"
    assert settings.max_concurrent == 12
    assert settings.peers_seed == (
        {"name": "postbox2", "url": "http://vm:8080", "token": "shared-secret"},
    )


def test_env_overrides_config_yaml_values(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        """
port: 9999
instance: postbox1
""".lstrip()
    )
    monkeypatch.setenv("POSTBOX_PORT", "7777")
    monkeypatch.setenv("POSTBOX_INSTANCE", "postbox-env")

    settings = load_settings(tmp_path)

    assert settings.port == 7777
    assert settings.instance == "postbox-env"


def test_empty_config_yaml_keeps_backward_compatible_defaults(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    (tmp_path / "config.yaml").write_text("\n")

    settings = load_settings(tmp_path)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.observer_token is None
    assert settings.max_concurrent == 5
    assert settings.reconcile_interval == 20
    assert settings.agent_cooldown == 5
    assert settings.max_runtime == 900
    assert settings.auto_disable_after == 5
    assert settings.backoff_base == 5
    assert settings.backoff_cap == 300
    assert settings.instance is None
    assert settings.peers_seed == ()


def test_null_config_yaml_values_fall_back_to_defaults(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        """
host:
port:
instance:
fleet:
  max_concurrent:
""".lstrip()
    )

    settings = load_settings(tmp_path)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.instance is None
    assert settings.max_concurrent == 5
