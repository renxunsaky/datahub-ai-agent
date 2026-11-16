"""The conversational agent (phase 3).

Architecture rule (enforced by import-linter and ``tests/unit/test_architecture.py``):
nothing in this package may import the builder client, the catalog sources or
the build pipeline.  The agent only ever sees the catalog repository and the
end-user Starburst client.
"""
