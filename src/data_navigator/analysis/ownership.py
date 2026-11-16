"""Owning-team inference from naming rules.

TODO(confirm): whether teams are identifiable from schema names, COS bucket names
or a separate registry; the rules are configurable and results are always marked
``inferred`` so a confirmed owner is never overwritten.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from data_navigator.config import OwnershipRule


def infer_owner(
    *,
    full_name: str,
    schema_name: str,
    rules: Sequence[OwnershipRule],
    bucket: str | None = None,
) -> str | None:
    for rule in rules:
        if rule.applies_to == "schema":
            subject = schema_name
        elif rule.applies_to == "full_name":
            subject = full_name
        else:
            if bucket is None:
                continue
            subject = bucket
        match = re.search(rule.pattern, subject, flags=re.IGNORECASE)
        if match is None:
            continue
        team: str | None
        if rule.team:
            try:
                team = match.expand(rule.team)
            except (re.error, IndexError):
                team = rule.team
        else:
            team = match.groupdict().get("team") or (match.group(1) if match.groups() else None)
        if team:
            return team.lower()
    return None
