"""Command-line interface.

Catalog: ``build``, ``refresh``, ``cards``, ``index``, ``report``, ``review``.
Agent: ``chat``, ``ui``, ``audit``.  ``show-config`` prints the effective settings.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from data_navigator.agent.factory import build_agent_session
from data_navigator.agent.session import AgentReply, AgentSession
from data_navigator.catalog.factory import open_repository
from data_navigator.catalog.records import (
    CARD_STATUS_AI,
    CARD_STATUS_CONFIRMED,
    CARD_STATUS_REJECTED,
    CardRecord,
)
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.catalog.types import utcnow
from data_navigator.config import Secrets, Settings, load_secrets, load_settings
from data_navigator.llm.factory import build_llm
from data_navigator.pipeline.build import BuildSummary, CatalogBuilder
from data_navigator.pipeline.enrich import enrich_catalog
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
    config_path: Path | None = None


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
    ctx.obj = AppContext(settings=settings, secrets=load_secrets(secrets_file), config_path=config)


def _print_summary(summary: BuildSummary) -> None:
    typer.echo(summary.render())


def _split_tables(tables: str | None) -> list[str] | None:
    return [t.strip() for t in tables.split(",") if t.strip()] if tables else None


SkipCardsOption = Annotated[
    bool, typer.Option("--skip-cards", help="Do not (re)generate table cards.")
]
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Compute everything but write nothing.")
]


def _enrich_after_run(
    app_ctx: AppContext,
    repo: CatalogRepository,
    summary: BuildSummary,
    *,
    skip_cards: bool,
    dry_run: bool,
    only_changed: bool,
) -> None:
    if dry_run:
        return
    tables: Sequence[str] | None = None
    if only_changed:
        tables = sorted(set(summary.tables_added) | set(summary.tables_changed))
        if not tables and not skip_cards:
            # Nothing changed: still make sure every top table has a card.
            tables = None
    llm = build_llm(app_ctx.settings, app_ctx.secrets)
    enriched = enrich_catalog(
        app_ctx.settings, repo, llm, tables=tables, skip_cards=skip_cards, dry_run=False
    )
    typer.echo(enriched.render())


@app.command()
def build(
    ctx: typer.Context,
    dry_run: DryRunOption = False,
    skip_cards: SkipCardsOption = False,
) -> None:
    """Build the catalog from metadata, query history and repository .sql files."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=dry_run)
    sources = build_sources(app_ctx.settings, app_ctx.secrets)
    builder = CatalogBuilder(app_ctx.settings, repo, sources)
    summary = builder.build(dry_run=dry_run)
    _print_summary(summary)
    _enrich_after_run(
        app_ctx, repo, summary, skip_cards=skip_cards, dry_run=dry_run, only_changed=False
    )


@app.command()
def refresh(
    ctx: typer.Context,
    dry_run: DryRunOption = False,
    tables: Annotated[
        str | None,
        typer.Option("--tables", help="Comma-separated catalog.schema.table names to refresh."),
    ] = None,
    skip_cards: SkipCardsOption = False,
) -> None:
    """Incrementally update the catalog (new history, changed files, metadata diff)."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=dry_run)
    sources = build_sources(app_ctx.settings, app_ctx.secrets)
    builder = CatalogBuilder(app_ctx.settings, repo, sources)
    summary = builder.refresh(dry_run=dry_run, tables=_split_tables(tables))
    _print_summary(summary)
    _enrich_after_run(
        app_ctx, repo, summary, skip_cards=skip_cards, dry_run=dry_run, only_changed=True
    )


@app.command()
def cards(
    ctx: typer.Context,
    tables: Annotated[
        str | None, typer.Option("--tables", help="Comma-separated tables (default: top N).")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Regenerate even when the inputs are unchanged.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List the tables that would be (re)generated.")
    ] = False,
) -> None:
    """Generate or refresh table cards with the LLM, then rebuild the search index."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    llm = build_llm(app_ctx.settings, app_ctx.secrets)
    if llm is None:
        typer.echo("no LLM configured (llm.provider is 'none'); cannot generate cards", err=True)
        raise typer.Exit(code=2)
    enriched = enrich_catalog(
        app_ctx.settings, repo, llm, tables=_split_tables(tables), force=force, dry_run=dry_run
    )
    typer.echo(enriched.render())


