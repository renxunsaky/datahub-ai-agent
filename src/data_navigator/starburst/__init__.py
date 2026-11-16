"""Starburst access.

Two deliberately separate clients live here:

* ``builder_client.BuilderClient`` — the dedicated catalog-building user, allowed
  to read ``information_schema``, ``system.metadata`` and the configured
  query-history table only.
* ``end_user_client.EndUserClient`` — the person using the agent, used only for
  ``EXPLAIN`` and row-limited samples.

This package intentionally re-exports nothing so that importing one client never
pulls in the other (enforced by import-linter contracts in ``pyproject.toml``).
"""
