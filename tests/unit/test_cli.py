from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from data_navigator.cli import app

runner = CliRunner()
CONFIG = Path("config.example.yaml").resolve()


def test_build_dry_run_creates_no_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "catalog.db"
    monkeypatch.setenv("DN_STORAGE__URL", f"sqlite:///{db}")
    result = runner.invoke(app, ["--config", str(CONFIG), "build", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run, nothing written" in result.output
    assert "12 seen, 12 added" in result.output
    assert not db.exists()


def test_build_refresh_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "nested" / "catalog.db"
    monkeypatch.setenv("DN_STORAGE__URL", f"sqlite:///{db}")
    monkeypatch.setenv("DN_BUILDER_PASSWORD", "never-shown")

    build = runner.invoke(app, ["--config", str(CONFIG), "build"])
    assert build.exit_code == 0, build.output
    assert db.exists() and "23 new, 23 total" in build.output

    monkeypatch.setenv("DN_METADATA__FIXTURE_DIR", "tests/fixtures/information_schema_v2")
    refresh = runner.invoke(
        app,
        [
            "--config",
            str(CONFIG),
            "refresh",
            "--tables",
            "cos.retail_ods.customer,cos.cards_dm.card_spend_monthly",
        ],
    )
    assert refresh.exit_code == 0, refresh.output
    assert "added: cos.cards_dm.card_spend_monthly" in refresh.output
    assert "changed: cos.retail_ods.customer" in refresh.output

    report = runner.invoke(
        app, ["--config", str(CONFIG), "report", "--top", "3", "--out", str(tmp_path / "r.md")]
    )
    assert report.exit_code == 0, report.output
    assert "## Join graph" in (tmp_path / "r.md").read_text()

    shown = runner.invoke(app, ["--config", str(CONFIG), "show-config"])
    assert shown.exit_code == 0
    assert '"backend": "sqlite"' in shown.output
    assert "never-shown" not in shown.output


def test_missing_config_file_is_an_error() -> None:
    result = runner.invoke(app, ["--config", "nope.yaml", "build", "--dry-run"])
    assert result.exit_code != 0


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "build" in result.output and "refresh" in result.output
