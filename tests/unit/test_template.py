from __future__ import annotations

from data_navigator.config import TemplateSettings
from data_navigator.sqlproc.template import TemplateProcessor, split_statements


def test_jinja_expressions_tags_and_comments() -> None:
    processor = TemplateProcessor()
    text = (
        "{# comment with 'quotes' and ; #}\n"
        "SELECT * FROM {{ params.table }} WHERE dt = '{{ ds }}'\n"
        "{% if params.debit_only %} AND txn_type = 'DEBIT' {% endif %}\n"
        "AND x = {{ macros.ds_add(ds, -1) }}"
    )
    result = processor.render(text)
    assert result.replacements == 6
    assert "{{" not in result.text and "{%" not in result.text and "{#" not in result.text
    assert "FROM dn_tpl_var WHERE dt = 'dn_tpl_var'" in result.text
    assert "AND txn_type = 'DEBIT'" in result.text
    assert result.text.rstrip().endswith("AND x = dn_tpl_var")


def test_extra_patterns_and_disabled_jinja() -> None:
    processor = TemplateProcessor(TemplateSettings(jinja=False, placeholder="ph"))
    result = processor.render("SELECT ${var} FROM t WHERE a = '%(name)s' AND b = {{ keep }}")
    assert result.text == "SELECT ph FROM t WHERE a = 'ph' AND b = {{ keep }}"
    assert result.replacements == 2
    assert processor.placeholder == "ph"


def test_multiline_jinja_expression() -> None:
    result = TemplateProcessor().render("SELECT {{\n  value\n}} FROM t")
    assert result.text == "SELECT dn_tpl_var FROM t"


def test_split_statements_respects_strings_and_comments() -> None:
    text = (
        "SELECT 'a;b' AS x; -- trailing; comment\n"
        '/* block; comment */ SELECT "q;q" FROM t;\n'
        "SELECT 'it''s; fine' FROM `odd;name`;;\n"
        "-- only a comment\n"
        "   \n"
        "SELECT 3"
    )
    statements = split_statements(text)
    assert statements == [
        "SELECT 'a;b' AS x",
        '-- trailing; comment\n/* block; comment */ SELECT "q;q" FROM t',
        "SELECT 'it''s; fine' FROM `odd;name`",
        "-- only a comment\n   \nSELECT 3",
    ]


def test_split_statements_drops_comment_only_chunks() -> None:
    assert split_statements("-- nothing here\n/* nor here */;  ;") == []
    assert split_statements("") == []
    assert TemplateProcessor().split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_unterminated_constructs_do_not_crash() -> None:
    assert split_statements("SELECT 'unterminated; DROP") == ["SELECT 'unterminated; DROP"]
    assert split_statements("SELECT 1 /* unterminated; DROP") == ["SELECT 1 /* unterminated; DROP"]
