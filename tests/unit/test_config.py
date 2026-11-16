from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from data_navigator.config import Secrets, Settings, load_secrets, load_settings


def test_defaults_run_against_fixtures() -> None:
    settings = load_settings(None)
    assert settings.metadata.source == "fixture"
    assert settings.query_history.source == "jsonl"
    assert settings.sql_files.source == "local"
    assert settings.storage.backend == "sqlite"


def test_yaml_then_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  url: sqlite:///from-yaml.db\nsamples:\n  max_rows: 7\n")
    assert load_settings(config).storage.url == "sqlite:///from-yaml.db"

    monkeypatch.setenv("DN_STORAGE__URL", "sqlite:///from-env.db")
    settings = load_settings(config)
    assert settings.storage.url == "sqlite:///from-env.db"
    assert settings.samples.max_rows == 7  # untouched by the environment


def test_example_config_loads() -> None:
    settings = load_settings(Path("config.example.yaml"))
    assert settings.query_history.columns.schema_ == "schema"
    assert settings.llm.base_url is None


def test_secrets_are_rejected_in_yaml(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("starburst:\n  builder:\n    user: u\n    password: hunter2\n")
    with pytest.raises(ValidationError):
        load_settings(config)


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  api_key: nope\n")
    with pytest.raises(ValidationError):
        load_settings(config)


def test_yaml_must_be_a_mapping(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_settings(config)
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert isinstance(load_settings(empty), Settings)


def test_llm_base_url_has_no_default() -> None:
    assert Settings().llm.base_url is None


def test_samples_default_to_not_sending_to_llm() -> None:
    settings = Settings()
    assert settings.samples.send_to_llm is False
    assert settings.samples.max_rows == 10


def test_secrets_from_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DN_BUILDER_PASSWORD", "s3cret")
    monkeypatch.setenv("DN_GITLAB_TOKEN", "glpat-xyz")
    secrets = load_secrets(None)
    assert secrets.builder_password is not None
    assert secrets.builder_password.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(secrets)
    assert "glpat-xyz" not in str(secrets)
    assert secrets.llm_api_key is None


def test_secrets_from_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DN_LLM_API_KEY=abc\nDN_END_USER_USER=alice\n")
    secrets = Secrets(_env_file=env_file)  # type: ignore[call-arg]
    assert secrets.end_user_user == "alice"
    assert secrets.llm_api_key is not None
    assert secrets.llm_api_key.get_secret_value() == "abc"


def test_template_placeholder_must_be_identifier(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("template:\n  placeholder: 'not valid'\n")
    with pytest.raises(ValidationError):
        load_settings(config)
