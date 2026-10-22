"""The workspace reads durable approvals without running or leaking tool state."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent import langgraph_runtime as runtime


def test_unknown_session_does_not_read_another_checkpoint(monkeypatch):
    graph = SimpleNamespace(aget_state=AsyncMock())
    monkeypatch.setattr(runtime, "_graph", graph)
    monkeypatch.setattr(runtime, "user_memories", {})
    assert asyncio.run(runtime.get_workflow_status("user", "missing"))["status"] == "idle"
    graph.aget_state.assert_not_called()


def test_pending_approval_is_recovered_without_tool_arguments(monkeypatch):
    approval = {"type": "approval_required", "message": "请确认", "counts": {"deletes": 1}}
    snapshot = SimpleNamespace(
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value=approval)])],
        values={"messages": ["private prompt"], "pending_tool_calls": ["private arguments"]},
        next=("policy",),
    )
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot))
    monkeypatch.setattr(runtime, "_graph", graph)
    monkeypatch.setattr(runtime, "user_memories", {"user": {"sessions": {"one": {}}}})
    result = asyncio.run(runtime.get_workflow_status("user", "one"))
    assert result == {"engine": "langgraph", "status": "approval_required", "approval": approval}
    assert graph.aget_state.call_args.args[0]["configurable"]["thread_id"] == "user:one"


def test_pending_checkpoint_is_not_reported_as_running(monkeypatch):
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(
        tasks=[], values={"stop_reason": "complete"}, next=("agent",),
    )))
    monkeypatch.setattr(runtime, "_graph", graph)
    monkeypatch.setattr(runtime, "user_memories", {"user": {"sessions": {"one": {}}}})
    assert asyncio.run(runtime.get_workflow_status("user", "one"))["status"] == "incomplete"
