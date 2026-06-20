"""Owner: M2. Stream graph execution as SSE RunEvents.

Checkpointer lifecycle
─────────────────────
A module-level async initialiser creates the AsyncPostgresSaver once and reuses
it for every request (connection pool lives for the process lifetime).  If the
DB is unavailable (e.g. dev without Postgres) the graph runs without a
checkpointer — no resume, but everything else works.

Resume flow
───────────
Pass `resume=True` when the user returns after an interrupt (upload/answer).
The graph reads the LangGraph checkpoint for `thread_id=case_id` and continues
from the interrupted node; no initial state is re-supplied.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from app.graph.builder import build_graph
from app.graph.state import GraphState
from app.schemas.run import RunEvent

# ── Checkpointer singleton ────────────────────────────────────────────────────

_checkpointer = None
_cp_pool = None            # kept alive for the process lifetime (see below)
_cp_lock = asyncio.Lock()
_cp_tried = False          # avoid retrying a failed init on every request


async def _ensure_checkpointer():
    """Initialise the AsyncPostgresSaver once; return None on failure.

    Owns a process-lifetime AsyncConnectionPool. The previous implementation used
    ``AsyncPostgresSaver.from_conn_string(...)`` and discarded the returned context
    manager, so its connection was closed when the local was garbage-collected —
    the run then died mid-stream with "the connection is closed". A pool stored in
    a module global stays open, survives dropped connections, and is safe across
    concurrent requests.
    """
    global _checkpointer, _cp_pool, _cp_tried
    if _checkpointer is not None or _cp_tried:
        return _checkpointer
    async with _cp_lock:
        if _checkpointer is not None or _cp_tried:
            return _checkpointer
        _cp_tried = True
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            from app.config import settings

            # psycopg3 needs a plain postgresql:// URL (not SQLAlchemy-prefixed).
            db_url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
            # AsyncPostgresSaver requires autocommit + dict_row; prepare_threshold=0
            # keeps it compatible with transaction-pooling proxies (Neon -pooler / PgBouncer).
            pool = AsyncConnectionPool(
                conninfo=db_url,
                max_size=10,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await pool.open(wait=True)
            checkpointer = AsyncPostgresSaver(pool)
            await checkpointer.setup()
            _cp_pool = pool
            _checkpointer = checkpointer
        except Exception:
            if _cp_pool is not None:
                await _cp_pool.close()
            _cp_pool = None
            _checkpointer = None
    return _checkpointer


# ── Graph singleton (one compiled graph per checkpointer state) ───────────────

_graph = None


async def _get_graph():
    global _graph
    if _graph is not None:
        return _graph
    checkpointer = await _ensure_checkpointer()
    _graph = build_graph(checkpointer)
    return _graph


# ── SSE streaming ─────────────────────────────────────────────────────────────

# Graph node names. "run_eligibility"/"run_checklist" carry a prefix because a
# LangGraph node name may not collide with a GraphState key (see builder.py); the
# prefix is stripped below so SSE consumers still see "eligibility"/"checklist".
_NODE_NAMES = frozenset(
    ["planner", "knowledge", "dependency", "run_eligibility", "run_checklist",
     "document", "form", "reminder"]
)


async def run_case(
    case_id: str,
    user_id: str,
    goal: str,
    language: str = "en",
    resume: bool = False,
) -> AsyncGenerator[str, None]:
    graph = await _get_graph()
    config = {"configurable": {"thread_id": case_id}}

    initial_state: GraphState | None = None
    if not resume:
        initial_state = {
            "case_id": case_id,
            "user_id": user_id,
            "goal": goal,
            "language": language,
            "logs": [],
            "messages": [],
        }

    try:
        async for chunk in graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            for node_name, node_output in chunk.items():
                if node_name not in _NODE_NAMES:
                    continue
                agent = node_name.removeprefix("run_")  # clean SSE name
                yield RunEvent(agent=agent, status="started").to_sse()
                yield RunEvent(
                    agent=agent,
                    status="completed",
                    payload=node_output if isinstance(node_output, dict) else {},
                ).to_sse()

        yield RunEvent(agent="system", status="completed", message="done").to_sse()

    except Exception as exc:
        yield RunEvent(agent="system", status="error", message=str(exc)).to_sse()
