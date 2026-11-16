from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_navigator.catalog.records import CardRecord, KeyColumn
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import Secrets, Settings
from data_navigator.llm.replay import ReplayLLM
from data_navigator.pipeline.build import CatalogBuilder
from data_navigator.pipeline.enrich import enrich_catalog
from data_navigator.search.documents import (
    TABLE_PREFIX,
    TEMPLATE_PREFIX,
    build_documents,
    fingerprint_from_doc_id,
    table_document,
    table_name_from_doc_id,
)
from data_navigator.search.factory import build_search, reindex
from data_navigator.search.hybrid import HybridSearch, reciprocal_rank_fusion
from data_navigator.search.postgres_index import PostgresTextIndex, PostgresVectorIndex
from data_navigator.search.sqlite_text_index import SqliteTextIndex, fts_query
from data_navigator.search.sqlite_vector_index import SqliteVectorIndex
from data_navigator.search.text_index import SearchHit, TextDocument
from data_navigator.sources.factory import build_sources

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> SqliteCatalogRepository:
    repository = SqliteCatalogRepository(f"sqlite:///{tmp_path}/catalog.db")
    repository.initialize()
    settings = Settings()
    sources = build_sources(settings, Secrets(_env_file=None))  # type: ignore[call-arg]
    CatalogBuilder(settings, repository, sources, now=lambda: NOW).build()
    return repository


DOCS = [
    TextDocument(
        "table:cos.retail_ods.customer", "customer", "customer master data segment branch"
    ),
    TextDocument(
        "table:cos.retail_ods.account", "account", "deposit and loan accounts balance customer"
    ),
    TextDocument("template:abc", "abc", "select customer balance from account"),
]


def test_fts_query_builds_safe_match_expression() -> None:
    assert (
        fts_query("Customer's balance, 2026!")
        == '"customer" OR "customer"* OR "balance" OR "balance"* OR "2026" OR "2026"*'
    )
    assert fts_query("a ; --") is None
    assert fts_query('"drop"; OR x') == '"drop" OR "drop"* OR "or" OR "or"*'


def test_sqlite_text_index_roundtrip(repo: SqliteCatalogRepository) -> None:
    index = SqliteTextIndex(repo.engine)
    index.index(DOCS)
    index.index(DOCS)  # re-indexing replaces instead of duplicating
    assert index.count() == 3
    hits = index.search("customer balance")
    assert hits[0].doc_id in {"table:cos.retail_ods.account", "template:abc"}
    assert {h.doc_id for h in hits} == {d.doc_id for d in DOCS}
    only_tables = index.search("customer", prefix=TABLE_PREFIX)
    assert all(h.doc_id.startswith(TABLE_PREFIX) for h in only_tables) and len(only_tables) == 2
    assert (
        index.search("customer", limit=1, prefix="table:cos.retail_ods.cust")
        == [SearchHit("table:cos.retail_ods.customer", only_tables[0].score)]
        or len(index.search("customer", limit=1, prefix="table:cos.retail_ods.cust")) == 1
    )
    assert index.search("", limit=5) == [] and index.search("customer", limit=0) == []
    index.remove(["template:abc"])
    assert index.count() == 2
    index.clear()
    assert index.count() == 0


def test_sqlite_vector_index_cosine_ranking(repo: SqliteCatalogRepository) -> None:
    index = SqliteVectorIndex(repo.engine)
    index.upsert("table:a", [1.0, 0.0, 0.0])
    index.upsert("table:b", [0.0, 1.0, 0.0])
    index.upsert("template:c", [0.9, 0.1, 0.0])
    index.upsert("table:a", [1.0, 0.0, 0.0])  # upsert is idempotent
    index.upsert("table:d", [1.0, 0.0])  # wrong dimension: ignored at search time
    assert index.count() == 4
    hits = index.search([1.0, 0.0, 0.0], limit=2)
    assert [h.doc_id for h in hits] == ["table:a", "template:c"]
    assert hits[0].score == pytest.approx(1.0)
    assert [h.doc_id for h in index.search([1.0, 0.0, 0.0], prefix="table:")] == [
        "table:a",
        "table:b",
    ]
    assert index.search([0.0, 0.0, 0.0]) == [] and index.search([1.0, 0.0, 0.0], limit=0) == []
    with pytest.raises(ValueError, match="non-empty"):
        index.upsert("x", [])
    index.remove(["table:a"])
    index.clear()
    assert index.count() == 0 and index.search([1.0, 0.0, 0.0]) == []


