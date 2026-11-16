"""Low-level Trino connection plumbing shared by both clients.

The clients never construct connections themselves; they receive a
``ConnectionFactory`` so tests can inject fakes and so that credentials stay in
one place.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import SecretStr

from data_navigator.config import StarburstConnectionSettings


class CursorLike(Protocol):
    """Subset of the DB-API cursor used by the clients."""

    description: Sequence[Sequence[Any]] | None

    def execute(self, operation: str, params: Any = None) -> Any: ...

    def fetchall(self) -> Sequence[Sequence[Any]]: ...

    def close(self) -> None: ...


class ConnectionLike(Protocol):
    """Subset of the DB-API connection used by the clients."""

    def cursor(self) -> CursorLike: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[], ConnectionLike]


@dataclass(frozen=True)
class Credentials:
    """Credentials of one Starburst identity. Never logged; ``SecretStr`` masks values."""

    user: str
    password: SecretStr | None = None
    jwt: SecretStr | None = None


def make_connection_factory(
    settings: StarburstConnectionSettings,
    credentials: Credentials,
    *,
    source_suffix: str,
    session_properties: Mapping[str, str] | None = None,
) -> ConnectionFactory:
    """Build a factory that opens a fresh ``trino.dbapi.Connection`` on each call."""

    def factory() -> ConnectionLike:
        # Imported lazily so unit tests never need a Trino server or the client's
        # optional dependencies.
        import trino.auth
        import trino.dbapi

        auth: Any = None
        if settings.auth == "basic":
            if credentials.password is None:
                msg = f"password missing for Starburst user {credentials.user!r}"
                raise RuntimeError(msg)
            auth = trino.auth.BasicAuthentication(
                credentials.user, credentials.password.get_secret_value()
            )
        elif settings.auth == "jwt":
            if credentials.jwt is None:
                msg = f"JWT missing for Starburst user {credentials.user!r}"
                raise RuntimeError(msg)
            auth = trino.auth.JWTAuthentication(credentials.jwt.get_secret_value())

        connection: ConnectionLike = trino.dbapi.connect(  # type: ignore[no-untyped-call]
            host=settings.host,
            port=settings.port,
            user=credentials.user,
            http_scheme=settings.http_scheme,
            auth=auth,
            verify=settings.verify_ssl,
            request_timeout=settings.request_timeout_s,
            source=f"{settings.source_tag}-{source_suffix}",
            session_properties=dict(session_properties or {}),
        )
        return connection

    return factory
