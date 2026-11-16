"""Single-page Streamlit chat for one user.

Run with ``data-navigator ui`` (which sets ``DN_CONFIG_PATH``) or directly with
``streamlit run src/data_navigator/ui/streamlit_app.py``.  The user's Starburst
credentials come from the environment / ``.env`` (``DN_END_USER_*``); nothing
here lets the person pick another identity.

Sample rows are kept in the Streamlit session only: they are never written to
the catalog, the logs or the audit trail.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import streamlit as st

from data_navigator.agent.factory import build_agent_session
from data_navigator.agent.session import AgentReply, AgentSession
from data_navigator.catalog.factory import open_repository
from data_navigator.config import load_secrets, load_settings
from data_navigator.sqlproc.statements import quiet_sqlglot

CONFIRM_LABEL = "允许读取样例 / Allow sample"
DECLINE_LABEL = "拒绝 / Decline"


def create_session() -> AgentSession:
    quiet_sqlglot()
    config_env = os.environ.get("DN_CONFIG_PATH")
    config_path = Path(config_env) if config_env else None
    if config_path is None and Path("config.yaml").exists():
        config_path = Path("config.yaml")
    settings = load_settings(config_path)
    secrets = load_secrets(".env" if Path(".env").exists() else None)
    repository = open_repository(settings, dry_run=True)
    return build_agent_session(settings, secrets, repository)


def _render_reply(reply: AgentReply) -> None:
    if reply.mode == "sql" and reply.sql:
        st.markdown("**Validated read-only SQL** (EXPLAIN passed with your identity)")
        st.code(reply.sql, language="sql")
        if reply.tables:
            st.markdown("Tables: " + ", ".join(f"`{t}`" for t in reply.tables))
        if reply.joins:
            st.markdown("Joins: " + "; ".join(reply.joins))
        if reply.assumptions:
            st.markdown("Assumptions: " + "; ".join(reply.assumptions))
        if reply.explanation:
            st.markdown(reply.explanation)
        if reply.plan:
            with st.expander("EXPLAIN (excerpt)"):
                st.code(reply.plan)
    elif reply.mode == "candidates":
        st.markdown("I am not confident enough to write SQL yet. Possible tables:")
        for candidate in reply.candidates:
            purpose = candidate.get("purpose") or "no description"
            if "ai_inferred" in str(candidate.get("purpose_source") or ""):
                purpose += " *(AI inferred)*"
            st.markdown(
                f"- `{candidate['table']}` — {candidate.get('owner_team') or 'unknown owner'}; "
                f"access: **{candidate.get('access')}** — {purpose}"
                + (f"  \n  why: {candidate['why']}" if candidate.get("why") else "")
            )
        if reply.explanation:
            st.markdown(reply.explanation)
        for question in reply.clarifying_questions:
            st.markdown(f"**Question:** {question}")
    else:
        st.markdown(reply.text or "")
    for warning in reply.warnings:
        st.warning(warning)
    if reply.sample is not None:
        sample = reply.sample
        if sample.get("status") == "ok":
            st.markdown(
                f"Sample: {sample.get('row_count', 0)} row(s), limit {sample.get('row_limit')}"
            )
            st.dataframe(
                [dict(zip(sample.get("columns", []), row, strict=False)) for row in sample["rows"]]
            )
        else:
            st.error(f"sample not executed: {sample.get('status')} {sample.get('message') or ''}")


def _record(role: str, reply: AgentReply | None = None, text: str | None = None) -> None:
    st.session_state.history.append({"role": role, "reply": reply, "text": text})


def main() -> None:
    st.set_page_config(page_title="Data Navigator", layout="wide")
    st.title("Data Navigator")
    if "session" not in st.session_state:
        try:
            st.session_state.session = create_session()
        except Exception as error:
            st.error(f"cannot start the agent: {type(error).__name__}: {error}")
            st.stop()
        st.session_state.history = []
        st.session_state.pending = None
    session: AgentSession = st.session_state.session
    st.caption(f"Signed in as **{session.user}** (identity from the environment).")

    for item in st.session_state.history:
        with st.chat_message(item["role"]):
            reply = item.get("reply")
            if reply is not None:
                _render_reply(reply)
            else:
                st.markdown(item.get("text") or "")

    pending: dict[str, Any] | None = st.session_state.pending
    if pending is not None:
        with st.chat_message("assistant"):
            st.markdown(
                "The agent wants to run this SQL with your identity and return at most "
                f"**{pending.get('max_rows')}** rows. Allow it?"
            )
            st.code(pending.get("sql") or "", language="sql")
            allow, decline = st.columns(2)
            if allow.button(CONFIRM_LABEL, type="primary"):
                st.session_state.pending = None
                reply = session.resume(True)
                _record("assistant", reply=reply)
                st.rerun()
            if decline.button(DECLINE_LABEL):
                st.session_state.pending = None
                reply = session.resume(False)
                _record("assistant", reply=reply)
                st.rerun()
        return

    question = st.chat_input("Describe the data you need")
    if question:
        _record("user", text=question)
        reply = session.ask(question)
        if reply.needs_confirmation:
            st.session_state.pending = reply.pending
        else:
            _record("assistant", reply=reply)
        st.rerun()


if __name__ == "__main__":  # pragma: no cover - streamlit entry point
    main()
else:  # Streamlit executes the script as __main__; keep the module importable in tests.
    pass
