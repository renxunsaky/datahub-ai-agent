"""The conversational agent (phases 3-4).

Architecture rule (enforced by import-linter and ``tests/unit/test_architecture.py``):
nothing in this package may import the builder client, the catalog sources or
the build pipeline.  The agent only ever sees the catalog repository, the search
index and the end-user Starburst client.
"""
