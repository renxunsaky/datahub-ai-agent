"""Generate and refresh table cards with the LLM.

* Only the top ``N`` tables by usage are considered (configurable).
* A card is regenerated only when its context pack hash changed (or ``force``).
* Fields a reviewer edited or confirmed are never overwritten (repository rule).
* Invalid model output is retried up to ``max_generation_attempts`` times.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from data_navigator.cards.context import build_context_pack, top_tables
from data_navigator.cards.prompts import SYSTEM_PROMPT, user_prompt
from data_navigator.cards.schemas import CardOutputError, parse_card_output
from data_navigator.catalog.records import CardRecord, TableState
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.catalog.types import utcnow
from data_navigator.config import CardsSettings
from data_navigator.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)


@dataclass
class CardGenerationSummary:
    considered: int = 0
    generated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped_reviewed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        line = (
            f"  cards: {self.considered} considered, {len(self.generated)} generated, "
            f"{len(self.unchanged)} unchanged, {len(self.skipped_reviewed)} reviewed (kept), "
            f"{len(self.failed)} failed"
        )
        for table, reason in sorted(self.failed.items()):
            line += f"\n    failed {table}: {reason}"
        return line


class CardGenerator:
    def __init__(
        self,
        llm: LLMClient,
        repository: CatalogRepository,
        settings: CardsSettings,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._llm = llm
        self._repo = repository
        self._settings = settings
        self._now = now

    def generate(
        self,
        *,
        tables: Sequence[str] | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> CardGenerationSummary:
        """Generate cards for *tables* (default: the top-N tables by usage)."""
        summary = CardGenerationSummary()
        all_tables = self._repo.list_tables()
        if tables:
            wanted = {name.lower() for name in tables}
            selected = [t for t in all_tables if t.full_name in wanted]
        else:
            selected = top_tables(all_tables, self._settings.top_n_tables)
        summary.considered = len(selected)
        if not selected:
            return summary
        templates = self._repo.list_templates()
        join_edges = self._repo.list_join_edges()
        existing = {card.table: card for card in self._repo.list_cards()}
        for table in selected:
            pack = build_context_pack(
                table, templates=templates, join_edges=join_edges, settings=self._settings
            )
            current = existing.get(table.full_name)
            if current is not None and current.review_status != "ai_inferred":
                summary.skipped_reviewed.append(table.full_name)
                continue
            if not force and current is not None and current.input_hash == pack.hash:
                summary.unchanged.append(table.full_name)
                continue
            if dry_run:
                summary.generated.append(table.full_name)
                continue
            try:
                self._generate_one(table, pack.render(), pack.hash)
            except (LLMError, CardOutputError) as error:
                summary.failed[table.full_name] = f"{type(error).__name__}: {error}"
                log.warning("card generation failed for %s: %s", table.full_name, type(error))
                continue
            summary.generated.append(table.full_name)
        return summary

    def _generate_one(self, table: TableState, rendered: str, input_hash: str) -> CardRecord:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt(rendered)},
        ]
        known = [column.name for column in table.columns]
        last_error: CardOutputError | None = None
        for attempt in range(1, self._settings.max_generation_attempts + 1):
            response = self._llm.chat(messages, json_mode=True)
            try:
                output = parse_card_output(response.content, known_columns=known)
            except CardOutputError as error:
                last_error = error
                log.info("card output invalid for %s (attempt %d)", table.full_name, attempt)
                messages.append({"role": "assistant", "content": response.content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": f"That reply was invalid ({error}). Reply with a corrected "
                        "JSON object only.",
                    }
                )
                continue
            draft = output.to_draft()
            with self._repo.transaction():
                card = self._repo.save_generated_card(
                    table.full_name,
                    draft,
                    input_hash=input_hash,
                    model=self._llm.model_name,
                    generated_at=self._now(),
                )
                if card is not None:
                    self._repo.set_column_descriptions(
                        table.full_name, {c.name: c.meaning for c in draft.key_columns}
                    )
            if card is None:  # pragma: no cover - the table was listed a moment ago
                msg = f"table {table.full_name} vanished during generation"
                raise CardOutputError(msg)
            return card
        assert last_error is not None
        raise last_error


def tables_needing_cards(repository: CatalogRepository, names: Iterable[str]) -> list[str]:
    """Among *names*, those that have no card yet."""
    cards = {card.table for card in repository.list_cards()}
    return [name for name in names if name.lower() not in cards]
