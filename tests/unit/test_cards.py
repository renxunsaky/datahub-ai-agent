from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_navigator.cards.context import build_context_pack, top_tables
from data_navigator.cards.generate import CardGenerator, tables_needing_cards
from data_navigator.cards.schemas import CardOutputError, parse_card_output
from data_navigator.catalog.records import CARD_STATUS_CONFIRMED
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import CardsSettings, Secrets, Settings
from data_navigator.llm.client import LLMError
from data_navigator.llm.replay import ReplayEntry, ReplayLLM, response_from_dict
from data_navigator.pipeline.build import CatalogBuilder
from data_navigator.sources.factory import build_sources

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
ACCOUNT = "cos.retail_ods.account"


@pytest.fixture
def repo(tmp_path: Path) -> SqliteCatalogRepository:
    repository = SqliteCatalogRepository(f"sqlite:///{tmp_path}/catalog.db")
    repository.initialize()
    settings = Settings()
    settings.teams.mapping = {"alice": "retail", "bob": "retail", "carol": "risk"}
    sources = build_sources(settings, Secrets(_env_file=None))  # type: ignore[call-arg]
    CatalogBuilder(settings, repository, sources, now=lambda: NOW).build()
    return repository


def card_json(
    purpose: str = "Accounts snapshot", columns: list[str] | None = None
) -> dict[str, object]:
    return {
        "purpose": purpose,
        "key_columns": [
            {"name": c, "meaning": f"meaning of {c}", "confidence": 0.8, "evidence": "comment"}
            for c in (columns or ["customer_id"])
        ],
        "common_usage": "join to customer",
        "example_questions": ["How many accounts?", " ", "Total balance?"],
        "overall_confidence": 0.7,
        "evidence": ["table comment"],
    }


def replay(*responses: dict[str, object], match: str | None = None) -> ReplayLLM:
    return ReplayLLM(
        entries=[ReplayEntry(match=match, responses=[response_from_dict(r) for r in responses])]
    )


# -- schema ---------------------------------------------------------------------------


def test_parse_card_output_validates_and_cleans() -> None:
    payload = card_json(columns=["account_id", "balance"])
    card = parse_card_output(json.dumps(payload), known_columns=["ACCOUNT_ID", "balance"])
    assert card.example_questions == ["How many accounts?", "Total balance?"]
    draft = card.to_draft()
    assert [c.name for c in draft.key_columns] == ["account_id", "balance"]


def test_parse_card_output_accepts_code_fence_and_deduplicates() -> None:
    payload = card_json(columns=["balance", "BALANCE"])
    card = parse_card_output(f"```json\n{json.dumps(payload)}\n```", known_columns=["balance"])
    assert len(card.key_columns) == 1


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        ("no json here", "no JSON object"),
        ('{"a": }', "not valid JSON"),
        (json.dumps({"purpose": "x"}), "validation error"),
        (json.dumps(card_json(columns=["ghost"])), "unknown columns"),
    ],
)
def test_parse_card_output_rejects_bad_replies(text: str, message: str) -> None:
    with pytest.raises(CardOutputError, match=message):
        parse_card_output(text, known_columns=["account_id", "balance"])


# -- context pack ----------------------------------------------------------------------


def test_context_pack_contains_catalog_data_only_and_is_stable(
    repo: SqliteCatalogRepository,
) -> None:
    table = repo.get_table(ACCOUNT)
    assert table is not None
    settings = CardsSettings()
    pack = build_context_pack(
        table, templates=repo.list_templates(), join_edges=repo.list_join_edges(), settings=settings
    )
    rendered = pack.render()
    assert rendered.startswith("<table>\nname: cos.retail_ods.account")
    assert "<columns>" in rendered and "<join_edges>" in rendered
    assert "<representative_queries>" in rendered and "<populating_statements>" not in rendered
    assert "'OPEN'" not in rendered and "DATE '2026" not in rendered  # constants normalised
    written = repo.get_table("cos.retail_dm.customer_daily_balance")
    assert written is not None
    written_pack = build_context_pack(
        written,
        templates=repo.list_templates(),
        join_edges=repo.list_join_edges(),
        settings=settings,
    )
    assert "<populating_statements>" in written_pack.render()
    again = build_context_pack(
        table, templates=repo.list_templates(), join_edges=repo.list_join_edges(), settings=settings
    )
    assert again.hash == pack.hash and not pack.truncated


