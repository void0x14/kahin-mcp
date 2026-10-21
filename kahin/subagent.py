"""Subagent & Harness Identity and Isolation Routing.

Resolves caller identity from harness metadata (Claude Code, OpenCode, Codex,
Grok Build), environment variables, or explicit tool arguments, and scopes
tool execution to the subagent's dedicated browser tab and context.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any

# Active subagent execution context for the current asyncio task.
# Holds {"caller_id": str | None, "target_id": str | None}
_active_subagent_context: ContextVar[dict[str, Any]] = ContextVar(
    "_active_subagent_context",
    default={},
)


def get_active_subagent_context() -> dict[str, Any]:
    """Return the active subagent context dictionary."""
    return _active_subagent_context.get()


def set_active_subagent_context(
    caller_id: str | None = None,
    target_id: str | None = None,
) -> Token:
    """Bind caller identity and optional target_id for the current asyncio task."""
    current = dict(_active_subagent_context.get())
    if caller_id is not None:
        current["caller_id"] = caller_id
    if target_id is not None:
        current["target_id"] = target_id
    return _active_subagent_context.set(current)


def reset_active_subagent_context(token: Token) -> None:
    """Reset the context to the previous token state."""
    _active_subagent_context.reset(token)


def extract_caller_identity(
    arguments: dict[str, Any] | None = None,
    context: Any = None,
) -> tuple[str | None, str | None]:
    """Resolve caller identity and explicit target_id across harnesses.

    Priority order:
    1. Explicit tool arguments (agent_id, subagent_id, target_id, tab_id)
    2. Harness request metadata (_meta: claudecode/toolUseId, agent_id, sessionId, taskId)
    3. MCP session / client identity (FastMCP ServerSession or client_id)
    4. Harness process environment (KAHIN_AGENT_ID, CLAUDE_CODE_AGENT, CODEX_SESSION_ID, etc.)
    """
    args = arguments or {}
    explicit_agent = (
        args.get("agent_id")
        or args.get("agentId")
        or args.get("subagent_id")
        or args.get("subagentId")
        or args.get("caller_id")
        or args.get("callerId")
    )
    explicit_target = (
        args.get("target_id")
        or args.get("targetId")
        or args.get("tab_id")
        or args.get("tabId")
    )

    caller_id: str | None = str(explicit_agent) if explicit_agent else None
    target_id: str | None = str(explicit_target) if explicit_target else None

    # Check FastMCP / MCP SDK context
    meta_agent: str | None = None
    session_id: str | None = None
    if context is not None:
        # FastMCP Context object
        req_ctx = getattr(context, "request_context", None)
        if req_ctx is not None:
            # Metadata dict in request
            # Note: per-tool-call IDs (like claudecode/toolUseId) change on every
            # turn and must NOT be used as caller/agent identity.
            meta = getattr(req_ctx, "meta", None)
            if isinstance(meta, dict):
                meta_agent = (
                    meta.get("agent_id")
                    or meta.get("agentId")
                    or meta.get("subagent_id")
                    or meta.get("subagentId")
                    or meta.get("caller_id")
                    or meta.get("callerId")
                    or meta.get("taskId")
                    or meta.get("task_id")
                    or meta.get("sessionId")
                    or meta.get("session_id")
                )
            if meta_agent is None and hasattr(req_ctx, "request"):
                req = getattr(req_ctx, "request", None)
                params = getattr(req, "params", None)
                if params is not None and hasattr(params, "meta"):
                    p_meta = getattr(params, "meta", None)
                    if isinstance(p_meta, dict):
                        meta_agent = (
                            p_meta.get("agent_id")
                            or p_meta.get("subagent_id")
                            or p_meta.get("sessionId")
                            or p_meta.get("taskId")
                        )

            # ServerSession identity
            sess = getattr(req_ctx, "session", None)
            if sess is not None:
                sess_id = getattr(sess, "session_id", None)
                if isinstance(sess_id, str) and sess_id:
                    session_id = sess_id
                else:
                    session_id = f"mcp-session-{id(sess)}"

        # Direct session_id or client_id on Context (FastMCP HTTP/SSE)
        if not session_id and getattr(context, "session_id", None):
            session_id = str(getattr(context, "session_id"))
        elif not session_id and getattr(context, "client_id", None):
            session_id = str(getattr(context, "client_id"))

    # Environment variables set by AI harnesses
    env_agent = (
        os.environ.get("KAHIN_AGENT_ID")
        or os.environ.get("CLAUDE_CODE_AGENT")
        or os.environ.get("SUBAGENT_ID")
        or os.environ.get("AGENT_ID")
        or os.environ.get("CODEX_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("OPENCODE_SESSION_ID")
    )

    resolved_caller = (
        caller_id
        or (str(meta_agent) if meta_agent else None)
        or (str(env_agent) if env_agent else None)
        or session_id
    )

    return (resolved_caller, target_id)
