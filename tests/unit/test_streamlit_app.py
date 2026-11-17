from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from tests.conftest import REPLAY_SCRIPT

APP = Path("src/data_navigator/ui/streamlit_app.py").resolve()


@pytest.fixture
def app(
    enriched_repo: SqliteCatalogRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AppTest:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f"storage: {{url: 'sqlite:///{enriched_repo.engine.url.database}'}}",
                "starburst: {end_user: {source: fixture, fixture_access: {alice: ['cos.*.*']}}}",
                f"llm: {{provider: replay, replay_path: '{REPLAY_SCRIPT}'}}",
                "audit: {path: null}",
            ]
        )
    )
    monkeypatch.setenv("DN_CONFIG_PATH", str(config))
    monkeypatch.setenv("DN_END_USER_USER", "alice")
    test = AppTest.from_file(str(APP), default_timeout=60)
    test.run()
    return test


def test_page_renders_sql_and_sample_confirmation(app: AppTest) -> None:
    assert not app.exception
    assert "Signed in as **alice**" in app.caption[0].value
    app.chat_input[0].set_value("What is the total balance per customer segment today?").run()
    assert not app.exception
    assert any("Validated read-only SQL" in m.value for m in app.markdown)
    assert app.code[0].value.startswith("SELECT c.segment_code")

    app.chat_input[0].set_value("show me a sample").run()
    assert not app.exception
    labels = [b.label for b in app.button]
    assert any("Allow sample" in label for label in labels) and any(
        "Decline" in label for label in labels
    )
    decline = next(b for b in app.button if "Decline" in b.label)
    decline.click().run()
    assert not app.exception and not app.button

    app.chat_input[0].set_value("show me a sample").run()
    allow = next(b for b in app.button if "Allow sample" in b.label)
    allow.click().run()
    assert not app.exception
    assert any("Sample:" in m.value for m in app.markdown) and len(app.dataframe) == 1


def test_page_reports_startup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("llm: {provider: none}\n")
    monkeypatch.setenv("DN_CONFIG_PATH", str(config))
    monkeypatch.setenv("DN_END_USER_USER", "alice")
    test = AppTest.from_file(str(APP), default_timeout=60).run()
    assert test.error and "cannot start the agent" in test.error[0].value
