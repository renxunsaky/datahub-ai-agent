"""Build the configured sources (and the builder client when needed)."""

from __future__ import annotations

from dataclasses import dataclass

from data_navigator.config import Secrets, Settings
from data_navigator.sources.metadata import (
    FixtureMetadataSource,
    MetadataSource,
    StarburstMetadataSource,
)
from data_navigator.sources.query_history import (
    JsonlQueryHistorySource,
    QueryHistorySource,
    StarburstQueryHistorySource,
)
from data_navigator.sources.sql_files import (
    GitSqlFileSource,
    LocalDirectorySqlFileSource,
    SqlFileSource,
)
from data_navigator.sources.teams import ConfigTeamResolver, TeamResolver
from data_navigator.starburst.builder_client import BuilderClient, build_builder_client


@dataclass
class Sources:
    metadata: MetadataSource
    query_history: QueryHistorySource
    sql_files: SqlFileSource
    teams: TeamResolver

    def describe(self) -> dict[str, str]:
        return {
            "metadata": type(self.metadata).__name__,
            "query_history": type(self.query_history).__name__,
            "sql_files": type(self.sql_files).__name__,
            "teams": type(self.teams).__name__,
        }


def build_sources(settings: Settings, secrets: Secrets) -> Sources:
    builder: BuilderClient | None = None

    def client() -> BuilderClient:
        nonlocal builder
        if builder is None:
            builder = build_builder_client(settings, secrets)
        return builder

    metadata: MetadataSource
    if settings.metadata.source == "starburst":
        metadata = StarburstMetadataSource(client(), settings.metadata)
    else:
        metadata = FixtureMetadataSource(
            settings.metadata.fixture_dir,
            catalogs=settings.metadata.catalogs,
            excluded_schemas=settings.metadata.excluded_schemas,
        )

    history: QueryHistorySource
    if settings.query_history.source == "starburst":
        history = StarburstQueryHistorySource(client(), settings.query_history)
    else:
        history = JsonlQueryHistorySource(settings.query_history.jsonl_path, settings.query_history)

    files: SqlFileSource
    if settings.sql_files.source == "git":
        if not settings.sql_files.repo_url:
            msg = "sql_files.repo_url must be configured for the git source"
            raise ValueError(msg)
        files = GitSqlFileSource(
            settings.sql_files.repo_url,
            settings.sql_files.clone_dir,
            branch=settings.sql_files.branch,
            include_dirs=settings.sql_files.include_dirs,
            extensions=settings.sql_files.extensions,
            token=secrets.gitlab_token,
            username=settings.sql_files.git_username,
        )
    else:
        files = LocalDirectorySqlFileSource(
            settings.sql_files.local_path,
            include_dirs=settings.sql_files.include_dirs,
            extensions=settings.sql_files.extensions,
        )

    teams = ConfigTeamResolver(settings.teams.mapping, default_team=settings.teams.default_team)
    return Sources(metadata=metadata, query_history=history, sql_files=files, teams=teams)