@app.command()
def index(ctx: typer.Context) -> None:
    """Rebuild the full-text (and, with an embedding model, vector) search index."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    llm = build_llm(app_ctx.settings, app_ctx.secrets)
    enriched = enrich_catalog(app_ctx.settings, repo, llm, skip_cards=True)
    typer.echo(enriched.render())


# -- review --------------------------------------------------------------------------

review_app = typer.Typer(help="Review AI-generated table cards.", no_args_is_help=True)
app.add_typer(review_app, name="review")


def _render_card(card: CardRecord) -> str:
    status = f"status: {card.review_status}"
    if card.reviewed_by:
        status += f" (by {card.reviewed_by})"
    if card.confirmed_fields:
        status += f"; confirmed fields: {', '.join(card.confirmed_fields)}"
    confidence = card.confidence if card.confidence is not None else "-"
    generated = card.generated_at.isoformat() if card.generated_at else "-"
    lines = [
        f"# {card.table}",
        status,
        f"confidence: {confidence}; model: {card.model or '-'}; generated: {generated}",
        "",
        f"purpose: {card.purpose or '-'}",
        "",
        "key columns:",
    ]
    for column in card.key_columns:
        evidence = f" [{column.evidence}]" if column.evidence else ""
        lines.append(f"  - {column.name}: {column.meaning} ({column.confidence:.2f}){evidence}")
    if not card.key_columns:
        lines.append("  (none)")
    lines += ["", f"common usage: {card.common_usage or '-'}", "", "example questions:"]
    lines += [f"  - {q}" for q in card.example_questions] or ["  (none)"]
    lines += ["", "evidence:"]
    lines += [f"  - {e}" for e in card.evidence] or ["  (none)"]
    return "\n".join(lines)


@review_app.command("list")
def review_list(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option("--status", help="ai_inferred (default) | confirmed | rejected | all"),
    ] = CARD_STATUS_AI,
) -> None:
    """List cards waiting for review (or with the given status)."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    wanted = None if status == "all" else status
    cards_found = repo.list_cards(status=wanted)
    if not cards_found:
        typer.echo("no cards")
        return
    for card in cards_found:
        confidence = f"{card.confidence:.2f}" if card.confidence is not None else "-"
        typer.echo(f"{card.table}\t{card.review_status}\t{confidence}\t{card.purpose or ''}")


@review_app.command("show")
def review_show(ctx: typer.Context, table: str) -> None:
    """Print one card in full."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    card = repo.get_card(table)
    if card is None:
        typer.echo(f"no card for {table}", err=True)
        raise typer.Exit(code=1)
    typer.echo(_render_card(card))


def _reviewer(reviewer: str | None, secrets: Secrets) -> str:
    name = reviewer or secrets.end_user_user
    if not name:
        typer.echo("reviewer unknown: pass --by or set DN_END_USER_USER", err=True)
        raise typer.Exit(code=2)
    return name


ReviewerOption = Annotated[str | None, typer.Option("--by", help="Reviewer name.")]


@review_app.command("confirm")
def review_confirm(ctx: typer.Context, table: str, by: ReviewerOption = None) -> None:
    """Mark a card as confirmed; its fields are never regenerated afterwards."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    card = repo.review_card(
        table,
        status=CARD_STATUS_CONFIRMED,
        reviewed_by=_reviewer(by, app_ctx.secrets),
        reviewed_at=utcnow(),
    )
    if card is None:
        typer.echo(f"unknown table {table}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{table}: confirmed")


@review_app.command("reject")
def review_reject(ctx: typer.Context, table: str, by: ReviewerOption = None) -> None:
    """Mark a card as rejected; it is hidden from search and the agent."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    card = repo.review_card(
        table,
        status=CARD_STATUS_REJECTED,
        reviewed_by=_reviewer(by, app_ctx.secrets),
        reviewed_at=utcnow(),
    )
    if card is None:
        typer.echo(f"unknown table {table}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{table}: rejected")


@review_app.command("edit")
def review_edit(
    ctx: typer.Context,
    table: str,
    purpose: Annotated[str | None, typer.Option("--purpose")] = None,
    common_usage: Annotated[str | None, typer.Option("--common-usage")] = None,
    example_questions: Annotated[
        str | None, typer.Option("--example-questions", help="JSON list of strings.")
    ] = None,
    key_columns: Annotated[
        str | None, typer.Option("--key-columns", help="JSON list of {name, meaning, ...}.")
    ] = None,
    by: ReviewerOption = None,
    confirm: Annotated[
        bool, typer.Option("--confirm/--keep-status", help="Also mark the card confirmed.")
    ] = True,
) -> None:
    """Edit card fields; edited fields are kept on every future regeneration."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    edits: dict[str, Any] = {}
    if purpose is not None:
        edits["purpose"] = purpose
    if common_usage is not None:
        edits["common_usage"] = common_usage
    if example_questions is not None:
        edits["example_questions"] = _json_list(example_questions, "--example-questions")
    if key_columns is not None:
        edits["key_columns"] = _json_list(key_columns, "--key-columns")
    if not edits:
        typer.echo("nothing to edit", err=True)
        raise typer.Exit(code=2)
    existing = repo.get_card(table)
    status = CARD_STATUS_CONFIRMED if confirm or existing is None else existing.review_status
    card = repo.review_card(
        table,
        status=status,
        reviewed_by=_reviewer(by, app_ctx.secrets),
        reviewed_at=utcnow(),
        edits=edits,
    )
    if card is None:
        typer.echo(f"unknown table {table}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{table}: edited {', '.join(sorted(edits))}; status {card.review_status}")


