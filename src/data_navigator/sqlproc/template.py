"""Template handling for ``.sql`` files and statement splitting.

Airflow renders ``.sql`` files with Jinja before execution.  We cannot render
them (no runtime context), so template expressions are replaced with a fixed
placeholder identifier that keeps the statement parseable:

* ``{{ ... }}`` becomes the placeholder (``'{{ ds }}'`` stays a string literal,
  ``{{ params.table }}`` becomes an identifier);
* ``{% ... %}`` tags are removed and their body kept;
* ``{# ... #}`` comments are removed;
* additional patterns (``${var}``, ``%(name)s``, ...) come from configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from data_navigator.config import TemplateSettings

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_TAG = re.compile(r"\{%.*?%\}", re.DOTALL)
_JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.DOTALL)


@dataclass(frozen=True)
class TemplateResult:
    text: str
    replacements: int


class TemplateProcessor:
    def __init__(self, settings: TemplateSettings | None = None) -> None:
        self._settings = settings or TemplateSettings()
        self._extra = [re.compile(pattern, re.DOTALL) for pattern in self._settings.extra_patterns]

    @property
    def placeholder(self) -> str:
        return self._settings.placeholder

    def render(self, text: str) -> TemplateResult:
        """Replace template syntax with the placeholder identifier."""
        count = 0
        if self._settings.jinja:
            text, n = _JINJA_COMMENT.subn("", text)
            count += n
            text, n = _JINJA_TAG.subn("", text)
            count += n
            text, n = _JINJA_EXPR.subn(self.placeholder, text)
            count += n
        for pattern in self._extra:
            text, n = pattern.subn(self.placeholder, text)
            count += n
        return TemplateResult(text=text, replacements=count)

    def split_statements(self, text: str) -> list[str]:
        """Split on ``;`` outside strings, quoted identifiers and comments.

        Statements that contain only whitespace or comments are dropped.
        """
        return split_statements(text)


def split_statements(text: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    code: list[str] = []  # non-comment characters, used to drop comment-only chunks
    i = 0
    n = len(text)
    quote: str | None = None
    while i < n:
        char = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if quote is not None:
            buffer.append(char)
            code.append(char)
            if char == quote:
                if nxt == quote:  # doubled quote inside the literal
                    buffer.append(nxt)
                    code.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char == "-" and nxt == "-":
            end = text.find("\n", i)
            end = n if end == -1 else end
            buffer.append(text[i:end])
            i = end
            continue
        if char == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buffer.append(text[i:end])
            i = end
            continue
        if char in ("'", '"', "`"):
            quote = char
            buffer.append(char)
            code.append(char)
            i += 1
            continue
        if char == ";":
            _flush(statements, buffer, code)
            i += 1
            continue
        buffer.append(char)
        code.append(char)
        i += 1
    _flush(statements, buffer, code)
    return statements


def _flush(statements: list[str], buffer: list[str], code: list[str]) -> None:
    statement = "".join(buffer).strip()
    if statement and "".join(code).strip():
        statements.append(statement)
    buffer.clear()
    code.clear()
