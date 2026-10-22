"""Repeatable read-only evaluation. LLM tool calls are recorded, never dispatched."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.tool_routing import ROUTING_INSTRUCTION, select_tools, selection_metrics
from core.http_client import close_client
from core.tool_catalog import TOOL_POLICIES
from scripts.benchmark_queries import CASES
from tools import mcp


# Conversation fixtures describe user intent only; no fabricated business answers.
DIALOGUES = [
    ("long_followup", ["查询容器 BOX-0001", "当前状态呢", "再查历史", "继续", "再查历史"],
     "get_container_history", {"product", "excel"}),
    ("switch_to_order", ["创建一个产品", "查询工单 MO-100076435 的现场容器进度"],
     "get_mfgorder_progress", {"product", "excel"}),
    ("switch_to_product", ["追溯容器 BOX-0001", "查询产品 InstanceID 00062c8000000002 的完整工艺链"],
     "get_product_process", {"container", "excel"}),
    ("read_rework", ["创建一个工单", "查询容器 BOX-0001 的返工关联历史"],
     "get_container_trace", {"mfgorder", "excel"}),
]


def messages_for(prompts):
    messages = []
    for index, prompt in enumerate(prompts):
        messages.append({"role": "user", "content": prompt})
        if index < len(prompts) - 1:
            # Synthetic turn boundary, no invented business data or write outcome.
            # Consecutive user-only messages would represent queued unanswered
            # requests, not a user switching topics after a completed turn.
            messages.append({"role": "assistant", "content": "本轮对话结束。"})
    return messages


def routing_check(tools, case):
    name, prompts, expected, forbidden = case
    selected = select_tools(tools, messages_for(prompts))
    names = [t["function"]["name"] for t in selected]
    unexpected = [n for n in names if TOOL_POLICIES[n].group in forbidden
                  or TOOL_POLICIES[n].effect != "read"]
    return {"case": name, "prompts": prompts, "expected": expected,
            "passed": expected in names and not unexpected,
            "unexpected_tools": unexpected, **selection_metrics(selected, len(tools))}


def summarize(samples):
    durations = sorted(s["seconds"] for s in samples)
    return {"count": len(samples), "success_count": sum(s["success"] for s in samples),
            "median_seconds": round(statistics.median(durations), 4) if durations else None,
            "p95_seconds": durations[math.ceil(.95 * len(durations)) - 1] if durations else None,
            "p95_method": "nearest-rank; small samples are descriptive only"}


def scoped_success(name, result):
    if result.get("error"):
        return False
    if name == "trace":
        sections = result.get("sections", {})
        return len(sections) == 6 and all(v.get("page_complete") for v in sections.values())
    if name == "container":
        return bool(result.get("data", {}).get("ContainerStatusDetails"))
    if name == "product":
        return result.get("complete") is True
    containers = result.get("containers", [])
    return bool(containers) and all(not c.get("error") and not c.get("recent_history", {}).get("error")
                                    for c in containers)


async def measure_read(name, read):
    started = time.perf_counter()
    try:
        result = json.loads(await asyncio.wait_for(read(), timeout=70))
        sample = {"success": scoped_success(name, result),
                  "scope": {k: result[k] for k in ("complete", "requested_sections_complete", "page") if k in result},
                  "has_error": bool(result.get("error"))}
    except Exception as exc:
        sample = {"success": False, "error_type": type(exc).__name__}
    return {**sample, "seconds": round(time.perf_counter() - started, 4)}


async def probe_model(messages, candidates, expected):
    from agent.llm_client import oai_client
    from config import LLM_MODEL
    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(oai_client.chat.completions.create(
            model=LLM_MODEL, messages=[{"role": "system", "content": ROUTING_INSTRUCTION}] + messages,
            tools=candidates, tool_choice="auto", max_tokens=1024), timeout=50)
        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []
        calls = [c.function.name for c in tool_calls]
        arguments_valid = True
        for call in tool_calls:
            try:
                if not isinstance(json.loads(call.function.arguments), dict):
                    arguments_valid = False
            except (TypeError, ValueError):
                arguments_valid = False
        success = bool(calls) and all(c == expected for c in calls) and arguments_valid and choice.finish_reason != "length"
        result = {"success": success, "chosen_tools": calls, "arguments_json_valid": arguments_valid,
                  "finish_reason": choice.finish_reason,
                  "usage": response.usage.model_dump() if response.usage else None}
    except Exception as exc:
        result = {"success": False, "error_type": type(exc).__name__, "usage": None}
    return {**result, "seconds": round(time.perf_counter() - started, 4)}


async def run(rounds, live, llm, output):
    tools = [{"type": "function", "function": {"name": t.name, "description": t.description,
              "parameters": t.parameters}} for t in await mcp.list_tools()]
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "rounds": rounds,
              "method": {"service": "fixed read-only calls; scoped success is not full-history completeness",
                         "connection": "new_connection closes local HTTP pool; reused_connection follows immediately; server caches uncontrolled",
                         "model": "synthetic completed-turn boundaries, no business answers; intent replay, not end-to-end dialogue; selected/all order alternates; tool calls never executed",
                         "cache": "provider usage retained when returned; no claim to force cold/warm model or database caches"},
              "routing": [routing_check(tools, c) for c in DIALOGUES], "services": [], "model": []}

    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    try:
        if live:
            for name, _, _, read in CASES:
                entry = {"case": name, "samples": []}
                report["services"].append(entry)
                for index in range(rounds):
                    await close_client()
                    for mode in ("new_connection", "reused_connection"):
                        entry["samples"].append({"round": index + 1, "mode": mode, **await measure_read(name, read)})
                        save()
                entry["summary"] = {mode: summarize([s for s in entry["samples"] if s["mode"] == mode])
                                    for mode in ("new_connection", "reused_connection")}
                print("service", name, entry["summary"], flush=True)
                save()
        if llm:
            for name, prompts, expected, _ in DIALOGUES:
                messages = messages_for(prompts)
                selected = select_tools(tools, messages)
                entry = {"case": name, "samples": []}
                report["model"].append(entry)
                for index in range(rounds):
                    modes = [("selected", selected), ("all", tools)]
                    if index % 2:
                        modes.reverse()
                    for mode, candidates in modes:
                        entry["samples"].append({"round": index + 1, "mode": mode,
                                                 **await probe_model(messages, candidates, expected)})
                        save()
                entry["summary"] = {mode: summarize([s for s in entry["samples"] if s["mode"] == mode])
                                    for mode in ("selected", "all")}
                print("model", name, entry["summary"], flush=True)
                save()
    finally:
        await close_client()
        save()
    return report


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, choices=range(2, 11), default=3)
    parser.add_argument("--live", action="store_true", help="Run fixed read-only service probes")
    parser.add_argument("--llm", action="store_true", help="Make paid model selection probes; never execute returned calls")
    parser.add_argument("--output", type=Path, default=Path("docs/query-evaluation.json"))
    args = parser.parse_args()
    result = asyncio.run(run(args.rounds, args.live, args.llm, args.output))
    failures = any(not c["passed"] for c in result["routing"]) or any(
        not s["success"] for category in ("services", "model") for c in result[category] for s in c["samples"])
    sys.exit(1 if failures else 0)