def test_reciprocal_rank_fusion_merges_rankings() -> None:
    text = [SearchHit("a", 10.0), SearchHit("b", 5.0), SearchHit("c", 1.0)]
    vector = [SearchHit("b", 0.9), SearchHit("d", 0.8)]
    fused = reciprocal_rank_fusion([text, vector], k=1)
    assert [h.doc_id for h in fused] == ["b", "a", "d", "c"]
    assert fused[0].score == pytest.approx(1 / 3 + 1 / 2)


def test_hybrid_search_falls_back_to_text_only(
    repo: SqliteCatalogRepository, caplog: pytest.LogCaptureFixture
) -> None:
    text_only = HybridSearch(SqliteTextIndex(repo.engine))
    with caplog.at_level("WARNING"):
        summary = text_only.index(DOCS)
        hits = text_only.search("customer")
    assert not summary.vector_enabled and summary.embedded == 0 and hits
    assert "full-text matching only" in caplog.text
    assert "text only" in summary.render()
    assert text_only.search("   ") == []


def test_hybrid_search_uses_embeddings_when_available(repo: SqliteCatalogRepository) -> None:
    llm = ReplayLLM()
    hybrid = HybridSearch(
        SqliteTextIndex(repo.engine), SqliteVectorIndex(repo.engine), llm.embed, rrf_k=1
    )
    summary = hybrid.index(DOCS)
    assert summary.vector_enabled and summary.embedded == 3 and summary.embedding_errors == 0
    hits = hybrid.search("deposit loan accounts balance", limit=2, prefix=TABLE_PREFIX)
    assert hits[0].doc_id == "table:cos.retail_ods.account"
    hybrid.remove(["table:cos.retail_ods.account"])
    assert all(h.doc_id != "table:cos.retail_ods.account" for h in hybrid.search("account"))
    hybrid.clear()
    assert hybrid.search("account") == []

    def broken(texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedding service down")

    degraded = HybridSearch(SqliteTextIndex(repo.engine), SqliteVectorIndex(repo.engine), broken)
    summary = degraded.index(DOCS)
    assert summary.embedding_errors == 3 and summary.embedded == 0
    assert degraded.search("customer")  # text hits still returned


def test_documents_cover_tables_cards_and_templates(repo: SqliteCatalogRepository) -> None:
    docs = build_documents(repo)
    tables = [d for d in docs if d.doc_id.startswith(TABLE_PREFIX)]
    templates = [d for d in docs if d.doc_id.startswith(TEMPLATE_PREFIX)]
    assert len(tables) == 12 and len(templates) == 23
    account = next(d for d in tables if d.title == "cos.retail_ods.account")
    assert "retail ods account" in account.body and "End-of-day balance" in account.body
    assert "'OPEN'" not in "".join(d.body for d in templates)
    assert table_name_from_doc_id(account.doc_id) == "cos.retail_ods.account"
    assert fingerprint_from_doc_id(templates[0].doc_id) == templates[0].title
    assert (
        table_name_from_doc_id("template:x") is None and fingerprint_from_doc_id("table:x") is None
    )

    table = repo.get_table("cos.retail_ods.account")
    assert table is not None
    card = CardRecord(
        table=table.full_name,
        purpose="Snapshot of accounts",
        key_columns=[KeyColumn("balance", "End of day balance")],
        example_questions=["Total balance?"],
    )
    body = table_document(table, card).body
    assert "Snapshot of accounts" in body and "Total balance?" in body
    rejected = CardRecord(table=table.full_name, purpose="Wrong", review_status="rejected")
    assert "Wrong" not in table_document(table, rejected).body


def test_build_search_and_reindex(repo: SqliteCatalogRepository) -> None:
    settings = Settings()
    search = build_search(settings, repo, None)
    assert not search.vector_enabled
    summary = reindex(repo, search)
    assert summary.documents == 35
    hits = search.search("card spend merchant", limit=3, prefix=TABLE_PREFIX)
    assert "table:cos.cards_ods.card_transaction" in {h.doc_id for h in hits}

    with_vectors = build_search(settings, repo, ReplayLLM())
    assert with_vectors.vector_enabled and reindex(repo, with_vectors).embedded == 35

    settings.storage.backend = "postgres"
    with pytest.raises(NotImplementedError):
        build_search(settings, repo, None)
    with pytest.raises(NotImplementedError):
        PostgresTextIndex("postgresql://x")
    with pytest.raises(NotImplementedError):
        PostgresVectorIndex("postgresql://x")


def test_enrich_catalog_without_llm_indexes_only(repo: SqliteCatalogRepository) -> None:
    summary = enrich_catalog(Settings(), repo, None)
    assert summary.cards is None and summary.index is not None
    assert "skipped (no LLM configured" in summary.render()
    assert (
        enrich_catalog(Settings(), repo, None, skip_cards=True, skip_index=True).render()
        == "  cards: skipped (--skip-cards)"
    )