def test_context_pack_respects_size_budget(repo: SqliteCatalogRepository) -> None:
    table = repo.get_table(ACCOUNT)
    assert table is not None
    pack = build_context_pack(
        table,
        templates=repo.list_templates(),
        join_edges=repo.list_join_edges(),
        settings=CardsSettings(max_context_chars=1000, max_queries_per_table=5),
    )
    assert (
        pack.truncated and len(pack.render()) <= 1000 + 200
    )  # table/columns sections are never cut


def test_top_tables_orders_by_usage(repo: SqliteCatalogRepository) -> None:
    top = top_tables(repo.list_tables(), 3)
    assert top[0].full_name == ACCOUNT and len(top) == 3


# -- generator ----------------------------------------------------------------------


def test_generator_writes_cards_and_skips_unchanged(repo: SqliteCatalogRepository) -> None:
    llm = replay({"json": card_json()})
    generator = CardGenerator(llm, repo, CardsSettings(top_n_tables=2), now=lambda: NOW)
    summary = generator.generate()
    assert summary.considered == 2 and len(summary.generated) == 2 and not summary.failed
    card = repo.get_card(ACCOUNT)
    assert card is not None and card.purpose == "Accounts snapshot"
    assert card.input_hash and card.model == "replay" and card.generated_at == NOW
    table = repo.get_table(ACCOUNT)
    assert table is not None
    descriptions = {c.name: c.ai_description for c in table.columns}
    assert descriptions["customer_id"] == "meaning of customer_id"

    second = generator.generate()
    assert second.unchanged and not second.generated
    assert len(llm.calls) == 2  # no new model calls

    forced = generator.generate(force=True, tables=[ACCOUNT])
    assert forced.generated == [ACCOUNT]
    dry = generator.generate(force=True, dry_run=True)
    assert len(dry.generated) == 2 and len(llm.calls) == 3


def test_generator_retries_invalid_output_then_fails(repo: SqliteCatalogRepository) -> None:
    llm = replay(
        {"content": "garbage"}, {"json": card_json(columns=["ghost"])}, {"json": card_json("Fixed")}
    )
    generator = CardGenerator(llm, repo, CardsSettings(max_generation_attempts=3), now=lambda: NOW)
    summary = generator.generate(tables=[ACCOUNT])
    assert summary.generated == [ACCOUNT]
    card = repo.get_card(ACCOUNT)
    assert card is not None and card.purpose == "Fixed"
    assert len(llm.calls) == 3 and "invalid" in llm.calls[-1][-1]["content"]

    strict = CardGenerator(
        replay({"content": "garbage"}), repo, CardsSettings(max_generation_attempts=2)
    )
    failed = strict.generate(tables=[ACCOUNT], force=True)
    assert ACCOUNT in failed.failed and "CardOutputError" in failed.failed[ACCOUNT]


def test_generator_records_llm_errors(repo: SqliteCatalogRepository) -> None:
    llm = ReplayLLM(entries=[])  # every call raises LLMError
    summary = CardGenerator(llm, repo, CardsSettings()).generate(tables=[ACCOUNT])
    assert "LLMError" in summary.failed[ACCOUNT]
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "x"}])


def test_confirmed_fields_survive_regeneration(repo: SqliteCatalogRepository) -> None:
    generator = CardGenerator(
        replay({"json": card_json("v1")}), repo, CardsSettings(), now=lambda: NOW
    )
    generator.generate(tables=[ACCOUNT])
    repo.review_card(
        ACCOUNT,
        status="ai_inferred",
        reviewed_by="eve",
        reviewed_at=NOW,
        edits={"purpose": "Human purpose"},
    )
    CardGenerator(
        replay({"json": card_json("v2")}), repo, CardsSettings(), now=lambda: NOW
    ).generate(tables=[ACCOUNT], force=True)
    card = repo.get_card(ACCOUNT)
    assert card is not None
    assert card.purpose == "Human purpose" and card.confirmed_fields == ["purpose"]
    assert card.common_usage == "join to customer"  # non-confirmed fields are refreshed

    repo.review_card(ACCOUNT, status=CARD_STATUS_CONFIRMED, reviewed_by="eve", reviewed_at=NOW)
    summary = CardGenerator(replay({"json": card_json("v3")}), repo, CardsSettings()).generate(
        tables=[ACCOUNT], force=True
    )
    assert summary.skipped_reviewed == [ACCOUNT]
    card = repo.get_card(ACCOUNT)
    assert card is not None and card.purpose == "Human purpose" and card.is_confirmed


def test_tables_needing_cards(repo: SqliteCatalogRepository) -> None:
    CardGenerator(replay({"json": card_json()}), repo, CardsSettings()).generate(tables=[ACCOUNT])
    assert tables_needing_cards(repo, [ACCOUNT, "cos.ref.branch"]) == ["cos.ref.branch"]
