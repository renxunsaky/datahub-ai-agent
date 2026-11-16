"""Fake Trino connection objects used by the client tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from trino.exceptions import TrinoUserError


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self.description: Sequence[Sequence[Any]] | None = None
        self.closed = False

    def execute(self, operation: str, params: Any = None) -> None:
        self._connection.executed.append(operation)
        if self._connection.error is not None:
            raise self._connection.error
        self.description = [(name, "varchar") for name in self._connection.columns]

    def fetchall(self) -> Sequence[Sequence[Any]]:
        return list(self._connection.rows)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(
        self,
        rows: Sequence[Sequence[Any]] = (),
        columns: Sequence[str] = (),
        error: BaseException | None = None,
    ) -> None:
        self.rows = list(rows)
        self.columns = list(columns)
        self.error = error
        self.executed: list[str] = []
        self.closed = False
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    """A connection factory that counts how often it is called."""

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls = 0

    def __call__(self) -> FakeConnection:
        self.calls += 1
        return self.connection


def trino_error(
    error_name: str, error_type: str = "USER_ERROR", message: str = "boom"
) -> TrinoUserError:
    return TrinoUserError(
        {"errorName": error_name, "errorType": error_type, "message": message, "errorCode": 1},
        "20240101_000000_00001_abcde",
    )


class RoutingConnection:
    """A fake connection whose response depends on the statement executed."""

    def __init__(
        self, route: Callable[[str], tuple[Sequence[str], Sequence[Sequence[Any]]]]
    ) -> None:
        self._route = route
        self.executed: list[str] = []
        self.closed = False

    def cursor(self) -> RoutingCursor:
        return RoutingCursor(self)

    def close(self) -> None:
        self.closed = True


class RoutingCursor:
    def __init__(self, connection: RoutingConnection) -> None:
        self._connection = connection
        self.description: Sequence[Sequence[Any]] | None = None
        self._rows: Sequence[Sequence[Any]] = []

    def execute(self, operation: str, params: Any = None) -> None:
        self._connection.executed.append(operation)
        columns, rows = self._connection._route(operation)
        self.description = [(name, "varchar") for name in columns]
        self._rows = rows

    def fetchall(self) -> Sequence[Sequence[Any]]:
        return list(self._rows)

    def close(self) -> None:
        pass
