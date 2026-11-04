"""Approved, resumable C4 isolated acceptance runner.

The user approved docs/c4-isolated-acceptance-plan.json in this chat. Writes
are limited to that manifest. Every mutation is journaled before submission.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.http_client import request, close_client
from tools.modeling_details import raw_json
from tools.process_context import ReadContext
from tools import quality as q, quality_plans as qp, quality_lots as ql
from config import CAMSTAR_SHOPFLOOR_BASE_URL
import uuid

PREFIX = 'CDX-C4-20261104'
PLAN = ROOT / 'docs/c4-isolated-acceptance-plan.json'
JOURNAL = ROOT / 'docs/quality-fixtures/c4-live-acceptance-journal.json'


def ref(suffix, revision=False):
    result = {'name': PREFIX + '-' + suffix}
    if revision:
        result['revision'] = '1'
    return result


class Runner:
    def __init__(self):
        self.plan = json.loads(PLAN.read_text(encoding='utf-8'))
        self.state = json.loads(JOURNAL.read_text(encoding='utf-8')) if JOURNAL.exists() else {
            'authorization': 'User approved the 29-object isolated acceptance plan in this chat',
            'objects': {}, 'attempts': [], 'scenarios': {}}

    def save(self):
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        JOURNAL.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    async def find(self, entity, name):
        data = await ReadContext(3).get(entity, params={'$filter': "Name eq '" + name + "'", '$top': 2})
        rows = data.get('value')
        if not isinstance(rows, list) or len(rows) > 1:
            raise ValueError('Ambiguous modeling identity: ' + entity + '/' + name)
        # Collection responses in this deployment omit scalar/reference values.
        # Resolve the identity first, then read the actual entity leaf.
        return await ReadContext(3).get(entity, rows[0]['InstanceID']) if rows else None

    async def create(self, entity, payload):
        name = payload['name']
        allowed = {(item['entity'], item['name']) for item in self.plan['modeling_objects']}
        if (entity, name) not in allowed:
            raise ValueError('Object outside approved manifest')
        key = entity + '/' + name
        old = await self.find(entity, name)
        if old:
            saved = self.state['objects'].get(key)
            if not saved or saved['payload'] != payload or saved.get('instance_id') != old.get('InstanceID'):
                raise ValueError('Existing object needs explicit reconciliation: ' + key)
            print('VERIFIED EXISTING', key, flush=True)
            return old
        if any(attempt.get('entity') == entity and attempt.get('payload', {}).get('name') == name
               and attempt['state'] in ('in_flight', 'unknown_object_exists', 'unknown_not_found')
               for attempt in self.state['attempts']):
            raise ValueError('Unresolved modeling create attempt: '+key)
        attempt = {'kind': 'modeling_create', 'entity': entity, 'payload': payload, 'state': 'in_flight'}
        self.state['attempts'].append(attempt)
        self.save()
        raw = await request('POST', '/api/' + entity, body=payload, response_formatter=raw_json)
        attempt['response'] = raw[:12000]
        current = await self.find(entity, name)
        if not current:
            attempt['state'] = 'not_found_after_attempt' if raw.startswith('❌ HTTP 400 ') else 'unknown_not_found'
            self.save()
            raise ValueError('Create did not produce the approved object: ' + key + '\n' + raw[:1800])
        if current.get('Revision') not in (None, payload.get('revision')):
            raise ValueError('Revision mismatch: ' + key)
        if raw.startswith('❌'):
            attempt['state'] = 'unknown_object_exists'
            self.save()
            raise ValueError('Create response failed although object exists: ' + key)
        attempt['state'] = 'created_and_read_back'
        self.state['objects'][key] = {'instance_id': current['InstanceID'], 'payload': payload, 'read_back': current}
        self.save()
        print('CREATED', key, current['InstanceID'], flush=True)
        return current

    async def modeling(self):
        def named(suffix, **fields):
            return dict(name=PREFIX + '-' + suffix, description='Approved isolated C4 acceptance', **fields)
        await self.create('ReworkReasons', named('RW'))
        await self.create('HoldReasons', named('HOLD'))
        await self.create('ReleaseReasons', named('RELEASE'))
        await self.create('LossReasonGroups', named('LG', entries=[{'name': 'SCRAP', 'instanceId': '00102c8000000002'}]))
        await self.create('ReworkReasonGroups', named('RG', entries=[ref('RW')]))
        for suffix in ('INSP', 'REPAIR'):
            await self.create('Operations', named(suffix, thruputReportingLevel={'name': 'BOX'},
                lossReasons=ref('LG'), reworkReasons=ref('RG')))
        await self.create('Specs', named('SI', revision='1', status=1, operation=ref('INSP'),
            validateLotSamplingComplete=False, validateSamplingOverallResult=False))
        await self.create('Specs', named('SR', revision='1', status=1, operation=ref('REPAIR'), allowReworkTo=True,
            validateLotSamplingComplete=False, validateSamplingOverallResult=False))
        # Persist the steps before binding cross-subentity references. The live
        # NamedSubentityRef contract requires an actual parent identity.
        steps = [dict(name='Inspect', **{'@odata.type': '#modeling.SpecStepChanges'}, spec=ref('SI', True),
            stepType=1, sequence=1),
            dict(name='Repair', **{'@odata.type': '#modeling.SpecStepChanges'}, spec=ref('SR', True),
                 stepType=1, sequence=2, isLastStep=True)]
        workflow = await self.create('Workflows', named('WF', revision='1', status=1, steps=steps))
        await self.workflow_graph(workflow)
        await self.create('SampleTests', named('TEST', revision='1', status=1, sampleType=1,
            aqlRejectReasons=ref('LG'), allowMoveOnFailure=False, scrapCountedRejectsByReason=False))
        await self.create('SamplingPlans', named('PLAN', revision='1', status=1, sampleRate=1,
            inspectionLevel={'name': 'IQC_DEMO_LVL', 'instanceId': '0020ef8000000002'},
            aqlLevel={'name': 'IQC_DEMO_AQL', 'instanceId': '0020ee8000000002'}, spec=ref('SI', True),
            sampleSizeBasedOnStartQty=False, samplingPlanDetails=[dict(sampleTest=ref('TEST', True), sampleRate=1,
                inspectAll=False, inspectAllContQty=False, inspectAllQtyOnFailure=False, spec=ref('SI', True), sequence=1)]))
        for suffix in ('P', 'Q', 'T'):
            body = named(suffix, revision='1', status=1, productType={'name': 'BOX'},
                         workflow=ref('WF', True), stdStartUOM={'name': 'EACH'})
            if suffix == 'P':
                body['samplingPlan'] = ref('PLAN', True)
            if suffix == 'Q':
                body['stdStartUOM2'] = {'name': 'UNIT'}
            await self.create('Products', body)
        for suffix, product, qty in [('MO', 'P', 6), ('QMO', 'Q', 10), ('TMO', 'T', 2)]:
            await self.create('MfgOrders', named(suffix, product=ref(product, True), qty=qty, uom={'name': 'EACH'}))

    async def workflow_graph(self, workflow):
        wid = workflow['InstanceID']
        leaf = await ReadContext(3).get('Workflows', wid, {'$expand': 'Steps'})
        ids = {s['Name']: s['InstanceID'] for s in leaf['Steps']}
        if set(ids) != {'Inspect', 'Repair'}:
            raise ValueError('Unexpected workflow step set')
        stepref = lambda name: {'instanceId': ids[name], 'name': name, 'parent': {'instanceId': wid}}
        body = {'firstStep': stepref('Inspect'), 'steps': [{
                '@odata.type': '#modeling.SpecStepChanges', 'name': 'Inspect',
                'objectToChange': stepref('Inspect'),
                'paths-Expanded': [{'listItemAction': 'add', 'value': {
                    '@odata.type': '#modeling.MovePathChanges', 'name': 'Next',
                    'toStep': stepref('Repair')}}],
                'reworkPaths-Expanded': [{'listItemAction': 'add', 'value': {
                    '@odata.type': '#modeling.ReworkPathChanges', 'name': 'C4Repair',
                    'toStep': stepref('Repair'), 'reEntryStep': stepref('Inspect'),
                    'endReworkStep': stepref('Repair')}}]}, {
                '@odata.type': '#modeling.SpecStepChanges', 'name': 'Repair',
                'objectToChange': stepref('Repair')}]}
        saved = self.state['scenarios'].get('workflow_graph')
        if saved:
            if saved.get('state') == 'verified':
                return
            if not (saved.get('state') == 'read_back_pending_verification'
                    and any(message in saved.get('response', '') for message in ('Step Changes', 'Not all array elements'))
                    and all(not s.get('Paths') and not s.get('ReworkPaths') for s in saved['read_back']['Steps'])):
                raise ValueError('Reconcile previous workflow graph attempt before resuming')
            self.state['attempts'].append(saved | {'state': 'confirmed_no_graph_change'})
        attempt = {'kind': 'workflow_graph', 'payload': body, 'state': 'in_flight'}
        self.state['scenarios']['workflow_graph'] = attempt
        self.save()

        raw = await request('PUT', '/api/Workflows/' + wid, body=body, response_formatter=raw_json)
        attempt['response'] = raw[:12000]
        check = await ReadContext(3).get('Workflows/' + wid, params={'$expand': 'Steps($expand=Paths,ReworkPaths)'})
        attempt['read_back'] = check
        attempt['state'] = 'read_back_pending_verification'
        self.save()
        if raw.startswith('❌'):
            raise ValueError('Workflow graph failed: ' + raw[:1800])
        items = check.get('Steps', [])
        inspect = next((s for s in items if s.get('Name') == 'Inspect'), {})
        if [p.get('Name') for p in inspect.get('Paths', [])] != ['Next'] or [p.get('Name') for p in inspect.get('ReworkPaths', [])] != ['C4Repair']:
            raise ValueError('Workflow graph not confirmed')
        attempt['state'] = 'verified'
        self.save()

    async def quality_submit(self, key, kind, arguments):
        """Native integration acceptance with the production builder/transport.

        Its dedicated journal never labels observations as human attestation or
        unlocks production plans. A submitted attempt is never automatically replayed.
        """
        if key in self.state['scenarios']:
            raise ValueError('Already attempted; inspect original acceptance journal: ' + key)
        marker = 'MCP-QUAL:' + uuid.uuid4().hex
        snap = await qp.build(kind, arguments, marker)
        route = qp.transport(snap)
        entry = {'kind': kind, 'snapshot': snap, 'route': route, 'state': 'in_flight'}
        self.state['scenarios'][key] = entry
        self.save()
        raw = await request(route['method'], route['path'], base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                            body=route['body'] if 'body' in route else snap['body'],
                            params=route.get('params'), response_formatter=raw_json)
        entry['response'] = raw
        entry['state'] = 'submitted_needs_final_evidence'
        self.save()
        if raw.startswith('❌'):
            raise ValueError('Native acceptance transaction rejected: ' + raw[:2000])
        return entry

    async def lots(self):
        for item in self.plan['sampling_lots']:
            key = 'lot_create/' + item['name']
            if key in self.state['scenarios']:
                old = await ql.lot_record(item['name'])
                if not old or self.state['scenarios'][key].get('lot_id') != old['MfgLotId']:
                    raise ValueError('Reconcile previous lot create')
                continue
            if await ql.lot_record(item['name']):
                raise ValueError('Unjournaled existing lot')
            entry = await self.quality_submit(key, 'sampling_lot', dict(lot_name=item['name'],
                product_name=item['product'], product_revision=item['revision'], quantity=str(item['qty']),
                uom_name=item['uom'], sample_rate=None, allow_adjust_to_lot_size=False))
            row = await ql.lot_record(item['name'])
            if not row:
                raise ValueError('Created lot not found')
            entry['lot_id'] = row['MfgLotId']
            entry['read_back'] = row
            self.save()
            print('LOT CREATED', item['name'], row['MfgLotId'], flush=True)

    async def starts(self):
        level = await self.find('ContainerLevels', 'BOX')
        if not level or level.get('InstanceID') != '0005288000000003':
            raise ValueError('Level identity not confirmed')
        for item in self.plan['containers']:
            name = item['name']
            key = 'start/' + name
            existing = await q.sql_rows('SELECT TOP 2 ContainerId,ContainerName,LevelId,ProductId,MfgOrderId,Qty,Qty2,UOMId,UOM2Id,SamplingLotId '
                'FROM Container WHERE ContainerName=' + q.literal(name), 2)
            if existing:
                prior = self.state['scenarios'].get(key)
                if len(existing) != 1 or not prior or prior.get('container_id') != existing[0]['ContainerId']:
                    raise ValueError('Existing Container needs reconciliation: ' + name)
                print('VERIFIED EXISTING CONTAINER', name, flush=True)
                continue
            if key in self.state['scenarios']:
                raise ValueError('Unresolved Start attempt: ' + name)
            product = await self.find('Products', item['product'])
            order = await self.find('MfgOrders', item['mfg_order'])
            if (not product or product.get('Revision') != item['revision'] or not order
                    or (order.get('Product') or {}).get('InstanceId') != product['InstanceID']
                    or float(order['Qty']) < sum(x['qty'] for x in self.plan['containers'] if x['mfg_order'] == item['mfg_order'])):
                raise ValueError('Order/product/revision/quantity preflight failed: ' + name)
            d = dict(containerName=name, autoNumber=False, mfgOrder={'name': item['mfg_order'], 'instanceId': order['InstanceID']},
                product={'name': item['product'], 'revision': item['revision'], 'instanceId': product['InstanceID']},
                qty=item['qty'], level={'name': 'BOX', 'instanceId': level['InstanceID']},
                owner={'name': 'PRODUCTION', 'instanceId': '0005468000000002'},
                startReason={'name': 'NORMAL', 'instanceId': '0007808000000002'}, uom={'name': item['uom']})
            suffix = name.rsplit('-', 1)[1]
            lot_suffix = {'R01': 'LR', 'R02': 'LR', 'S01': 'LS', 'S02': 'LS', 'F01': 'LF', 'F02': 'LF'}.get(suffix)
            if lot_suffix:
                lot = await ql.lot_record(PREFIX + '-' + lot_suffix)
                if not lot or lot['ProductId'] != product['InstanceID']:
                    raise ValueError('Start lot binding not confirmed')
                d['samplingLot'] = {'name': lot['MfgLotName'], 'instanceId': lot['MfgLotId']}
            if 'qty2' in item:
                d['qty2'] = item['qty2']
                d['uoM2'] = {'name': item['uom2']}
            body = {'details': d, 'comments': 'Approved isolated C4 acceptance ' + uuid.uuid4().hex}
            preflight = {'product': product, 'order': order, 'level': level}
            for field, expected in [('Owner', 'PRODUCTION'), ('StartReason', 'NORMAL')]:
                raw = await request('POST', '/api/Start/RequestSelectionValues', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                    body=body, params={'selectionValuesExpression': 'Details.' + field}, response_formatter=raw_json)
                data = json.loads(raw)
                node = data.get('data', data).get('Details', {}).get(field, {})
                candidates = node.get('SelectionValuesEx')
                if not isinstance(candidates, list) or len(candidates) != 1 or candidates[0].get('Name') != expected:
                    raise ValueError('Start contextual selection not confirmed: ' + field + ' ' + raw[:1200])
                preflight[field] = data
            if lot_suffix:
                raw = await request('POST', '/api/Start/RequestSelectionValues', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                    body=body, params={'selectionValuesExpression': 'Details.SamplingLot'}, response_formatter=raw_json)
                data = json.loads(raw)
                candidates = data.get('data', data).get('Details', {}).get('SamplingLot', {}).get('SelectionValuesEx', [])
                if not any(x.get('Name') == lot['MfgLotName'] and x.get('InstanceId') == lot['MfgLotId'] for x in candidates):
                    raise ValueError('Intended lot unavailable in native Start context')
                preflight['SamplingLot'] = data
            entry = {'state': 'in_flight', 'body': body, 'preflight': preflight}
            self.state['scenarios'][key] = entry
            self.save()
            raw = await request('POST', '/api/Start', base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                body=body, params={'execute': 'true'}, response_formatter=raw_json)
            entry['response'] = raw
            entry['state'] = 'submitted_needs_final_evidence'
            self.save()
            rows = await q.sql_rows('SELECT TOP 2 ContainerId,ContainerName,LevelId,ProductId,MfgOrderId,Qty,Qty2,UOMId,UOM2Id,SamplingLotId '
                'FROM Container WHERE ContainerName=' + q.literal(name), 2)
            entry['read_back'] = rows
            self.save()
            if raw.startswith('❌') or len(rows) != 1:
                raise ValueError('Start did not verify: ' + raw[:1800])
            row = rows[0]
            if (row['ProductId'] != product['InstanceID'] or row['MfgOrderId'] != order['InstanceID']
                    or row['LevelId'] != level['InstanceID'] or float(row['Qty']) != item['qty']
                    or float(row.get('Qty2') or 0) != item.get('qty2', 0)
                    or row.get('SamplingLotId', '') != (lot['MfgLotId'] if lot_suffix else '')):
                raise ValueError('Started Container context differs from manifest: ' + name)
            entry['container_id'] = row['ContainerId']
            entry['state'] = 'created_and_read_back'
            self.save()
            print('CONTAINER CREATED', name, row['ContainerId'], flush=True)

    async def collections(self):
        from tools.quality_lot_execution import lot_view
        for ls, members in [('LR', ['R01', 'R02']), ('LS', ['S01', 'S02']), ('LF', ['F01', 'F02'])]:
            lotname = PREFIX + '-' + ls
            for member in members:
                key = 'collect/' + member
                if key in self.state['scenarios']:
                    raise ValueError('Collection already attempted; inspect its final evidence before continuing')
                args = dict(lot_name=lotname, container_name=PREFIX + '-' + member,
                    sample_test_name=PREFIX + '-TEST', sample_test_revision='1',
                    lot_instance_id=None, container_instance_id=None, lot_sample_data_instance_id=None,
                    samples_collected=1, rejects=1, reject_reason_name='SCRAP', samples=None, comments='', counted_groups=None)
                entry = await self.quality_submit(key, 'lot_inspection', args)
                entry['read_back'] = await lot_view(lotname)
                self.save()
                print('COLLECTED', member, json.dumps(entry['read_back']['tasks']), flush=True)

    async def dispositions(self):
        from tools import quality_lot_rework as rework, quality_lot_scrap as scrap
        for key, kind, args, reader in [
            ('positive_rework', 'lot_rework', dict(lot_name=PREFIX+'-LR', lot_instance_id=None,
                rework_reason_name=PREFIX+'-RW', comments='', expected_path_name='C4Repair'), rework.inspect_snapshot),
            ('positive_scrap', 'lot_scrap', dict(lot_name=PREFIX+'-LS', lot_instance_id=None,
                loss_reason_name='SCRAP', comments=''), scrap.inspect_snapshot)]:
            if key in self.state['scenarios']:
                entry = self.state['scenarios'][key]
                entry['read_back'] = await reader(entry['snapshot'])
                self.save()
                print('INSPECTED ORIGINAL DISPOSITION', key, flush=True)
                continue
            entry = await self.quality_submit(key, kind, args)
            entry['read_back'] = await reader(entry['snapshot'])
            self.save()
            print('DISPOSITION SUBMITTED', key, flush=True)

    async def losses(self):
        from tools.quality_quantity_loss import inspect_snapshot
        for suffix, options in [('D01', dict(loss_quantity='2', secondary_loss_quantity='1', record_all_quantity=False)),
                                ('A01', dict(loss_quantity=None, record_all_quantity=True))]:
            key = 'quantity_loss/' + suffix
            if key in self.state['scenarios']:
                raise ValueError('Quantity loss already attempted; inspect original evidence')
            entry = await self.quality_submit(key, 'quantity_loss', dict(container_name=PREFIX+'-'+suffix,
                container_instance_id=None, loss_reason_name='SCRAP', close_when_zero=True, adjust_throughput=True,
                comments='', **options))
            entry['read_back'] = await inspect_snapshot(entry['snapshot'])
            self.save()
            print('LOSS SUBMITTED', suffix, flush=True)

    async def negative(self):
        from tools.quality_lot_execution import lot_view
        from tools.quality_lot_rework_evidence import sampling_tasks
        from tools.quality_quantity_loss import record

        key = 'negative_held_member'
        if key in self.state['scenarios']:
            raise ValueError('Negative scenario already started; reconcile its journal')
        args = dict(lot_name=PREFIX+'-LF', lot_instance_id=None,
                    rework_reason_name=PREFIX+'-RW', comments='', expected_path_name='C4Repair')
        ready = json.loads(await qp.prepare('lot_rework', args))
        if ready.get('state') != 'ready':
            raise ValueError(json.dumps(ready, ensure_ascii=True))
        ids = [self.state['scenarios']['start/'+PREFIX+'-'+suffix]['container_id'] for suffix in ('F01','F02')]
        ref = {'name': PREFIX+'-F02', 'instanceId': ids[1]}
        entry = {'state': 'running', 'plan': ready, 'transactions': {}}
        self.state['scenarios'][key] = entry
        self.save()

        async def post(service, body):
            if service in entry['transactions']:
                raise ValueError('Negative scenario mutation was already attempted')
            tx = {'body': body, 'state': 'in_flight'}
            entry['transactions'][service] = tx
            self.save()
            tx['response'] = await request('POST', '/api/'+service, base_url=CAMSTAR_SHOPFLOOR_BASE_URL,
                                           params={'execute': 'true'}, body=body, response_formatter=raw_json)
            tx['state'] = 'response_recorded'
            self.save()
            return tx['response']

        async def evidence():
            lot = await lot_view(args['lot_name'])
            if [m['ContainerId'] for m in lot['members']] != ids:
                raise ValueError('Negative lot membership differs from approved scope')
            history = await q.sql_rows('SELECT TOP 30 HistoryMainlineId,TxnId,ContainerId,TxnTypeName,Comments '
                'FROM HistoryMainline WHERE ContainerId IN ('+','.join(q.literal(cid) for cid in ids)
                +') ORDER BY HistoryMainlineId', 30)
            if len(history) >= 30:
                raise ValueError('Negative scenario history exceeds its bound')
            return {'lot': lot, 'tasks': await sampling_tasks(lot['members']),
                    'members': [await record(cid) for cid in ids], 'history': history}

        entry['initial'] = await evidence()
        await post('Hold', {'container': ref, 'holdReason': {'name': PREFIX+'-HOLD'},
                            'comments': 'C4 acceptance '+uuid.uuid4().hex})
        entry['held'] = await evidence()
        self.save()
        if [r['CurrentHoldCount'] for r in entry['held']['members']] != [0, 1]:
            raise ValueError('Expected hold on F02 not confirmed')
        try:
            entry['stale_plan'] = json.loads(await qp.execute_quality_transaction(ready['plan_id'], ready['plan_digest']))
            self.save()
            if entry['stale_plan'].get('submitted') is not False or await evidence() != entry['held']:
                raise ValueError('Stale client plan did not fail closed')
            body = dict(ready['request_preview']['body'], comments='MCP-QUAL:'+uuid.uuid4().hex)
            result = await post('ExecuteSamplingLot', body)
            entry['after_failure'] = await evidence()
            entry['failure_parent_history'] = await q.sql_rows('SELECT TOP 3 HistoryMainlineId,TxnId,TxnTypeName '
                'FROM HistoryMainline WHERE Comments='+q.literal(body['comments']), 3)
            entry['rollback_observed'] = (result.startswith('❌ HTTP 400') and 'hold' in result.lower()
                and entry['after_failure'] == entry['held'] and not entry['failure_parent_history'])
            self.save()
        finally:
            held = await record(ids[1])
            if held and held['CurrentHoldCount'] == 1:
                await post('Release', {'container': ref, 'releaseReason': {'name': PREFIX+'-RELEASE'},
                                       'comments': 'C4 acceptance '+uuid.uuid4().hex})
                entry['released'] = await evidence()
                self.save()
        if not entry.get('rollback_observed') or entry['released']['members'][1]['CurrentHoldCount'] != 0:
            raise ValueError('Negative scenario needs reconciliation')
        entry['state'] = 'verified'
        self.save()

    async def history(self):
        from decimal import Decimal

        for key, member_suffixes, result, deltas in [
            ('positive_rework', ('R01', 'R02'), '2', None),
            ('positive_scrap', ('S01', 'S02'), '3', (-1, 0)),
            ('quantity_loss/D01', ('D01',), None, (-2, -1)),
            ('quantity_loss/A01', ('A01',), None, (-2, 0)),
        ]:
            entry = self.state['scenarios'][key]
            snap = entry['snapshot']
            read_back = entry['read_back']
            member_ids = {self.state['scenarios']['start/'+PREFIX+'-'+suffix]['container_id']
                          for suffix in member_suffixes}
            marker = q.literal(snap['body']['comments'])
            roots = await q.sql_rows('SELECT TOP 2 HistoryMainlineId,TxnId,TxnTypeName,ContainerId,ReversalStatus '
                                     'FROM HistoryMainline WHERE Comments='+marker, 2)
            expected_type = 'ExecuteSamplingLot' if result else 'ChangeQty'
            if (len(roots) != 1 or roots[0]['TxnTypeName'] != expected_type or roots[0]['ReversalStatus'] != '1'
                    or not all(q.valid_id(roots[0][field]) for field in ('HistoryMainlineId','TxnId'))):
                raise ValueError('Final transaction root missing or ambiguous: '+key)
            root = roots[0]
            tid, rid = q.literal(root['TxnId']), root['HistoryMainlineId']
            mainlines = await q.sql_rows('SELECT TOP 8 HistoryMainlineId,TxnId,ContainerId,TxnTypeName,ReversalStatus '
                                         'FROM HistoryMainline WHERE TxnId='+tid+' ORDER BY HistoryMainlineId', 8)
            child_type = 'Rework' if result == '2' else 'ChangeQty'
            members = [row for row in mainlines if row['ContainerId']]
            if (len(mainlines) != len(member_ids)+bool(result) or len(members) != len(member_ids)
                    or {row['ContainerId'] for row in members} != member_ids
                    or any(row['TxnTypeName'] != child_type or row['ReversalStatus'] != '1' for row in members)):
                raise ValueError('Final member history does not match approved set: '+key)
            final = {'root': root, 'mainlines': mainlines}
            if result:
                lot = await q.sql_rows('SELECT TOP 2 ExecuteSamplingLotHistoryId,HistoryMainlineId,HistoryId,'
                    'TxnId,SamplingLotResult,SamplingCompleted,ReworkReasonId,ScrapReasonId '
                    'FROM ExecuteSamplingLotHistory WHERE TxnId='+tid, 2)
                reason_field = 'ReworkReasonId' if result == '2' else 'ScrapReasonId'
                if (len(lot) != 1 or lot[0]['HistoryMainlineId'] != rid or lot[0]['HistoryId'] != snap['target_id']
                        or lot[0]['SamplingLotResult'] != result or lot[0]['SamplingCompleted'] != 'true'
                        or lot[0][reason_field] != snap['reason']['instanceId']
                        or read_back['current_lot']['lot']['SamplingLotResult'] != int(result)
                        or not read_back['sampling_task_cleanup']['all_original_tasks_absent']):
                    raise ValueError('Final lot result, reason or cleanup mismatch: '+key)
                final['lot_history'] = lot
            if result == '2':
                rows = await q.sql_rows('SELECT TOP 8 MoveHistoryId,HistoryMainlineId,HistoryId,TxnId,Qty,'
                    'PathId,ToStepId,ToSpecId,ReEntryStepId,EndReworkStepId,ReworkReasonId FROM MoveHistory WHERE TxnId='+tid, 8)
                routes = {row['container']['instanceId']: row for row in snap['preview']['member_routes']}
                if (len(rows) != len(member_ids) or {(r['HistoryId'],r['HistoryMainlineId']) for r in rows}
                        != {(m['ContainerId'],m['HistoryMainlineId']) for m in members}
                        or not all(r['expected_route_currently_observed'] and r['quantity_currently_unchanged']
                                   for r in read_back['member_route_evidence'])):
                    raise ValueError('Final rework member evidence mismatch')
                for row in rows:
                    route = routes[row['HistoryId']]
                    expected = {'PathId': route['path']['instanceId'], 'ToStepId': route['to_step']['instanceId'],
                                'ToSpecId': route['target_spec']['instanceId'], 'ReEntryStepId': route['reentry_step']['instanceId'],
                                'EndReworkStepId': route['end_rework']['InstanceId'],
                                'ReworkReasonId': snap['reason']['instanceId']}
                    if any(row[k] != v for k,v in expected.items()) or Decimal(row['Qty']) != Decimal(route['container_sql']['Qty']):
                        raise ValueError('Final rework route/quantity history mismatch')
                final['move_history'] = rows
            else:
                rows = await q.sql_rows('SELECT TOP 8 h.QtyHistoryId,h.HistoryMainlineId,h.HistoryId,h.TxnId,'
                    'h.Qty,h.Qty2,h.IntegrationTxnId,d.QtyHistoryDetailsId,d.ContainerId,d.ReasonCodeId,d.ChangeQtyType,'
                    'd.QtyMultiplier,d.AdjustThruput,d.CountsAgainstProduction,d.RecordAllQty,d.ThisContainerClosed,d.UOMId,d.UOM2Id '
                    'FROM QtyHistory h INNER JOIN QtyHistoryDetails d ON d.QtyHistoryId=h.QtyHistoryId AND d.TxnId=h.TxnId '
                    'WHERE h.TxnId='+tid, 8)
                closed = key != 'quantity_loss/D01'
                if (len(rows) != len(member_ids) or {(r['ContainerId'],r['HistoryMainlineId']) for r in rows}
                        != {(m['ContainerId'],m['HistoryMainlineId']) for m in members}
                        or any(r['HistoryId'] != r['ContainerId'] or r['IntegrationTxnId'] != rid
                            or r['ReasonCodeId'] != snap['reason']['instanceId'] or r['ChangeQtyType'] != '2'
                            or r['QtyMultiplier'] != '-1' or r['AdjustThruput'] != 'true' or r['CountsAgainstProduction'] != 'true'
                            or r['RecordAllQty'] != str(closed).lower() or r['ThisContainerClosed'] != str(closed).lower()
                            or (Decimal(r['Qty']),Decimal(r['Qty2'] or 0)) != deltas for r in rows)):
                    raise ValueError('Final loss history or production adjustment mismatch: '+key)
                if result:
                    if not all(r['zero_quantity_currently_observed'] and r['closed_currently_observed']
                               for r in read_back['original_members']):
                        raise ValueError('Final scrap member balances or status mismatch')
                elif closed:
                    current = read_back['current_record']
                    if current['Status'] != 2 or Decimal(current['Qty']) != 0:
                        raise ValueError('Final full loss balance or status mismatch')
                elif not read_back['dual_quantity_evidence']['current_balances_match_projection']:
                    raise ValueError('Final dual loss balances or units mismatch')
                final['quantity_history'] = rows
            entry['final_history'] = final
            entry['state'] = 'verified'
            self.save()
            print('VERIFIED FINAL HISTORY', key, root['TxnId'], flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--approved', action='store_true', required=True)
    parser.add_argument('stage', choices=['modeling', 'lots', 'starts', 'collections', 'dispositions', 'losses', 'negative', 'history'])
    args = parser.parse_args()
    runner = Runner()
    try:
        await getattr(runner, args.stage)()
    finally:
        runner.save()
        await close_client()


if __name__ == '__main__':
    asyncio.run(main())
