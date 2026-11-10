"""One-release fallbacks for the pre-rename names (WOWQUESTER_* variables, wowquester.<name> credentials)."""

from questersboard import envvars, secrets


def test_new_env_name_wins(monkeypatch):
    monkeypatch.setenv("QUESTERSBOARD_CONFIG", "/etc/questersboard/config.toml")
    monkeypatch.setenv("WOWQUESTER_CONFIG", "/etc/old/config.toml")
    assert envvars.getenv("CONFIG") == "/etc/questersboard/config.toml"


def test_old_env_name_still_read(monkeypatch):
    monkeypatch.delenv("QUESTERSBOARD_CONFIG", raising=False)
    monkeypatch.setenv("WOWQUESTER_CONFIG", "/etc/old/config.toml")
    assert envvars.getenv("CONFIG") == "/etc/old/config.toml"
    monkeypatch.delenv("WOWQUESTER_CONFIG")
    assert envvars.getenv("CONFIG", "dflt") == "dflt"


def test_old_secret_env_still_read(monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("QUESTERSBOARD_SECRET_FIELD_KEY", raising=False)
    monkeypatch.setenv("WOWQUESTER_SECRET_FIELD_KEY", "fixture-value")
    assert secrets.get_secret("field-key").reveal() == "fixture-value"


def test_credential_new_then_old_name(monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    (tmp_path / "wowquester.field-key").write_text("old-fixture\n")
    assert secrets.get_secret("field-key").reveal() == "old-fixture"
    (tmp_path / "questersboard.field-key").write_text("new-fixture\n")
    assert secrets.get_secret("field-key").reveal() == "new-fixture"
