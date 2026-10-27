"""Offline subprocess fixture for T05 fault/protocol acceptance.

MES context/history use captured fixtures; preview/completion replies are
synthetic. All replies use HTTPX MockTransport. Default HTTP transports are
disabled, including for the MCP stdio server. This is never a live MES check.
"""
import asyncio
import copy
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from core import http_client
from tools import component_issue_plans as plans, component_issue_receipts as receipts, mcp
from tools import component_issue_bulk as bulk, component_issue_bulk_receipts as bulk_receipts

SHOPFLOOR = 'https://camstar.invalid/Shopfloor'
QUERY = 'https://camstar.invalid/Query'
DATA = ROOT / 'docs/t05-fixtures'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def echo(value):
    """Translate request casing for a deterministic serialization-only reply."""
    if isinstance(value, list):
        return [echo(item) for item in value]
    if isinstance(value, dict):
        names = {'bomLineItem': 'BOMLineItem', 'uom': 'UOM'}
        return {names.get(key, key[0].upper() + key[1:]): echo(item) for key, item in value.items()}
    return value


class FaultHarness:
    def __init__(self, directory):
        self.directory = Path(directory)
        control = self.directory/'control.json'
        self.mode = read_json(control).get('mode', 5) if control.exists() else 5
        if self.mode == 2:
            with sqlite3.connect(self.database) as db:
                self.snapshot = json.loads(db.execute('SELECT payload FROM plans ORDER BY created LIMIT 1').fetchone()[0])['snapshot']
            proof = read_json(DATA/'bulk-derived-receipt-F.json')
        else:
            self.snapshot = copy.deepcopy(read_json(DATA / 'formal-execution-D.json')['persisted_plan']['plan']['snapshot'])
            self.snapshot.pop('marker')
            self.snapshot['environment'] = SHOPFLOOR
            proof = read_json(DATA/'issue-receipt-verification-D.json')
        self.rows = {item['table']: json.loads(item['response']) for item in proof['reads']}
        self.sent = False
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.transport))

    @property
    def database(self):
        return self.directory / 'fault-plans.sqlite3'

    def control(self):
        return read_json(self.directory / 'control.json')

    def event(self, **value):
        # Each scenario has at most one executing child; readers do not log here.
        with (self.directory / 'transport.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value) + '\n')
            stream.flush()

    async def status(self, *args, **kwargs):
        source = self.mode == 2 and args[0] == self.snapshot['arguments']['source_container_name']
        return json.dumps({'complete': True, 'data': {'ContainerStatusDetails': self.snapshot['source_context' if source else 'context']}})

    async def requirement(self, *args, **kwargs):
        if self.sent and self.control().get('execute') == 'crash_after_response':
            self.event(stage='response_delivered_before_final_save')
            os._exit(23)
        data = {'status': 'matched', 'complete': True, 'data': {
            'ParentContainer': self.snapshot['context']['Container'],
            'IssueDetails': self.snapshot['constraints']}}
        if self.mode == 2:
            data['data']['ObjectName'] = self.snapshot['arguments']['source_container_name']
        return json.dumps(data)

    async def history(self, *args, **kwargs):
        return '{"complete":true,"rows":[]}'

    async def transport(self, request):
        assert str(request.url).startswith((SHOPFLOOR + '/', QUERY + '/'))
        assert request.method == 'POST'
        body = json.loads(request.content)
        if request.url.path == '/Shopfloor/api/ComponentIssue':
            if request.url.params['execute'] == 'false':
                response = echo(body)
                if self.mode == 2:
                    response['ServiceDetails'][0]['Actuals'][0]['CDOTypeId'] = '6540'
                return httpx.Response(200, json=response)
            assert request.url.params['execute'] == 'true'
            self.sent = True
            self.event(stage='execute_accepted', marker=body['comments'],
                       qty=body['serviceDetails'][0]['actualQtyIssued'])
            while self.control().get('execute') == 'hold':
                await asyncio.sleep(0.02)
            if self.control().get('execute') == 'disconnect':
                raise httpx.RemoteProtocolError('Injected response loss after fixture commit')
            return httpx.Response(200, json={'CompletionMsg': 'OFFLINE fixture accepted'})
        assert request.url.path == '/Query/api/AdHoc'
        query = body['queryText']
        if self.mode == 2 and 'FROM Container WHERE' in query:
            return httpx.Response(200, json=[] if 'WHERE ParentContainerId=' in query else [self.snapshot['source_balance']])
        table = next(table for table in self.rows if 'FROM ' + table + ' WHERE' in query)
        self.event(stage='receipt_read', table=table)
        mode = self.control().get('receipt', 'full')
        if mode == 'permission':
            return httpx.Response(403, json={'error': 'Injected permission denial'})
        rows = copy.deepcopy(self.rows[table])
        if table == 'HistoryMainline':
            rows[0]['Comments'] = query.split("AND Comments='")[1].split("'")[0]
        if mode == 'empty' or mode == 'late_actual' and table == 'IssueActualsHistory':
            rows = []
        elif mode == 'duplicate':
            rows *= 2
        return httpx.Response(200, json=rows)

    def install(self, patch):
        def blocked(*args, **kwargs):
            raise AssertionError('Fault harness forbids real HTTPX network transport')

        async def async_blocked(*args, **kwargs):
            return blocked(*args, **kwargs)

        patch.setattr(httpx.HTTPTransport, 'handle_request', blocked)
        patch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', async_blocked)
        patch.setattr(http_client, '_client', self.client)
        patch.setattr(http_client, 'get_headers', lambda: {})
        patch.setattr(plans, 'PLAN_DB', self.database)
        patch.setattr(plans, 'CAMSTAR_SHOPFLOOR_BASE_URL', SHOPFLOOR)
        patch.setattr(receipts, 'CAMSTAR_QUERY_BASE_URL', QUERY)
        patch.setattr(plans, 'get_container_details', self.status)
        patch.setattr(plans, 'get_component_requirement', self.requirement)
        patch.setattr(plans, 'get_component_issue_history', self.history)
        patch.setattr(plans, 'get_container_history', self.history)
        if self.mode == 2:
            patch.setattr(bulk, 'get_container_details', self.status)
            patch.setattr(bulk, 'get_component_requirement', self.requirement)
            patch.setattr(bulk, 'CAMSTAR_QUERY_BASE_URL', QUERY)
            patch.setattr(bulk, 'CAMSTAR_SHOPFLOOR_BASE_URL', SHOPFLOOR)
            patch.setattr(bulk_receipts, 'CAMSTAR_QUERY_BASE_URL', QUERY)
            patch.setattr(bulk_receipts, 'CAMSTAR_SHOPFLOOR_BASE_URL', SHOPFLOOR)


def main():
    # pytest's patch manager restores parent-process installs; this child exits.
    from unittest.mock import patch
    from contextlib import ExitStack

    class Patches:
        def setattr(self, target, name, value):
            stack.enter_context(patch.object(target, name, value))

    with ExitStack() as stack:
        harness = FaultHarness(sys.argv[1])
        harness.install(Patches())
        if sys.argv[2] == 'stdio':
            mcp.run(transport='stdio', show_banner=False)
        else:
            result = asyncio.run(plans.issue_components(sys.argv[2], sys.argv[3]))
            print(result, flush=True)


if __name__ == '__main__':
    main()
