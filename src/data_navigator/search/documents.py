"""Turn catalog entries into searchable documents.

Two document families share one index, distinguished by the ``doc_id`` prefix:

* ``table:<catalog.schema.table>`` — name, comment, owner, card text, columns.
* ``template:<fingerprint>`` — tables touched and the de-identified SQL.

Documents never contain SQL constants (templates are normalised) or sample data.
"""

from __future__ import annotations

import re

from data_navigator.catalog.records import CardRecord, TableState, TemplateRecord
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.search.text_index import TextDocument

TABLE_PREFIX = "table:"
TEMPLATE_PREFIX = "template:"

_SPLIT = re.compile(r"[^0-9A-Za-z一-鿿]+")


def split_identifier(name: str) -> str:
    """``cos.retail_ods.customer`` -> ``cos retail ods customer`` (keeps the original too)."""
    tokens = [t for t in _SPLIT.split(name) if t]
    return " ".join([name, *tokens])


def table_document(table: TableState, card: CardRecord | None) -> TextDocument:
    parts = [split_identifier(table.full_name)]
    if table.comment:
        parts.append(table.comment)
    if table.owner_team:
        parts.append(f"owner {table.owner_team}")
    if card is not None and not card.is_rejected:
        for text in (card.purpose, card.common_usage):
            if text:
                parts.append(text)
        parts.extend(card.example_questions)
        for key_column in card.key_columns:
            parts.append(f"{key_column.name} {key_column.meaning}")
    for column in table.columns:
        column_text = split_identifier(column.name)
        if column.comment:
            column_text += f" {column.comment}"
        if column.ai_description:
            column_text += f" {column.ai_description}"
        parts.append(column_text)
    return TextDocument(
        doc_id=TABLE_PREFIX + table.full_name, title=table.full_name, body="\n".join(parts)
    )


def template_document(template: TemplateRecord) -> TextDocument:
    tables = [split_identifier(t) for t in template.tables_read + template.tables_written]
    body = "\n".join([*tables, template.statement_type, template.normalized_sql])
    return TextDocument(
        doc_id=TEMPLATE_PREFIX + template.fingerprint, title=template.fingerprint, body=body
    )


def build_documents(repository: CatalogRepository) -> list[TextDocument]:
    cards = {card.table: card for card in repository.list_cards()}
    documents = [
        table_document(table, cards.get(table.full_name)) for table in repository.list_tables()
    ]
    documents.extend(template_document(t) for t in repository.list_templates())
    return documents


def table_name_from_doc_id(doc_id: str) -> str | None:
    return doc_id[len(TABLE_PREFIX) :] if doc_id.startswith(TABLE_PREFIX) else None


def fingerprint_from_doc_id(doc_id: str) -> str | None:
    return doc_id[len(TEMPLATE_PREFIX) :] if doc_id.startswith(TEMPLATE_PREFIX) else None
