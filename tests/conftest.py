"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from data_navigator.agent.factory import build_agent_session
from data_navigator.agent.session import AgentSession
from data_navigator.cards.generate import CardGenerator
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import Secrets, Settings
from data_navigator.llm.client import LLMClient
from data_navigator.llm.replay import ReplayLLM
from data_navigator.pipeline.build import CatalogBuilder
from data_navigator.search.factory import build_search, reindex
from data_navigator.sources.factory import build_sources
from data_navigator.sqlproc.statements import quiet_sqlglot

BUILD_TIME = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
FIXTURE_TEAMS = {
    "alice": "retail",
    "bob": "retail",
    "carol": "risk",
    "dave": "cards",
    "eve": "data_platform",
}
FIXTURE_ACCESS = {
    "alice": ["cos.retail_*.*", "cos.ref.*"],
    "bob": ["cos.retail_ods.*"],
    "carol": ["cos.risk_dm.*", "cos.retail_dm.*", "cos.ref.*"],
    "dave": ["cos.cards_*.*", "cos.retail_ods.account"],
    "eve": ["cos.*.*"],
}
REPLAY_SCRIPT = Path("eval/replay.example.yaml").resolve()


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure the developer's own ``DN_*`` variables never leak into tests."""
    for name in list(os.environ):
        if name.startswith("DN_"):
            monkeypatch.delenv(name, raising=False)
    quiet_sqlglot()


def fixture_settings() -> Settings:
    """Settings that run everything against the bundled fixtures and the replay LLM."""
    settings = Settings()
    settings.teams.mapping = dict(FIXTURE_TEAMS)
    settings.starburst.end_user.source = "fixture"
    settings.starburst.end_user.fixture_access = {k: list(v) for k, v in FIXTURE_ACCESS.items()}
    settings.llm.provider = "replay"
    settings.llm.replay_path = REPLAY_SCRIPT
    settings.audit.path = None
    return settings


def no_secrets(**values: Any) -> Secrets:
    return Secrets(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def enriched_catalog_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A catalog built once per test session: tables, patterns, replay cards and the index."""
    db = tmp_path_factory.mktemp("catalog") / "catalog.db"
    repo = SqliteCatalogRepository(f"sqlite:///{db}")
    repo.initialize()
    settings = fixture_settings()
    sources = build_sources(settings, no_secrets())
    CatalogBuilder(settings, repo, sources, now=lambda: BUILD_TIME).build()
    llm = ReplayLLM.from_yaml(REPLAY_SCRIPT)
    CardGenerator(llm, repo, settings.cards, now=lambda: BUILD_TIME).generate()
    reindex(repo, build_search(settings, repo, llm))
    repo.engine.dispose()
    return db


@pytest.fixture
def enriched_repo(enriched_catalog_db: Path, tmp_path: Path) -> Iterator[SqliteCatalogRepository]:
    """A private copy of the enriched catalog for one test."""
    db = tmp_path / "catalog.db"
    shutil.copy(enriched_catalog_db, db)
    repo = SqliteCatalogRepository(f"sqlite:///{db}")
    yield repo
    repo.engine.dispose()


def make_session(
    repo: SqliteCatalogRepository,
    llm: LLMClient,
    *,
    user: str = "alice",
    settings: Settings | None = None,
) -> AgentSession:
    settings = settings or fixture_settings()
    return build_agent_session(settings, no_secrets(), repo, user=user, llm=llm)
