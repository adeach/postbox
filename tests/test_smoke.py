def test_settings_creates_data_dir(tmp_path):
    from courier.config import load_settings

    s = load_settings(str(tmp_path / "data"))
    assert s.data_dir.exists()
    assert s.db_path.name == "courier.db"
