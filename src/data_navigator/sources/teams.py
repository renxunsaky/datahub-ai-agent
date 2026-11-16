"""User-to-team mapping."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class TeamResolver(Protocol):
    def team_for_user(self, user: str | None) -> str | None: ...


class ConfigTeamResolver:
    """Static mapping from configuration. TODO(confirm): authoritative source (LDAP, HR, ...)."""

    def __init__(self, mapping: Mapping[str, str], *, default_team: str | None = None) -> None:
        self._mapping = {user.lower(): team for user, team in mapping.items()}
        self._default = default_team

    def team_for_user(self, user: str | None) -> str | None:
        if not user:
            return None
        return self._mapping.get(user.lower(), self._default)
