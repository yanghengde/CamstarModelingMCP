import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from agent.safety import plan_fingerprint
from core.inference_metrics import InferenceMetrics


def test_plan_identity_ignores_json_whitespace_but_not_targets():
    def call(args): return [{"function": {"name": "delete_spec", "arguments": args}}]
    assert plan_fingerprint(call('{"key":"a"}')) == plan_fingerprint(call('{ "key": "a" }'))
    assert plan_fingerprint(call('{"key":"a"}')) != plan_fingerprint(call('{"key":"b"}'))


def test_old_approval_does_not_approve_new_plan(monkeypatch):
    from agent import langgraph_runtime as runtime
    pending = [{"function": {"name": "rebuild_workflow_route", "arguments": "{}"}}]
    observed = []
    monkeypatch.setattr(runtime, "interrupt", lambda payload: observed.append(payload) or {"approved": True})
    result = runtime._policy_node({"pending_tool_calls": pending, "approval_granted": True, "approved_plan": "old"})
    assert observed[0]["plan_id"] == plan_fingerprint(pending)
    assert result["approved_plan"] == plan_fingerprint(pending)


def test_usage_only_final_chunk_is_captured():
    metrics = InferenceMetrics()
    assert metrics.result()["usage"] is None
    metrics.observe(SimpleNamespace(usage={"prompt_tokens": 12}, choices=[]))
    assert metrics.result()["usage"]["prompt_tokens"] == 12
    assert metrics.result()["first_delta_seconds"] is None


def test_unverified_patch_never_reaches_server(monkeypatch):
    from tools import mfgorders, mfglines, producttypes
    for module, name in [(mfgorders, "patch_mfgorder"), (mfglines, "patch_mfgline"), (producttypes, "patch_producttype")]:
        request = AsyncMock()
        monkeypatch.setattr(module, "request", request)
        assert asyncio.run(getattr(module, name)('{}')).startswith('Error:')
        request.assert_not_called()
