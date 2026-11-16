"""Shared pytest configuration."""

from __future__ import annotations

import os

import pytest

from data_navigator.sqlproc.statements import quiet_sqlglot


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure the developer's own ``DN_*`` variables never leak into tests."""
    for name in list(os.environ):
        if name.startswith("DN_"):
            monkeypatch.delenv(name, raising=False)
    quiet_sqlglot()
