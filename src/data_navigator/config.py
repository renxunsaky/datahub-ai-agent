"""Application configuration.

Configuration is layered, lowest priority first:

1. Defaults declared on the models below.
2. A YAML file (``--config``), loaded by :func:`load_settings`.
3. Environment variables prefixed with ``DN_`` and nested with ``__``
   (for example ``DN_STORAGE__URL=sqlite:///./catalog.db``).

Secrets are deliberately *not* part of :class:`Settings`.  They live in
:class:`Secrets`, which only reads environment variables and a local ``.env``
file, so a YAML file can never carry a password or token (``extra="forbid"``
rejects unknown keys such as ``password``).

Values marked ``TODO(confirm)`` are placeholders for environment details that
still need to be confirmed with the platform team; see ``docs/open-questions.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class StorageSettings(BaseModel):
    """Where the catalog database lives.

    ``sqlite`` is the MVP backend. ``postgres`` is reserved for a later phase and
    currently backed by a stub repository (see ``catalog/postgres_repo.py``).
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["sqlite", "postgres"] = "sqlite"
    url: str = "sqlite:///./catalog.db"


# ---------------------------------------------------------------------------
# Starburst
# ---------------------------------------------------------------------------


class StarburstConnectionSettings(BaseModel):
    """Connection parameters shared by both Starburst clients.

    Credentials are *never* configured here; see :class:`Secrets`.
    """

    model_config = ConfigDict(extra="forbid")

    # TODO(confirm): Starburst coordinator host, port and HTTP scheme.
    host: str = "starburst.example.internal"
    port: int = 443
    http_scheme: Literal["http", "https"] = "https"
    # TODO(confirm): authentication method for the builder user and for end users.
    auth: Literal["basic", "jwt", "none"] = "basic"
    # ``True``/``False`` or a path to a CA bundle, passed to the trino client as ``verify``.
    verify_ssl: bool | str = True
    request_timeout_s: float = Field(default=60.0, gt=0)
    # Identifies this application in Starburst query logs (``source`` header).
    source_tag: str = "data-navigator"


class BuilderSettings(BaseModel):
    """The dedicated catalog-building user. Used *only* while building the catalog."""

    model_config = ConfigDict(extra="forbid")

    # TODO(confirm): name of the dedicated Starburst user; its password comes from
    # the ``DN_BUILDER_PASSWORD`` environment variable.
    user: str = "data_navigator_builder"


class StarburstSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: StarburstConnectionSettings = Field(default_factory=StarburstConnectionSettings)
    builder: BuilderSettings = Field(default_factory=BuilderSettings)


# ---------------------------------------------------------------------------
# Sample queries (end-user connection)
# ---------------------------------------------------------------------------


class SampleSettings(BaseModel):
    """Limits applied to every sample query executed on behalf of an end user."""

    model_config = ConfigDict(extra="forbid")

    # Hard cap on rows returned by a sample. Enforced in code, never by the LLM.
    max_rows: int = Field(default=10, ge=1, le=1000)
    # Whether sample rows may be shown to the LLM. Stays ``False`` until the security
    # team confirms LLMaaS may process data of that classification.
    send_to_llm: bool = False
    # TODO(confirm): sensible execution-time and scan limits for sample queries.
    query_max_execution_time: str = "30s"
    # Additional Trino session properties applied to sample queries, e.g.
    # ``query_max_scan_physical_bytes``.  Keys and values are passed through verbatim.
    session_properties: dict[str, str] = Field(
        default_factory=lambda: {"query_max_scan_physical_bytes": "1GB"}
    )


# ---------------------------------------------------------------------------
# Catalog sources
# ---------------------------------------------------------------------------


class MetadataSettings(BaseModel):
    """How table and column metadata is read."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["starburst", "fixture"] = "fixture"
    # TODO(confirm): catalogs visible to the builder user that should be indexed.
    catalogs: list[str] = Field(default_factory=lambda: ["cos"])
    excluded_schemas: list[str] = Field(
        default_factory=lambda: ["information_schema", "sys", "pg_catalog"]
    )
    # ``auto`` tries ``information_schema.columns.comment`` first and falls back to
    # ``SHOW COLUMNS`` per table when that column does not exist.
    column_comments: Literal["auto", "information_schema", "show_columns"] = "auto"
    fixture_dir: Path = Path("tests/fixtures/information_schema")


class QueryHistoryColumns(BaseModel):
    """Column names of the query-history table. All of them are TODO(confirm)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    query_id: str = "query_id"
    query_text: str = "query"
    user: str = "user"
    state: str = "state"
    started_at: str = "created"
    ended_at: str | None = "end"
    catalog: str | None = "catalog"
    schema_: str | None = Field(default="schema", alias="schema")


class QueryHistorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["starburst", "jsonl"] = "jsonl"
    # TODO(confirm): fully-qualified name of the query-history table in Starburst
    # (for example ``system.runtime.queries`` keeps only recent in-memory history).
    table: str | None = None
    columns: QueryHistoryColumns = Field(default_factory=QueryHistoryColumns)
    # Only queries in one of these states are processed.
    success_states: list[str] = Field(default_factory=lambda: ["FINISHED"])
    # Initial time window for a full build; refresh continues from the last run.
    window_days: int = Field(default=90, ge=1)
    # TODO(confirm): retention period of the history table (bounds ``window_days``).
    retention_days: int | None = None
    # Fixture / test adapter.
    jsonl_path: Path = Path("tests/fixtures/query_history/queries.jsonl")
    # Statement types to skip entirely (never parsed, never stored).
    ignored_prefixes: list[str] = Field(
        default_factory=lambda: ["EXPLAIN", "SHOW", "DESCRIBE", "DESC", "SET ", "USE ", "RESET"]
    )


class SqlFilesSettings(BaseModel):
    """Where the Airflow repository's ``.sql`` files come from."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["git", "local"] = "local"
    # TODO(confirm): GitLab HTTPS clone URL. The token comes from ``DN_GITLAB_TOKEN``
    # and is passed through a git credential helper, never written into the URL.
    repo_url: str | None = None
    branch: str = "main"
    clone_dir: Path = Path(".cache/airflow-repo")
    # Username presented together with the token (GitLab accepts ``oauth2`` for PATs).
    git_username: str = "oauth2"
    include_dirs: list[str] = Field(default_factory=lambda: ["src", "automation"])
    extensions: list[str] = Field(default_factory=lambda: [".sql"])
    # Fixture / test adapter: a plain directory with the same layout.
    local_path: Path = Path("tests/fixtures/repo")
    # TODO(confirm): catalog and schema assumed for unqualified table names in files.
    default_catalog: str | None = None
    default_schema: str | None = None


class TemplateSettings(BaseModel):
    """Template syntax found in ``.sql`` files. TODO(confirm) which forms are used."""

    model_config = ConfigDict(extra="forbid")

    jinja: bool = True
    # Extra placeholder patterns (regular expressions) replaced before parsing.
    extra_patterns: list[str] = Field(
        default_factory=lambda: [r"\$\{[^}]*\}", r"%\([A-Za-z_][A-Za-z0-9_]*\)s"]
    )
    # Identifier substituted for every template expression.
    placeholder: str = "dn_tpl_var"

    @field_validator("placeholder")
    @classmethod
    def _placeholder_is_identifier(cls, value: str) -> str:
        if not value.isidentifier():
            msg = "template.placeholder must be a plain identifier"
            raise ValueError(msg)
        return value


class OwnershipRule(BaseModel):
    """Infers the owning team of a table from its name.

    ``pattern`` is a regular expression matched against ``applies_to`` (the schema
    name, the full ``catalog.schema.table`` name, or the storage bucket when known).
    ``team`` may reference named groups of the pattern with ``\\g<name>``; when it is
    omitted the group named ``team`` is used.
    """

    model_config = ConfigDict(extra="forbid")

    pattern: str
    applies_to: Literal["schema", "full_name", "bucket"] = "schema"
    team: str | None = None


class AnalysisSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Weights for join-edge scoring, by evidence source.
    weight_repo_file: float = Field(default=3.0, ge=0)
    weight_repeated_query: float = Field(default=2.0, ge=0)
    weight_single_query: float = Field(default=1.0, ge=0)
    # A history pattern executed at least this many times counts as "repeated".
    repeated_query_threshold: int = Field(default=2, ge=1)
    # Minimum occurrences before a column is flagged as a join key / filter column.
    join_key_min_count: int = Field(default=2, ge=1)
    filter_min_count: int = Field(default=2, ge=1)
    # TODO(confirm): how owning teams are determined (schema prefix, bucket, registry).
    ownership_rules: list[OwnershipRule] = Field(
        default_factory=lambda: [
            OwnershipRule(pattern=r"^(?P<team>[a-z0-9]+)_", applies_to="schema"),
        ]
    )


class TeamsSettings(BaseModel):
    """User-to-team mapping. TODO(confirm): the authoritative source for this mapping."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["config"] = "config"
    mapping: dict[str, str] = Field(default_factory=dict)
    default_team: str | None = None


