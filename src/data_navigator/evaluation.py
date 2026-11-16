"""Evaluation harness for the agent.

Metrics (per the task description):

* **mode accuracy** - did the agent pick the expected mode (``sql`` / ``candidates``)?
* **candidate recall@5** - share of ``expected_tables`` found among the first five
  tables the agent named (candidates, or the tables of its SQL answer);
* **EXPLAIN first-pass rate** - share of cases whose first ``validate_sql`` call
  succeeded;
* **SQL execution correctness** - for cases with ``expected_sql``: both statements
  are run with the case's user (row-capped) and the result sets compared.  With
  the fixture end-user connection the rows are synthetic, so this metric is only
  meaningful against the real platform.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from data_navigator.agent.factory import build_agent_session
from data_navigator.catalog.factory import open_repository
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.config import Secrets, Settings, load_secrets, load_settings
from data_navigator.llm.client import LLMClient
from data_navigator.llm.factory import build_llm
from data_navigator.sqlproc.statements import quiet_sqlglot
from data_navigator.starburst.end_user_client import EndUserClient, build_end_user_client

log = logging.getLogger(__name__)

EXECUTION_ROWS = 100


@dataclass
class GoldenCase:
    id: str
    question: str
    user: str
    expected_sql: str | None = None
    expected_mode: str = "sql"
    expected_tables: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> GoldenCase:
        expected_sql = data.get("expected_sql")
        mode = data.get("expected_mode") or ("sql" if expected_sql else "candidates")
        return cls(
            id=str(data.get("id") or f"case_{index + 1}"),
            question=str(data["question"]),
            user=str(data["user"]),
            expected_sql=str(expected_sql).strip() if expected_sql else None,
            expected_mode=str(mode),
            expected_tables=[str(t).lower() for t in data.get("expected_tables", [])],
        )


@dataclass
class CaseResult:
    id: str
    user: str
    question: str
    expected_mode: str
    actual_mode: str
    mode_correct: bool
    recall_at_5: float | None
    first_validation: str | None
    explain_first_pass: bool | None
    execution_correct: bool | None
    execution_note: str | None
    tables: list[str]
    sql: str | None
    warnings: list[str]
    error: str | None = None


@dataclass
class EvalReport:
    results: list[CaseResult]

    @property
    def mode_accuracy(self) -> float | None:
        return _ratio([r.mode_correct for r in self.results])

    @property
    def recall_at_5(self) -> float | None:
        values = [r.recall_at_5 for r in self.results if r.recall_at_5 is not None]
        return sum(values) / len(values) if values else None

    @property
    def explain_first_pass(self) -> float | None:
        return _ratio(
            [r.explain_first_pass for r in self.results if r.explain_first_pass is not None]
        )

    @property
    def execution_correct(self) -> float | None:
        return _ratio(
            [r.execution_correct for r in self.results if r.execution_correct is not None]
        )

    def render(self) -> str:
        lines = ["# Data Navigator evaluation", ""]
        lines.append(f"- cases: {len(self.results)}")
        lines.append(f"- mode accuracy: {_pct(self.mode_accuracy)}")
        lines.append(f"- candidate recall@5: {_pct(self.recall_at_5)}")
        lines.append(f"- EXPLAIN first-pass rate: {_pct(self.explain_first_pass)}")
        lines.append(f"- SQL execution correctness: {_pct(self.execution_correct)}")
        lines += [
            "",
            "| case | user | expected | actual | recall@5 | first EXPLAIN | exec | note |",
        ]
        lines.append("| --- | --- | --- | --- | ---: | --- | --- | --- |")
        for r in self.results:
            recall = "-" if r.recall_at_5 is None else f"{r.recall_at_5:.2f}"
            execution = (
                "-" if r.execution_correct is None else ("yes" if r.execution_correct else "no")
            )
            note = r.error or r.execution_note or "; ".join(r.warnings) or ""
            lines.append(
                f"| {r.id} | {r.user} | {r.expected_mode} | {r.actual_mode} | {recall} | "
                f"{r.first_validation or '-'} | {execution} | {note} |"
            )
        lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "cases": len(self.results),
                "mode_accuracy": self.mode_accuracy,
                "recall_at_5": self.recall_at_5,
                "explain_first_pass": self.explain_first_pass,
                "execution_correct": self.execution_correct,
            },
            "results": [asdict(r) for r in self.results],
        }


def load_golden_set(path: Path) -> list[GoldenCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = raw.get("cases", raw) if isinstance(raw, dict) else raw
    if not isinstance(cases, list):
        msg = f"{path}: expected a list of cases"
        raise ValueError(msg)
    return [GoldenCase.from_dict(dict(item), i) for i, item in enumerate(cases)]


def recall_at_k(expected: Sequence[str], actual: Sequence[str], k: int = 5) -> float | None:
    if not expected:
        return None
    top = {name.lower() for name in actual[:k]}
    return sum(1 for name in expected if name.lower() in top) / len(expected)


class Evaluator:
    def __init__(
        self,
        settings: Settings,
        secrets: Secrets,
        repository: CatalogRepository,
        *,
        llm: LLMClient | None = None,
        clients: dict[str, EndUserClient] | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._repo = repository
        self._llm = llm or build_llm(settings, secrets)
        if self._llm is None:
            msg = "the evaluation needs an LLM (llm.provider: replay or openai_compatible)"
            raise RuntimeError(msg)
        self._clients: dict[str, EndUserClient] = dict(clients or {})

    def run(self, cases: Sequence[GoldenCase]) -> EvalReport:
        return EvalReport([self.run_case(case) for case in cases])

    def run_case(self, case: GoldenCase) -> CaseResult:
        client = self._client_for(case.user)
        try:
            session = build_agent_session(
                self._settings,
                self._secrets,
                self._repo,
                user=case.user,
                llm=self._llm,
                client=client,
            )
            reply = session.ask(case.question)
            if reply.needs_confirmation:  # the model should not sample during evaluation
                reply = session.resume(False)
            state = session.state()
        except Exception as error:
            log.warning("case %s failed: %s", case.id, type(error).__name__)
            return CaseResult(
                id=case.id,
                user=case.user,
                question=case.question,
                expected_mode=case.expected_mode,
                actual_mode="error",
                mode_correct=False,
                recall_at_5=0.0 if case.expected_tables else None,
                first_validation=None,
                explain_first_pass=None,
                execution_correct=False if case.expected_sql else None,
                execution_note=None,
                tables=[],
                sql=None,
                warnings=[],
                error=f"{type(error).__name__}: {error}",
            )
        tables = (
            [c["table"] for c in reply.candidates]
            if reply.mode == "candidates"
            else list(reply.tables)
        )
        first_validation = state.get("first_validation")
        execution_correct: bool | None = None
        execution_note: str | None = None
        if case.expected_sql:
            if reply.mode == "sql" and reply.sql:
                execution_correct, execution_note = self._compare(
                    client, case.expected_sql, reply.sql
                )
            else:
                execution_correct, execution_note = False, "no SQL produced"
        return CaseResult(
            id=case.id,
            user=case.user,
            question=case.question,
            expected_mode=case.expected_mode,
            actual_mode=reply.mode,
            mode_correct=reply.mode == case.expected_mode,
            recall_at_5=recall_at_k(case.expected_tables, tables),
            first_validation=first_validation,
            explain_first_pass=None if first_validation is None else first_validation == "ok",
            execution_correct=execution_correct,
            execution_note=execution_note,
            tables=tables,
            sql=reply.sql,
            warnings=list(reply.warnings),
        )

    def _client_for(self, user: str) -> EndUserClient:
        client = self._clients.get(user)
        if client is None:
            client = build_end_user_client(
                self._settings, self._secrets, user=user, schema_map=self._repo.schema_map()
            )
            self._clients[user] = client
        return client

    @staticmethod
    def _compare(
        client: EndUserClient, expected_sql: str, actual_sql: str
    ) -> tuple[bool, str | None]:
        expected = client.sample(expected_sql, max_rows=EXECUTION_ROWS)
        actual = client.sample(actual_sql, max_rows=EXECUTION_ROWS)
        if not expected.ok:
            return False, f"expected SQL failed: {expected.status}"
        if not actual.ok:
            return False, f"generated SQL failed: {actual.status}"
        same = sorted(map(str, expected.rows)) == sorted(map(str, actual.rows)) and [
            c.lower() for c in expected.columns
        ] == [c.lower() for c in actual.columns]
        return same, None if same else "result sets differ"


def _ratio(values: Sequence[bool]) -> float | None:
    return sum(1 for v in values if v) / len(values) if values else None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the Data Navigator agent.")
    parser.add_argument("--config", type=Path, default=None, help="YAML configuration file")
    parser.add_argument("--env-file", type=Path, default=None, help="secrets file (.env)")
    parser.add_argument(
        "--golden", type=Path, default=Path("eval/golden_set.example.yaml"), help="golden set YAML"
    )
    parser.add_argument("--out", type=Path, default=None, help="write the Markdown report here")
    parser.add_argument("--json", type=Path, default=None, help="write the JSON results here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    quiet_sqlglot()
    config = args.config
    if config is None and Path("config.yaml").exists():
        config = Path("config.yaml")
    settings = load_settings(config)
    env_file: Path | str | None = args.env_file
    if env_file is None:
        env_file = ".env" if Path(".env").exists() else None
    secrets = load_secrets(env_file)
    repository = open_repository(settings, dry_run=True)
    report = Evaluator(settings, secrets, repository).run(load_golden_set(args.golden))
    text = report.render()
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    if args.json is not None:
        args.json.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(text)
    return 0
