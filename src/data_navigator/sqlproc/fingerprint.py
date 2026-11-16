"""Pattern fingerprints."""

from __future__ import annotations

import hashlib

from data_navigator.sqlproc.normalize import NormalizedSQL


def fingerprint(normalized: NormalizedSQL | str) -> str:
    """SHA-256 of the normalised text; identical for all constants and formatting."""
    text = normalized.text if isinstance(normalized, NormalizedSQL) else normalized
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