# -- agent -----------------------------------------------------------------------------


def _print_reply(reply: AgentReply) -> None:
    typer.echo(reply.text)
    if reply.sample is not None:
        sample = reply.sample
        if sample.get("status") == "ok":
            typer.echo("")
            typer.echo(
                f"sample ({sample.get('row_count', 0)} row(s), limit {sample.get('row_limit')}):"
            )
            typer.echo(" | ".join(sample.get("columns", [])))
            for row in sample.get("rows", []):
                typer.echo(" | ".join(str(v) for v in row))
        else:
            typer.echo(f"sample not executed: {sample.get('status')} {sample.get('message') or ''}")


def _confirm_loop(session: AgentSession, reply: AgentReply) -> AgentReply:
    """Resolve pending sample confirmations with a [y/N] prompt."""
    while reply.needs_confirmation:
        typer.echo("The agent wants to run this SQL with your identity:")
        typer.echo(reply.sql or "")
        max_rows = reply.pending.get("max_rows") if reply.pending else None
        approved = typer.confirm(f"Allow reading at most {max_rows} sample row(s)?", default=False)
        reply = session.resume(approved)
    return reply


@app.command()
def chat(
    ctx: typer.Context,
    user: Annotated[
        str | None, typer.Option("--user", help="Override DN_END_USER_USER (local runs only).")
    ] = None,
) -> None:
    """Interactive conversation: describe the data you need, get validated read-only SQL."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    try:
        session = build_agent_session(app_ctx.settings, app_ctx.secrets, repo, user=user)
    except RuntimeError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(f"Data Navigator chat as {session.user}. Type 'exit' to quit.")
    while True:
        try:
            question = typer.prompt("you")
        except (EOFError, KeyboardInterrupt, click.Abort):
            typer.echo("")
            break
        if question.strip().lower() in {"exit", "quit", "q"}:
            break
        if not question.strip():
            continue
        reply = _confirm_loop(session, session.ask(question))
        _print_reply(reply)


@app.command()
def ui(
    ctx: typer.Context,
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8501,
) -> None:
    """Start the local Streamlit page (single user, reads credentials from .env)."""
    app_ctx = _context(ctx)
    script = Path(__file__).resolve().parent / "ui" / "streamlit_app.py"
    env = dict(os.environ)
    config_path = (
        ctx.params.get("config") or ctx.parent.params.get("config") if ctx.parent else None
    )
    if config_path:
        env["DN_CONFIG_PATH"] = str(config_path)
    elif DEFAULT_CONFIG.exists():
        env["DN_CONFIG_PATH"] = str(DEFAULT_CONFIG.resolve())
    env.setdefault("DN_END_USER_USER", app_ctx.secrets.end_user_user or "")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(script),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    typer.echo(f"starting Streamlit on http://localhost:{port}")
    result = subprocess.run(command, env=env, check=False)
    raise typer.Exit(code=result.returncode)


@app.command()
def audit(
    ctx: typer.Context,
    user: Annotated[str | None, typer.Option("--user")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 50,
) -> None:
    """Show recent audit entries (question, de-identified SQL, validation, sample facts)."""
    app_ctx = _context(ctx)
    repo = open_repository(app_ctx.settings, dry_run=True)
    entries = repo.list_audit_entries(user=user, limit=limit)
    if not entries:
        typer.echo("no audit entries")
        return
    for entry in entries:
        when = entry.occurred_at.isoformat() if entry.occurred_at else "-"
        sample = (
            f"sample confirmed={entry.sample_confirmed} executed={entry.sample_executed} "
            f"rows={entry.sample_row_count}"
            if entry.sample_confirmed is not None
            else "no sample"
        )
        typer.echo(
            f"{when} {entry.user} [{entry.details.get('mode', entry.details.get('event', '-'))}] "
            f"validation={entry.validation_status or '-'} {sample}\n  q: {entry.question}"
            + (f"\n  sql: {entry.generated_sql}" if entry.generated_sql else "")
        )


def _json_list(raw: str, option: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except ValueError as error:
        typer.echo(f"{option} must be a JSON list", err=True)
        raise typer.Exit(code=2) from error
    if not isinstance(value, list):
        typer.echo(f"{option} must be a JSON list", err=True)
        raise typer.Exit(code=2)
    return value


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