# ---------------------------------------------------------------------------
# LLM, agent, cards, audit
# ---------------------------------------------------------------------------


class LLMSettings(BaseModel):
    """Internal LLM-as-a-Service endpoint (OpenAI-compatible unless confirmed otherwise)."""

    model_config = ConfigDict(extra="forbid")

    # Deliberately no default: the application must never fall back to a public endpoint.
    base_url: str | None = None
    # TODO(confirm): model names offered by LLMaaS, and whether embeddings are available.
    model: str | None = None
    embedding_model: str | None = None
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    max_concurrency: int = Field(default=4, ge=1)
    # TODO(confirm): LLMaaS rate limits.
    requests_per_minute: int = Field(default=60, ge=1)


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_steps: int = Field(default=10, ge=1)
    max_sql_retries: int = Field(default=3, ge=0)
    search_limit: int = Field(default=8, ge=1)


class CardsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_n_tables: int = Field(default=200, ge=1)
    max_queries_per_table: int = Field(default=5, ge=0)
    max_context_chars: int = Field(default=12_000, ge=1000)
    max_generation_attempts: int = Field(default=3, ge=1)


class AuditSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path = Path("audit.jsonl")


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Root configuration. See the module docstring for the precedence rules."""

    model_config = SettingsConfigDict(
        env_prefix="DN_",
        env_nested_delimiter="__",
        extra="forbid",
        validate_default=True,
    )

    storage: StorageSettings = Field(default_factory=StorageSettings)
    starburst: StarburstSettings = Field(default_factory=StarburstSettings)
    samples: SampleSettings = Field(default_factory=SampleSettings)
    metadata: MetadataSettings = Field(default_factory=MetadataSettings)
    query_history: QueryHistorySettings = Field(default_factory=QueryHistorySettings)
    sql_files: SqlFilesSettings = Field(default_factory=SqlFilesSettings)
    template: TemplateSettings = Field(default_factory=TemplateSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    teams: TeamsSettings = Field(default_factory=TeamsSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    cards: CardsSettings = Field(default_factory=CardsSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # ``init_settings`` carries the YAML content (see ``load_settings``); environment
        # variables must override it, hence the order.
        return (env_settings, init_settings)


class Secrets(BaseSettings):
    """Credentials. Read from environment variables and a local ``.env`` file only.

    Never log or persist these values; :class:`pydantic.SecretStr` masks them in
    ``repr`` and ``str``.
    """

    model_config = SettingsConfigDict(
        env_prefix="DN_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    builder_password: SecretStr | None = None
    end_user_user: str | None = None
    end_user_password: SecretStr | None = None
    end_user_jwt: SecretStr | None = None
    gitlab_token: SecretStr | None = None
    llm_api_key: SecretStr | None = None


def load_settings(config_path: Path | None = None) -> Settings:
    """Build :class:`Settings` from an optional YAML file plus environment overrides.

    A missing *config_path* is an error; passing ``None`` uses defaults and the
    environment only (which is enough to run against the bundled fixtures).
    """
    data: dict[str, Any] = {}
    if config_path is not None:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            msg = f"{config_path}: top level of the YAML file must be a mapping"
            raise ValueError(msg)
        data = raw
    return Settings(**data)


def load_secrets(env_file: Path | str | None = ".env") -> Secrets:
    """Load :class:`Secrets`; *env_file* may be ``None`` to read the environment only."""
    return Secrets(_env_file=env_file)  # type: ignore[call-arg]
