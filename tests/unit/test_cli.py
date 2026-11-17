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


# -- phase 2: cards, index, review ------------------------------------------------------


def _replay_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db = tmp_path / "catalog.db"
    monkeypatch.setenv("DN_STORAGE__URL", f"sqlite:///{db}")
    monkeypatch.setenv("DN_LLM__PROVIDER", "replay")
    monkeypatch.setenv("DN_LLM__REPLAY_PATH", str(Path("eval/replay.example.yaml").resolve()))
    return db


def test_build_generates_cards_and_index_with_replay_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _replay_env(monkeypatch, tmp_path)
    build = runner.invoke(app, ["--config", str(CONFIG), "build"])
    assert build.exit_code == 0, build.output
    assert "cards: 12 considered, 12 generated" in build.output
    assert "search index (text + vector): 35 documents, 35 embedded" in build.output

    again = runner.invoke(app, ["--config", str(CONFIG), "cards"])
    assert again.exit_code == 0, again.output
    assert "12 unchanged" in again.output

    forced = runner.invoke(
        app,
        ["--config", str(CONFIG), "cards", "--force", "--tables", "cos.ref.branch", "--dry-run"],
    )
    assert forced.exit_code == 0 and "1 considered, 1 generated" in forced.output

    listed = runner.invoke(app, ["--config", str(CONFIG), "review", "list"])
    assert listed.exit_code == 0 and "cos.retail_ods.account\tai_inferred\t0.90" in listed.output

    shown = runner.invoke(
        app, ["--config", str(CONFIG), "review", "show", "cos.retail_ods.account"]
    )
    assert shown.exit_code == 0 and "key columns:" in shown.output and "- balance:" in shown.output

    edited = runner.invoke(
        app,
        [
            "--config",
            str(CONFIG),
            "review",
            "edit",
            "cos.retail_ods.account",
            "--purpose",
            "Human purpose",
            "--example-questions",
            '["Q?"]',
            "--by",
            "eve",
        ],
    )
    assert edited.exit_code == 0, edited.output
    assert "edited example_questions, purpose; status confirmed" in edited.output

    regenerated = runner.invoke(app, ["--config", str(CONFIG), "cards", "--force"])
    assert regenerated.exit_code == 0 and "1 reviewed (kept)" in regenerated.output
    shown = runner.invoke(
        app, ["--config", str(CONFIG), "review", "show", "cos.retail_ods.account"]
    )
    assert (
        "purpose: Human purpose" in shown.output
        and "confirmed fields: purpose, example_questions" in shown.output
    )

    rejected = runner.invoke(
        app, ["--config", str(CONFIG), "review", "reject", "cos.ref.branch", "--by", "eve"]
    )
    assert rejected.exit_code == 0 and "rejected" in rejected.output
    confirmed = runner.invoke(
        app, ["--config", str(CONFIG), "review", "confirm", "cos.ref.product", "--by", "eve"]
    )
    assert confirmed.exit_code == 0
    status_all = runner.invoke(app, ["--config", str(CONFIG), "review", "list", "--status", "all"])
    assert (
        "cos.ref.branch\trejected" in status_all.output
        and "cos.ref.product\tconfirmed" in status_all.output
    )
    none = runner.invoke(app, ["--config", str(CONFIG), "review", "list", "--status", "bogus"])
    assert none.output.strip() == "no cards"

    index = runner.invoke(app, ["--config", str(CONFIG), "index"])
    assert index.exit_code == 0 and "35 documents" in index.output

    refreshed = runner.invoke(app, ["--config", str(CONFIG), "refresh"])
    assert refreshed.exit_code == 0 and "cards:" in refreshed.output


