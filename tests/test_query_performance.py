"""Regression coverage for query failure budgets and title latency."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent import langgraph_runtime as runtime
from agent import llm_client


def test_title_does_not_call_model(monkeypatch):
    create = AsyncMock(side_effect=AssertionError("title must not call LLM"))
    monkeypatch.setattr(llm_client.oai_client.chat.completions, "create", create)
    assert asyncio.run(llm_client.generate_title("query workflow")) == "query workflow"
    create.assert_not_awaited()


def test_failed_reads_stop_pending_reads(monkeypatch):
    tool = AsyncMock(return_value="Error: backend unavailable")
    monkeypatch.setattr(runtime, "get_tool_func", lambda _: tool)
    monkeypatch.setattr(runtime, "get_stream_writer", lambda: lambda _: None)
    monkeypatch.setattr(runtime, "record_perf", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "record_tool_outcome", lambda **k: None)
    monkeypatch.setattr(runtime, "MAX_READ_FAILURES", 2)
    state = {"messages": [], "pending_tool_calls": [
        {"id": str(i), "function": {"name": "list_workflows", "arguments": "{}"}}
        for i in range(3)
    ]}
    async def scenario():
        for _ in range(3):
            state.update(await runtime._execute_tool_node(state))
    asyncio.run(scenario())
    assert tool.await_count == 2
    assert len(state["messages"]) == 3
    assert "failure limit" in state["messages"][-1]["content"]


def test_failure_budget_requests_summary_without_tools(monkeypatch):
    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content="Partial results; query failed.", tool_calls=None))])
    create = AsyncMock(return_value=chunks())
    monkeypatch.setattr(runtime, "_client", SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(runtime, "get_stream_writer", lambda: lambda _: None)
    monkeypatch.setattr(runtime, "record_perf", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "build_runtime_experience_context", lambda *a: "")
    result = asyncio.run(runtime._agent_node({
        "messages": [{"role": "user", "content": "query workflows"}],
        "read_failures": runtime.MAX_READ_FAILURES,
    }))
    assert create.call_args.kwargs["tool_choice"] == "none"
    assert not result["pending_tool_calls"]
    assert result["final_reply"]