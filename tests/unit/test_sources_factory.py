from __future__ import annotations

import pytest

from data_navigator.config import Secrets, Settings
from data_navigator.sources.factory import build_sources
from data_navigator.sources.metadata import FixtureMetadataSource, StarburstMetadataSource
from data_navigator.sources.query_history import (
    JsonlQueryHistorySource,
    StarburstQueryHistorySource,
)
from data_navigator.sources.sql_files import GitSqlFileSource, LocalDirectorySqlFileSource
from data_navigator.sources.teams import ConfigTeamResolver


def secrets() -> Secrets:
    return Secrets(_env_file=None)  # type: ignore[call-arg]


def test_defaults_build_fixture_sources() -> None:
    sources = build_sources(Settings(), secrets())
    assert isinstance(sources.metadata, FixtureMetadataSource)
    assert isinstance(sources.query_history, JsonlQueryHistorySource)
    assert isinstance(sources.sql_files, LocalDirectorySqlFileSource)
    assert isinstance(sources.teams, ConfigTeamResolver)
    assert sources.describe()["metadata"] == "FixtureMetadataSource"


def test_real_sources_are_selected_by_configuration() -> None:
    settings = Settings()
    settings.metadata.source = "starburst"
    settings.query_history.source = "starburst"
    settings.query_history.table = "hist.logs.query_history"
    settings.sql_files.source = "git"
    settings.sql_files.repo_url = "https://gitlab.example.internal/data/airflow.git"
    sources = build_sources(settings, secrets())
    assert isinstance(sources.metadata, StarburstMetadataSource)
    assert isinstance(sources.query_history, StarburstQueryHistorySource)
    assert isinstance(sources.sql_files, GitSqlFileSource)


def test_git_source_requires_repo_url() -> None:
    settings = Settings()
    settings.sql_files.source = "git"
    with pytest.raises(ValueError, match="repo_url"):
        build_sources(settings, secrets())


def test_team_resolver() -> None:
    resolver = ConfigTeamResolver({"Alice": "retail"}, default_team="unknown")
    assert resolver.team_for_user("alice") == "retail"
    assert resolver.team_for_user("bob") == "unknown"
    assert resolver.team_for_user(None) is None
    assert ConfigTeamResolver({}).team_for_user("bob") is None