def test_review_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _replay_env(monkeypatch, tmp_path)
    assert runner.invoke(app, ["--config", str(CONFIG), "build", "--skip-cards"]).exit_code == 0
    missing = runner.invoke(app, ["--config", str(CONFIG), "review", "show", "cos.x.y"])
    assert missing.exit_code == 1
    no_reviewer = runner.invoke(
        app, ["--config", str(CONFIG), "review", "confirm", "cos.ref.branch"]
    )
    assert no_reviewer.exit_code == 2
    unknown = runner.invoke(
        app, ["--config", str(CONFIG), "review", "confirm", "cos.x.y", "--by", "eve"]
    )
    assert unknown.exit_code == 1
    unknown_reject = runner.invoke(
        app, ["--config", str(CONFIG), "review", "reject", "cos.x.y", "--by", "eve"]
    )
    assert unknown_reject.exit_code == 1
    nothing = runner.invoke(
        app, ["--config", str(CONFIG), "review", "edit", "cos.ref.branch", "--by", "eve"]
    )
    assert nothing.exit_code == 2
    bad_json = runner.invoke(
        app,
        [
            "--config",
            str(CONFIG),
            "review",
            "edit",
            "cos.ref.branch",
            "--by",
            "eve",
            "--key-columns",
            "{",
        ],
    )
    assert bad_json.exit_code == 2
    not_list = runner.invoke(
        app,
        [
            "--config",
            str(CONFIG),
            "review",
            "edit",
            "cos.ref.branch",
            "--by",
            "eve",
            "--key-columns",
            "{}",
        ],
    )
    assert not_list.exit_code == 2
    unknown_edit = runner.invoke(
        app,
        [
            "--config",
            str(CONFIG),
            "review",
            "edit",
            "cos.x.y",
            "--by",
            "eve",
            "--common-usage",
            "u",
            "--keep-status",
        ],
    )
    assert unknown_edit.exit_code == 1
    monkeypatch.setenv("DN_LLM__PROVIDER", "none")
    no_llm = runner.invoke(app, ["--config", str(CONFIG), "cards"])
    assert no_llm.exit_code == 2


# -- phases 3-4: chat, audit, ui -------------------------------------------------------


def _agent_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _replay_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DN_END_USER_USER", "alice")
    monkeypatch.setenv("DN_AUDIT__PATH", str(tmp_path / "audit.jsonl"))
    assert runner.invoke(app, ["--config", str(CONFIG), "build"]).exit_code == 0


def test_chat_flow_with_sample_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _agent_env(monkeypatch, tmp_path)
    script = "\n".join(
        [
            "What is the total balance per customer segment today?",
            "show me a sample",
            "n",
            "show me a sample",
            "y",
            "",
            "Show me the risk of our customers",
            "exit",
        ]
    )
    result = runner.invoke(app, ["--config", str(CONFIG), "chat"], input=script + "\n")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "chat as alice" in out and "Validated read-only SQL" in out
    assert out.count("Allow reading at most 10 sample row(s)?") == 2
    assert "sample (" in out and "segment_code | total_balance" in out
    assert "Possible tables:" in out and "access:" in out
    assert (tmp_path / "audit.jsonl").exists()

    audit = runner.invoke(
        app, ["--config", str(CONFIG), "audit", "--user", "alice", "--limit", "10"]
    )
    assert audit.exit_code == 0 and "sample confirmed=True executed=True" in audit.output
    assert "sample confirmed=False" in audit.output and "'OPEN'" not in audit.output
    empty = runner.invoke(app, ["--config", str(CONFIG), "audit", "--user", "nobody"])
    assert empty.output.strip() == "no audit entries"


def test_chat_eof_and_missing_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _agent_env(monkeypatch, tmp_path)
    eof = runner.invoke(app, ["--config", str(CONFIG), "chat", "--user", "bob"], input="")
    assert eof.exit_code == 0 and "chat as bob" in eof.output
    monkeypatch.delenv("DN_END_USER_USER")
    missing = runner.invoke(app, ["--config", str(CONFIG), "chat"], input="exit\n")
    assert missing.exit_code == 2 and "DN_END_USER_USER" in missing.output


def test_ui_command_launches_streamlit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _replay_env(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    class Done:
        returncode = 0

    def fake_run(command: list[str], env: dict[str, str], check: bool) -> Done:
        captured["command"] = command
        captured["env"] = env
        return Done()

    monkeypatch.setattr("data_navigator.cli.subprocess.run", fake_run)
    result = runner.invoke(app, ["--config", str(CONFIG), "ui", "--port", "9999"])
    assert result.exit_code == 0, result.output
    command = captured["command"]
    assert isinstance(command, list) and command[1:4] == ["-m", "streamlit", "run"]
    assert command[4].endswith("streamlit_app.py") and "9999" in command
    env = captured["env"]
    assert isinstance(env, dict) and env["DN_CONFIG_PATH"] == str(CONFIG)
