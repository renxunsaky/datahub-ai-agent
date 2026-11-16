"""Wire a ready-to-use :class:`AgentSession` from settings, secrets and the catalog."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from data_navigator.agent.graph import AgentRuntime, build_graph
from data_navigator.agent.session import AgentSession
from data_navigator.agent.tools import AgentTools
from data_navigator.audit.log import AuditLogger
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.config import Secrets, Settings
from data_navigator.llm.client import LLMClient
from data_navigator.llm.factory import build_llm
from data_navigator.search.factory import build_search
from data_navigator.starburst.end_user_client import EndUserClient, build_end_user_client


def build_agent_session(
    settings: Settings,
    secrets: Secrets,
    repository: CatalogRepository,
    *,
    user: str | None = None,
    llm: LLMClient | None = None,
    client: EndUserClient | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    thread_id: str | None = None,
) -> AgentSession:
    """Create a session bound to *user* (default ``DN_END_USER_USER``).

    The identity is fixed here and injected into the tools through the end-user
    client; the model never receives or chooses it.
    """
    identity = user or secrets.end_user_user
    if not identity:
        msg = "end-user identity missing: set DN_END_USER_USER"
        raise RuntimeError(msg)
    model = llm or build_llm(settings, secrets)
    if model is None:
        msg = "the agent needs an LLM: set llm.provider to openai_compatible or replay"
        raise RuntimeError(msg)
    end_user = client or build_end_user_client(
        settings, secrets, user=identity, schema_map=repository.schema_map()
    )
    search = build_search(settings, repository, model)
    tools = AgentTools(
        repository=repository,
        search=search,
        client=end_user,
        settings=settings.agent,
        samples=settings.samples,
    )
    audit = AuditLogger(repository, path=settings.audit.path)
    runtime = AgentRuntime(
        llm=model,
        tools=tools,
        settings=settings.agent,
        samples=settings.samples,
        audit=audit,
        user=identity,
    )
    graph = build_graph(runtime, checkpointer=checkpointer or InMemorySaver())
    return AgentSession(graph, user=identity, thread_id=thread_id)
