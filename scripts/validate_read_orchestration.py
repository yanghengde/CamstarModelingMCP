"""Explicit read-only live acceptance; never starts/stops the VS Code server.

New source is exercised directly. The existing MCP process is separately checked
for its advertised inventory; an old process is reported, never hot-patched.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import read_catalog as c
from core.http_client import close_client
from tools.read_orchestration import get_camstar_read_metadata, run_camstar_read_template

NAMES = ['CDX-P1-20261108-END2', 'CDX-P1-20261108-COMPLETE', 'CDX-P1-20261108-REPORT',
         'CDX-C4-20261104-TMO', 'CDX-C4-20261104-QMO']
NEW_TOOLS = {'search_camstar_read_tools', 'get_camstar_read_metadata',
             'execute_camstar_read_plan', 'run_camstar_read_template'}


async def runtime_check(url):
    import httpx
    from config import MCP_API_KEY
    headers = {'Authorization': 'Bearer ' + MCP_API_KEY,
               'Accept': 'application/json, text/event-stream'}
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        async def rpc(method, params, index):
            response = await client.post(url, headers=headers,
                json={'jsonrpc': '2.0', 'id': index, 'method': method, 'params': params})
            response.raise_for_status()
            value = response.json()
            if value.get('error'):
                raise ValueError('Existing MCP endpoint rejected ' + method)
            return value['result']
        init = await rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'camstar-read-acceptance', 'version': '1'}}, 1)
        headers['MCP-Protocol-Version'] = init['protocolVersion']
        await client.post(url, headers=headers, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        tools = await rpc('tools/list', {}, 2)
        schemas = {t['name']: t.get('inputSchema', {}) for t in tools['tools']}
        names = set(schemas); cursor = tools.get('nextCursor')
        while cursor:
            tools = await rpc('tools/list', {'cursor': cursor}, 3)
            names.update(t['name'] for t in tools['tools']); cursor = tools.get('nextCursor')
            schemas.update({t['name']: t.get('inputSchema', {}) for t in tools['tools']})
        result = {'reachable': True, 'tools_count': len(names), 'new_tools_loaded': sorted(names & NEW_TOOLS),
                  'requires_user_process_reload': not NEW_TOOLS <= names, 'process_modified': False}
        result['query_guidance_loaded'] = 'execute_adhoc_query是兜底' in init.get('instructions', '')
        result['typed_template_parameters'] = 'order_names' in json.dumps(schemas.get('run_camstar_read_template', {}))
        result['requires_user_process_reload'] = (result['requires_user_process_reload']
            or not result['query_guidance_loaded'] or not result['typed_template_parameters'])
        if NEW_TOOLS <= names:
            reply = await rpc('tools/call', {'name': 'run_camstar_read_template',
                'arguments': {'template': 'order_dates', 'parameters': {'order_names': NAMES}}}, 4)
            if reply.get('isError'):
                raise ValueError('MCP template invocation failed')
            text = next(x['text'] for x in reply['content'] if x['type'] == 'text')
            output = json.loads(text)
            if not output.get('complete'):
                raise ValueError('Runtime template returned incomplete evidence')
            result['native_template_verified'] = True
        return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mcp-url', default='http://127.0.0.1:8030/mcp/')
    parser.add_argument('--output', default='tmp/read-orchestration-live.json')
    args = parser.parse_args()
    report = {'production_writes': 0, 'server_restarted': False}
    try:
        # A forced contract refresh is explicit and never caches production rows.
        report['metadata'] = {}
        for name in ('orders', 'containers', 'current_status', 'history'):
            data = json.loads(await get_camstar_read_metadata('dataset', name, refresh=True))
            if not data.get('complete'):
                raise ValueError('Live metadata unavailable: ' + str(data.get('error')))
            report['metadata'][name] = data['metadata']
        entity = json.loads(await get_camstar_read_metadata('entity', 'Products', refresh=True))
        if not entity.get('complete'):
            raise ValueError('Live Modeling Swagger metadata unavailable')
        report['modeling_entity_metadata_verified'] = True
        outputs = []
        for _ in range(2):
            started = time.perf_counter()
            output = json.loads(await run_camstar_read_template('order_dates', {'order_names': NAMES}))
            if not output.get('complete') or len(output.get('rows', [])) != 5:
                raise ValueError('Live template incomplete: ' + json.dumps(output, ensure_ascii=False)[:3000])
            outputs.append({'wall_ms': round((time.perf_counter() - started) * 1000, 2), 'output': output})
        if outputs[0]['output']['rows'] != outputs[1]['output']['rows']:
            raise ValueError('Production rows changed between reads; do not compare performance as equivalent results')
        tmo = next(r for r in outputs[0]['output']['rows'] if r['order_name'].endswith('-TMO'))
        if tmo['container_count'] != 1 or tmo['actual_first_container_start']['display'] != '2026-11-04T22:50:48.337+08:00':
            raise ValueError('Closed-container date regression')
        report.update(source_template_verified=True, repeated_reads=outputs)
        try:
            report['existing_runtime'] = await runtime_check(args.mcp_url)
        except Exception as exc:
            report['existing_runtime'] = {'reachable': False, 'error': str(exc)[:500], 'process_modified': False}
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'source_template_verified': True,
            'tmo_actual_start': tmo['actual_first_container_start']['display'],
            'wall_ms': [o['wall_ms'] for o in outputs], 'existing_runtime': report['existing_runtime'],
            'production_writes': 0}, ensure_ascii=True))
    finally:
        await close_client()


if __name__ == '__main__':
    asyncio.run(main())
