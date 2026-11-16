"""Post-build enrichment: table cards (LLM) and the search index.

Runs after ``build`` / ``refresh`` and on demand (``cards`` / ``index``
commands).  Without an LLM the cards step is skipped and the index is text-only.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from data_navigator.cards.generate import CardGenerationSummary, CardGenerator
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.config import Settings
from data_navigator.llm.client import LLMClient
from data_navigator.search.factory import build_search, reindex
from data_navigator.search.hybrid import IndexSummary

log = logging.getLogger(__name__)


@dataclass
class EnrichSummary:
    cards: CardGenerationSummary | None = None
    index: IndexSummary | None = None
    cards_skipped_reason: str | None = None

    def render(self) -> str:
        lines = []
        if self.cards is not None:
            lines.append(self.cards.render())
        elif self.cards_skipped_reason:
            lines.append(f"  cards: skipped ({self.cards_skipped_reason})")
        if self.index is not None:
            lines.append(self.index.render())
        return "\n".join(lines)


def enrich_catalog(
    settings: Settings,
    repository: CatalogRepository,
    llm: LLMClient | None,
    *,
    tables: Sequence[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    skip_cards: bool = False,
    skip_index: bool = False,
) -> EnrichSummary:
    summary = EnrichSummary()
    if skip_cards:
        summary.cards_skipped_reason = "--skip-cards"
    elif llm is None:
        summary.cards_skipped_reason = "no LLM configured (llm.provider: none)"
    else:
        generator = CardGenerator(llm, repository, settings.cards)
        summary.cards = generator.generate(tables=tables, force=force, dry_run=dry_run)
    if not skip_index and not dry_run:
        search = build_search(settings, repository, llm)
        summary.index = reindex(repository, search)
    return summary
