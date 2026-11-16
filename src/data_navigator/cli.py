"""Command-line interface.

Commands available in phases 0-1: ``build``, ``refresh``, ``report``, ``show-config``.
``chat`` and ``review`` arrive with phases 2-3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from data_navigator.catalog.factory import create_repository
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.config import Secrets, Settings, load_secrets, load_settings
from data_navigator.pipeline.build import BuildSummary, CatalogBuilder
from data_navigator.pipeline.report import render_report
from data_navigator.sources.factory import build_sources
from data_navigator.sqlproc.statements import quiet_sqlglot

app = typer.Typer(
    help="Data Navigator: build a lightweight data catalog and generate read-only SQL.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_CONFIG = Path("config.yaml")


@dataclass
class AppContext:
    settings: Settings
    secrets: Secrets


def _context(ctx: typer.Context) -> AppContext:
    obj = ctx.obj
    if not isinstance(obj, AppContext):  # pragma: no cover - typer always runs the callback
        msg = "application context missing"
        raise RuntimeError(msg)
    return obj


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="YAML configuration file (defaults to ./config.yaml when it exists).",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", help="Secrets file (defaults to ./.env when it exists)."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at INFO level.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    quiet_sqlglot()
    if config is None and DEFAULT_CONFIG.exists():
        config = DEFAULT_CONFIG
    settings = load_settings(config)
    secrets_file: Path | str | None = env_file
    if secrets_file is None:
        secrets_file = ".env" if Path(".env").exists() else None
    ctx.obj = AppContext(settings=settings, secrets=load_secrets(secrets_file))


def open_repository(settings: Settings, *, dry_run: bool) -> CatalogRepository:
    """Open the catalog; a dry run never creates or migrates a database on disk."""
    if dry_run and settings.storage.backend == "sqlite":
        path = _sqlite_path(settings.storage.url)
        if path is None or not path.exists():
            memory = settings.storage.model_copy(update={"url": "sqlite://"})
            repo = create_repository(memory)
            repo.initialize()
            return repo
        return create_repository(settings.storage)
    repo = create_repository(settings.storage)
    repo.initialize()
    return repo


def _sqlite_path(url: str) -> Path | None:
    if "///" not in url:
        return None
    raw = url.split("///", 1)[1]
    if not raw or raw == ":memory:":
        return None
    return Path(raw).expanduser()


def _print_summary(summary: BuildSummary) -> None:
    typer.echo(summary.render())


@app.command()
def build(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Compute everything but write nothing.")
    ] = False,
) -> None:
    """Build the catalog from metadata, query history and repository .sql files."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=dry_run)
    sources = build_sources(app_ctx.settings, app_ctx.secrets)
    builder = CatalogBuilder(app_ctx.settings, repo, sources)
    _print_summary(builder.build(dry_run=dry_run))


@app.command()
def refresh(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Compute everything but write nothing.")
    ] = False,
    tables: Annotated[
        str | None,
        typer.Option("--tables", help="Comma-separated catalog.schema.table names to refresh."),
    ] = None,
) -> None:
    """Incrementally update the catalog (new history, changed files, metadata diff)."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=dry_run)
    sources = build_sources(app_ctx.settings, app_ctx.secrets)
    builder = CatalogBuilder(app_ctx.settings, repo, sources)
    selected = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
    _print_summary(builder.refresh(dry_run=dry_run, tables=selected))


@app.command()
def report(
    ctx: typer.Context,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the Markdown report to this file.")
    ] = None,
    top: Annotated[int, typer.Option("--top", min=1, help="Rows per section.")] = 20,
) -> None:
    """Print a Markdown report: usage, join graph and lineage."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    text = render_report(repo, top=top)
    if out is None:
        typer.echo(text)
    else:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"report written to {out}")


@app.command("show-config")
def show_config(ctx: typer.Context) -> None:
    """Print the effective configuration (secrets are never included)."""
    app_ctx = _context(ctx)
    typer.echo(app_ctx.settings.model_dump_json(indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
