from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.evaluation import (
    Evaluator,
    GoldenCase,
    load_golden_set,
    main,
    recall_at_k,
)
from data_navigator.llm.replay import ReplayLLM
from tests.conftest import REPLAY_SCRIPT, fixture_settings, no_secrets

GOLDEN = Path("eval/golden_set.example.yaml")


def test_recall_at_k() -> None:
    assert recall_at_k(["a", "b"], ["B", "x", "a"]) == 1.0
    assert recall_at_k(["a", "b"], ["x"] * 5 + ["a"]) == 0.0
    assert recall_at_k([], ["a"]) is None


def test_golden_set_loads(tmp_path: Path) -> None:
    cases = load_golden_set(GOLDEN)
    assert [c.id for c in cases] == ["balance_by_segment", "ambiguous_risk", "debit_by_channel"]
    assert cases[0].expected_mode == "sql" and cases[1].expected_mode == "candidates"
    assert cases[1].expected_sql is None and cases[0].expected_sql is not None
    bad = tmp_path / "bad.yaml"
    bad.write_text("foo: bar\n")
    with pytest.raises(ValueError, match="expected a list"):
        load_golden_set(bad)


def test_evaluator_reports_metrics(enriched_repo: SqliteCatalogRepository) -> None:
    settings = fixture_settings()
    evaluator = Evaluator(
        settings, no_secrets(), enriched_repo, llm=ReplayLLM.from_yaml(REPLAY_SCRIPT)
    )
    report = evaluator.run(load_golden_set(GOLDEN))
    assert [r.actual_mode for r in report.results] == ["sql", "candidates", "sql"]
    assert report.mode_accuracy == 1.0 and report.recall_at_5 == 1.0
    assert report.explain_first_pass == 0.5  # the debit case needed one fix
    assert report.execution_correct == 1.0
    text = report.render()
    assert (
        "mode accuracy: 100%" in text
        and "| balance_by_segment | alice | sql | sql | 1.00 | ok | yes |" in text
    )
    payload = report.to_dict()
    assert (
        payload["summary"]["cases"] == 3
        and payload["results"][2]["first_validation"] == "semantic_error"
    )

    denied = evaluator.run_case(
        GoldenCase(
            id="x",
            question="What is the total balance per customer segment today?",
            user="nobody",
            expected_sql="SELECT 1",
            expected_tables=["cos.retail_ods.account"],
        )
    )
    assert denied.actual_mode == "candidates" and not denied.mode_correct
    assert denied.execution_correct is False and denied.execution_note == "no SQL produced"


def test_evaluator_handles_crashes_and_missing_llm(
    enriched_repo: SqliteCatalogRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fixture_settings()
    evaluator = Evaluator(
        settings, no_secrets(), enriched_repo, llm=ReplayLLM.from_yaml(REPLAY_SCRIPT)
    )

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr("data_navigator.evaluation.build_agent_session", explode)
    result = evaluator.run_case(
        GoldenCase(id="c", question="q", user="alice", expected_sql="SELECT 1")
    )
    assert result.actual_mode == "error" and result.error == "RuntimeError: boom"
    assert (
        "RuntimeError: boom"
        in evaluator.run([GoldenCase(id="c", question="q", user="alice")]).render()
    )

    settings.llm.provider = "none"
    with pytest.raises(RuntimeError, match="needs an LLM"):
        Evaluator(settings, no_secrets(), enriched_repo)


def test_main_writes_reports(
    tmp_path: Path, enriched_repo: SqliteCatalogRepository, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f"storage: {{url: 'sqlite:///{enriched_repo.engine.url.database}'}}",
                "starburst:",
                "  end_user:",
                "    source: fixture",
                "    fixture_access: {alice: ['cos.*.*'], bob: ['cos.retail_ods.*'], carol: ['cos.risk_dm.*']}",
                f"llm: {{provider: replay, replay_path: '{REPLAY_SCRIPT}'}}",
                "audit: {path: null}",
            ]
        )
    )
    out = tmp_path / "report.md"
    out_json = tmp_path / "report.json"
    code = main(
        [
            "--config",
            str(config),
            "--golden",
            str(GOLDEN),
            "--out",
            str(out),
            "--json",
            str(out_json),
        ]
    )
    assert code == 0
    assert "# Data Navigator evaluation" in out.read_text()
    assert json.loads(out_json.read_text())["summary"]["cases"] == 3
    assert "candidate recall@5" in capsys.readouterr().out
