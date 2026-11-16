"""Create the configured LLM client (or none)."""

from __future__ import annotations

import logging

from data_navigator.config import Secrets, Settings
from data_navigator.llm.client import LLMClient, OpenAICompatibleClient
from data_navigator.llm.replay import ReplayLLM

log = logging.getLogger(__name__)


def build_llm(settings: Settings, secrets: Secrets) -> LLMClient | None:
    """Return the configured client; ``None`` when LLM features are disabled."""
    llm = settings.llm
    if llm.provider == "none":
        log.info("llm.provider is 'none': table cards and vector search are disabled")
        return None
    if llm.provider == "replay":
        return ReplayLLM.from_yaml(llm.replay_path)
    return OpenAICompatibleClient(llm, secrets.llm_api_key)
