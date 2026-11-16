#!/usr/bin/env python
"""Run the golden set against the agent and print a report.

    uv run python eval/run_eval.py --config config.yaml \
        --golden eval/golden_set.example.yaml --out eval/report.md

With ``llm.provider: replay`` this runs fully offline against the fixtures;
with the real LLMaaS and Starburst it measures the actual agent.
"""

from __future__ import annotations

import sys

from data_navigator.evaluation import main

if __name__ == "__main__":
    sys.exit(main())
