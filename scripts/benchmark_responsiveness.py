"""Compare a Git baseline with current response paths; live calls are opt-in.

No business writes. Capability live mode allows only the local directory tool;
order live mode runs the fixed read-only progress inquiry. No service restart.
"""
import argparse
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import logging
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def git_source(ref, path):
    return subprocess.run(['git', 'show', f'{ref}:{path}'], cwd=ROOT, check=True,
        capture_output=True, encoding='utf-8').stdout


def baseline_function(source, name, namespace):
    node = next(n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []  # Do not register baseline MCP tools a second time.
    scope = dict(namespace)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), '<git-baseline>', 'exec'), scope)
    return scope[name]


def summary(values):
    return {'samples': len(values), 'median_ms': statistics.median(values), 'min_ms': min(values), 'max_ms': max(values)}


async def run(args):
    logging.disable(logging.CRITICAL)
    from agent import context_budget, langgraph_runtime as runtime
    from agent.capability_query import render_capability_overview
    from agent.prompts import SYSTEM_PROMPT
    from tools import mcp, get_tool_func, order_progress
    from tools.tool_discovery import get_capabilities
    from core.http_client import close_client
    question = '你具备哪些能力？请根据实际能力目录简要介绍，并说明尚不支持的内容。'
    ref = subprocess.run(['git', 'rev-parse', args.baseline_ref], cwd=ROOT, capture_output=True,
        check=True, encoding='utf-8').stdout.strip()
    report = {'baseline_commit': ref, 'method': 'same local inputs; live baseline/current alternate, CPU samples run consecutively per mode including warmup; provider and server caches uncontrolled; samples do not establish a production SLA',
        'mes_write_performed': False, 'results': {}}
    baseline_context = {'__name__': 'baseline_context'}
    exec(compile(git_source(ref, 'agent/context_budget.py'), '<baseline-context>', 'exec'), baseline_context)
    history = json.loads(args.history_file.read_text(encoding='utf-8'))['messages']
    local = {}
    for mode, fn in [('before', baseline_context['budget_history']), ('after', context_budget.budget_history)]:
        elapsed = []
        for _ in range(5):
            start = time.perf_counter(); _, metrics = fn(history, budget=0)
            elapsed.append((time.perf_counter()-start)*1000)
        local[mode] = {**summary(elapsed), 'history_tokens': metrics['history_estimated_tokens'],
                       'removed_messages': metrics['history_removed_messages']}
    report['results']['history_preparation'] = local
    raw = await get_capabilities()
    start = time.perf_counter(); reply = render_capability_overview(question, raw)
    report['results']['capability_renderer'] = {'milliseconds': (time.perf_counter()-start)*1000,
        'model_calls': 0, 'reply_chars': len(reply or '')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    if args.live_capability:
        from agent.llm_client import oai_client
        from config import LLM_MODEL
        tools = [{'type': 'function', 'function': {'name': t.name, 'description': t.description, 'parameters': t.parameters}}
                 for t in await mcp.list_tools()]
        report['model'] = LLM_MODEL
        samples = {'before': [], 'after': []}
        model_calls = 0
        async def create(**kwargs):
            nonlocal model_calls
            model_calls += 1
            assert kwargs.get('tool_choice') in ('auto', 'none')
            return await oai_client.chat.completions.create(**kwargs, max_tokens=8192)
        async def local_directory(**kwargs):
            return await get_capabilities(**kwargs)
        def lookup(name):
            if name != 'get_capabilities':
                raise RuntimeError('Only local capability discovery is permitted in this benchmark')
            return local_directory
        try:
            with patch.object(runtime, '_client', SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))), \
                 patch.object(runtime, '_openai_tools', tools), patch.object(runtime, 'get_tool_func', lookup), \
                 patch.object(runtime, 'get_stream_writer', lambda: lambda _: None), \
                 patch.object(runtime, 'record_perf', lambda *a, **k: None), \
                 patch.object(runtime, 'build_runtime_experience_context', lambda *a: ''), \
                 patch.object(runtime, 'record_tool_outcome', lambda **k: None):
                before = baseline_function(git_source(ref, 'agent/langgraph_runtime.py'), '_agent_node', runtime.__dict__)
                for round_index in range(args.rounds):
                    for mode in (['before', 'after'] if round_index % 2 == 0 else ['after', 'before']):
                        state = {'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': question}],
                                 'request_message': question, 'username': 'performance-diagnostic', 'session_id': 'isolated',
                                 'loop_count': 0, 'capability_directory': ''}
                        calls_before = model_calls; start = time.perf_counter()
                        for _ in range(4):
                            state.update(await (before if mode == 'before' else runtime._agent_node)(state))
                            if not state.get('pending_tool_calls'):
                                break
                            while state.get('pending_tool_calls'):
                                state.update(await runtime._execute_tool_node(state))
                        samples[mode].append({'milliseconds': (time.perf_counter()-start)*1000,
                            'model_calls': model_calls-calls_before, 'completed': state.get('stop_reason') == 'complete',
                            'reply': state.get('final_reply', '')})
                        report['results']['capability_live'] = samples; save()
        finally:
            await oai_client.close()
    if args.live_order:
        before = baseline_function(git_source(ref, 'tools/order_progress.py'), 'get_mfgorder_progress', order_progress.__dict__)
        samples = {'before': [], 'after': []}
        try:
            for round_index in range(args.rounds):
                for mode in (['before', 'after'] if round_index % 2 == 0 else ['after', 'before']):
                    start = time.perf_counter()
                    raw = await (before if mode == 'before' else order_progress.get_mfgorder_progress)(
                        args.order_name, limit=args.order_limit, include_history=True)
                    data = json.loads(raw)
                    samples[mode].append({'milliseconds': (time.perf_counter()-start)*1000, 'error': data.get('error'),
                        'scope': data.get('page'), 'containers': [
                            {'name': row['name'], 'error': row.get('error'), 'qty': row.get('current', {}).get('Qty'),
                             'status': row.get('current', {}).get('StatusName'), 'order_completed': row.get('state_evidence', {}).get('production_completed')}
                            for row in data.get('containers', [])]})
                    report['results']['order_live'] = samples; save()
        finally:
            await close_client()
    for key in ('capability_live', 'order_live'):
        if key in report['results']:
            report['results'][key]['summary'] = {mode: summary([r['milliseconds'] for r in report['results'][key][mode]])
                                                for mode in ('before', 'after')}
    report['source_hashes'] = {path: hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in (
        'agent/capability_query.py', 'agent/context_budget.py', 'agent/langgraph_runtime.py', 'tools/order_progress.py')}
    save()
    print(json.dumps({key: value.get('summary', value) for key, value in report['results'].items()}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='HEAD')
    parser.add_argument('--history-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'scratch/response-performance.json')
    parser.add_argument('--live-capability', action='store_true')
    parser.add_argument('--live-order', action='store_true')
    parser.add_argument('--order-name', default='MO-100076435')
    parser.add_argument('--order-limit', type=int, choices=range(1, 21), default=10)
    parser.add_argument('--rounds', type=int, choices=range(1, 4), default=2)
    asyncio.run(run(parser.parse_args()))
