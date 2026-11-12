"""C6 isolated history -> local analysis -> native annotation acceptance, no SPC engine.

Create a read-only manifest first. Each mutation is durably journaled before
submission and cannot be replayed. Collection occurs BEFORE attaching the
annotation-only chart group, so it cannot invoke inline SPC for that group.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL, SAFE_CREATE_THRESHOLD
from core.http_client import get_client, get_headers, close_client
from core.transfer_attempt_lock import attempt_lock
from tools import quality as q, quality_plans as plans, quality_quantity_loss as loss
from tools import quality_spc_annotations as annotations, quality_spc_history as spc
from tools import quality_history, collection_submission as collection, collection_verification
from tools.bom_modeling import lookup
from tools.container_start import request_container_start_selection_values
from tools.process_context import ReadContext


VALUES = [9, 10, 11, 14]
NOTE = 'C6 isolated synthetic-data investigation; local calculation only, no native SPC engine or release decision.'


def models(prefix):
    ref = lambda suffix, rev=False: {'name': prefix+'-'+suffix, **({'revision': '1'} if rev else {})}
    def obj(entity, suffix, **fields):
        return {'entity': entity, 'body': {'name': prefix+'-'+suffix,
                'description': 'C6 isolated synthetic history and annotation acceptance; no engine', **fields}}
    return [
        obj('Operations', 'OP', thruputReportingLevel={'name': 'BOX'}),
        obj('UserDataCollectionDefs', 'DATA', revision='1', status=1, dataPointLayout=1,
            dataPoints=[{'@odata.type': '#modeling.ValueDataPointChanges', 'name': 'TestValue',
                         'dataType': 9, 'columnPosition': 1, 'rowPosition': 1, 'isRequired': True,
                         'uom': {'name': 'EACH'}}]),
        obj('Specs', 'SPEC', revision='1', status=1, operation=ref('OP'),
            txnMap=[{'dataCollectionDef': ref('DATA', True), 'txnToUse': 2200, 'required': True, 'sequence': 1}]),
        obj('Workflows', 'WF', revision='1', status=1,
            steps=[{'@odata.type': '#modeling.SpecStepChanges', 'name': 'Collect', 'stepType': 1,
                    'sequence': 1, 'spec': ref('SPEC', True)}]),
        obj('Products', 'PRODUCT', revision='1', status=1, productType={'name': 'BOX'},
            workflow=ref('WF', True), stdStartUOM={'name': 'EACH'}),
        obj('MfgOrders', 'ORDER', qty=1, product=ref('PRODUCT', True), uom={'name': 'EACH'}),
        # Native enum SPCChartDisplayOption.NoChartNever=3; no type, query,
        # connection or compiled rules are invented for this annotation fixture.
        obj('SPCChartDefs', 'CHART', chartVariable='TestValue', spcChartDisplayOption=3,
            recordAgainstCustomSPCDataSrc=False, triggerOCAPs=False, requireAnnotation=False),
        obj('SPCChartDefGroups', 'CHARTS', entries=[ref('CHART')]),
    ]


def read(path):
    envelope = json.loads(path.read_text(encoding='utf8'))
    value = envelope['payload']
    if (not re.fullmatch(r'CDX-C6-[A-Z0-9-]{1,40}', value['prefix'])
            or envelope['digest'] != plans.digest(value) or value['environment'] != plans.environment()
            or value['models'] != models(value['prefix']) or value['root_objects'] != 9
            or value['values'] != VALUES or value['annotation'] != NOTE
            or value['container_name'] != value['prefix']+'-SN' or value['baseline_count'] != 3):
        raise ValueError('Isolated manifest scope, content or environment changed.')
    return value


def records(path):
    journal = path.with_suffix('.journal.jsonl')
    return [json.loads(line) for line in journal.read_text(encoding='utf8').splitlines()] if journal.exists() else []


def log(path, stage, data):
    with path.with_suffix('.journal.jsonl').open('a', encoding='utf8') as stream:
        stream.write(json.dumps({'stage': stage, 'at': time.time(), 'data': data}, ensure_ascii=False)+'\n')
        stream.flush()
        os.fsync(stream.fileno())


def saved(path, stage):
    found = [row['data'] for row in records(path) if row['stage'] == stage]
    if len(found) != 1:
        raise ValueError('Missing or duplicate journal stage: '+stage)
    return found[0]


async def preflight(prefix):
    if not re.fullmatch(r'CDX-C6-[A-Z0-9-]{1,40}', prefix) or SAFE_CREATE_THRESHOLD < 9:
        raise ValueError('Explicit isolated prefix and root-object threshold9 required.')
    for item in models(prefix):
        if await lookup(item['entity'], item['body']['name']):
            raise ValueError('Existing fixture name, no adoption: '+item['body']['name'])
    if await q.sql_rows('SELECT TOP 2 ContainerId FROM Container WHERE ContainerName='+q.literal(prefix+'-SN'), 2):
        raise ValueError('Container already exists; no adoption.')
    for entity, name in [('ContainerLevels', 'BOX'), ('UOMs', 'EACH'), ('ProductTypes', 'BOX')]:
        await ReadContext(2).resolve(entity, {'Name': name}, revisioned=False)


async def plan(prefix, output):
    await preflight(prefix)
    output.parent.mkdir(parents=True, exist_ok=True)
    value = {'prefix': prefix, 'created': time.time(), 'environment': plans.environment(),
             'models': models(prefix), 'root_objects': 9, 'container_name': prefix+'-SN',
             'values': VALUES, 'baseline_count': 3, 'annotation': NOTE}
    with output.open('x', encoding='utf8') as stream:
        json.dump({'digest': plans.digest(value), 'payload': value}, stream, ensure_ascii=False, indent=2)
    print('Read-only fixture manifest:', output, '8 models, 1 explicit Container qty1 BOX/EACH', flush=True)


async def send(path, stage, method, api, body, *, modeling=True):
    if any(row['stage'] == stage+'_attempt' for row in records(path)):
        raise ValueError('Already attempted; reconcile, never replay: '+stage)
    log(path, stage+'_attempt', {'method': method, 'path': api, 'body': body})
    response = await get_client().request(method,
        (CAMSTAR_BASE_URL if modeling else CAMSTAR_SHOPFLOOR_BASE_URL).rstrip('/')+api,
        headers=get_headers(), json=body, follow_redirects=False)
    log(path, stage+'_response', {'http_status': response.status_code, 'body': response.text})
    if not 200 <= response.status_code < 300:
        raise ValueError('Stopped without retry: '+response.text[:2000])


async def apply(path):
    value = read(path)
    if time.time()-value['created'] > 3600:
        raise ValueError('Fixture manifest expired.')
    prefix = value['prefix']
    await preflight(prefix)
    objects = {}
    for item in value['models']:
        entity, body = item['entity'], item['body']
        await send(path, 'create-'+entity, 'POST', '/api/'+entity, body)
        obj = await ReadContext(2).resolve(entity, {'Name': body['name'],
            **({'Revision': '1'} if 'revision' in body else {})}, revisioned='revision' in body)
        objects[entity] = obj
        log(path, 'verified-'+entity, obj)
        print('Verified', entity, obj['InstanceID'], flush=True)
    workflow = objects['Workflows']
    expanded = await ReadContext(1).get('Workflows', workflow['InstanceID'], {'$expand': 'Steps'})
    if len(expanded['Steps']) != 1 or expanded['Steps'][0]['Name'] != 'Collect':
        raise ValueError('Unexpected workflow steps.')
    step = expanded['Steps'][0]
    await send(path, 'workflow-first', 'PUT', '/api/Workflows/'+workflow['InstanceID'],
        {'firstStep': {'name': 'Collect', 'instanceId': step['InstanceID'], 'parent': {'instanceId': workflow['InstanceID']}}})
    product, order = objects['Products'], objects['MfgOrders']
    if q.identifier(order['Product']) != product['InstanceID'] or product['Revision'] != '1' or float(order['Qty']) != 1:
        raise ValueError('Order/product/version/capacity changed.')
    context = dict(mfg_order_name=order['Name'], product_name=product['Name'], product_revision='1', qty=1,
        level_name='BOX', body_json=json.dumps({'details': {'containerName': value['container_name'], 'autoNumber': False}}))
    choices = {}
    for field in ('Owner', 'StartReason'):
        row = json.loads(await request_container_start_selection_values('Details.'+field, **context))['Details'][field]
        if str(row['RecordCount']) != '1' or len(row['SelectionValuesEx']) != 1:
            raise ValueError('Ambiguous Start selection: '+field)
        choices[field] = row['SelectionValuesEx'][0]['Name']
    body = {'details': {'containerName': value['container_name'], 'autoNumber': False, 'qty': 1,
        'level': {'name': 'BOX'}, 'owner': {'name': choices['Owner']}, 'startReason': {'name': choices['StartReason']},
        'product': {'name': product['Name'], 'revision': '1'}, 'mfgOrder': {'name': order['Name']}}}
    log(path, 'start-preflight', {'body': body, 'root_count': 9, 'unit': 'EACH'})
    await send(path, 'start', 'POST', '/api/Start', body, modeling=False)
    rows = await q.sql_rows('SELECT TOP 2 ContainerId FROM Container WHERE ContainerName='+q.literal(value['container_name']), 2)
    if len(rows) != 1:
        raise ValueError('Container identity missing or ambiguous.')
    record = await loss.record(rows[0]['ContainerId'])
    if record['ProductId'] != product['InstanceID'] or float(record['Qty']) != 1:
        raise ValueError('Started Container readback mismatch.')
    log(path, 'container', record)
    log(path, 'fixtures-complete', {'root_objects_verified': 9})


async def collect(path):
    value = read(path)
    cid = saved(path, 'container')['ContainerId']
    definition = saved(path, 'verified-UserDataCollectionDefs')
    for index, number in enumerate(value['values']):
        label = 'collect-'+str(index)
        if any(row['stage'] == label+'-plan' for row in records(path)):
            if not saved(path, label+'-verified').get('verified_success'):
                raise ValueError('Previous collection is not verified; never advance or replay.')
            continue
        current = await ReadContext(1).get('UserDataCollectionDefs', definition['InstanceID'], {'$expand': 'SPCChartDefGroup'})
        tokens = await q.sql_rows('SELECT DataCollectionDefId,SPCChartDefGroupId FROM DataCollectionDef WHERE DataCollectionDefId='
                                  +q.literal(definition['InstanceID']), 1)
        if (len(tokens) != 1 or tokens[0]['SPCChartDefGroupId'] not in (None, '')
                or current.get('SPCChartDefGroup') is not None or current.get('SPCChartDefEntries') not in (None, [])):
            raise ValueError('Collection fixture is now bound to SPC; never invoke inline engine.')
        ready = json.loads(await collection.preview_container_data_collection(value['container_name'], {'TestValue': number},
            instance_id=definition['InstanceID'], container_instance_id=cid))
        log(path, label+'-plan', ready)
        if not ready.get('executable') or ready.get('error'):
            raise ValueError('Collection preview stopped: '+json.dumps(ready, ensure_ascii=False))
        result = json.loads(await collection.collect_container_data(ready['plan_id'], ready['plan_digest']))
        log(path, label+'-result', result)
        if result.get('state') != 'unknown' or result.get('error'):
            raise ValueError('Collection result requires reconciliation, no replay.')
        verified = json.loads(await collection_verification.verify_collection_submission(ready['plan_id'], ready['plan_digest']))
        log(path, label+'-verified', verified)
        if not verified.get('verified_success'):
            raise ValueError('Collection history not verified; stop before next sample.')
        print('Collected and verified', index, number, flush=True)


def point_ids(path):
    ids = []
    for index in range(len(VALUES)):
        verification = saved(path, 'collect-'+str(index)+'-verified')
        if not verification.get('verified_success'):
            raise ValueError('Unverified collection baseline.')
        rows = verification['verification']['evidence']['rows']
        if len(rows) != 1 or rows[0]['DataName'] != 'TestValue' or float(rows[0]['DataValue']) != VALUES[index]:
            raise ValueError('Unexpected source history.')
        ids.append(annotations.iid(rows[0]['DataPointHistoryDetailId']))
    return ids


async def analyze(path):
    read(path)
    keys = point_ids(path)
    result = json.loads(await spc.analyze_spc_history('i_mr', [[keys[-1]]], ['beyond_3sigma'],
                                                    baseline_groups=[[key] for key in keys[:-1]]))
    log(path, 'analysis', result)
    if not result.get('complete') or not result.get('data_verified_against_mes') or not result.get('has_signals'):
        raise ValueError('Synthetic historical analysis did not match the declared scenario.')
    if result['primary_signals'][0]['window_end_id'] != keys[-1] or result['native_spc_result']:
        raise ValueError('Wrong signal source or result provenance.')
    print('MES-source local analysis verified; native engine not called.', flush=True)


async def bind(path):
    read(path)
    if not saved(path, 'analysis').get('complete'):
        raise ValueError('Analyze the predeclared independent baseline before group binding.')
    definition, group = (saved(path, 'verified-'+entity) for entity in ('UserDataCollectionDefs', 'SPCChartDefGroups'))
    before = await ReadContext(1).get('UserDataCollectionDefs', definition['InstanceID'], {'$expand': 'DataPoints,SPCChartDefGroup'})
    if before.get('SPCChartDefGroup') is not None:
        raise ValueError('Unexpected existing SPC group.')
    log(path, 'binding-before', before)
    await send(path, 'bind-annotation-chart', 'PUT', '/api/UserDataCollectionDefs/'+definition['InstanceID'],
               {'spcChartDefGroup': {'name': group['Name'], 'instanceId': group['InstanceID']}})
    after = await ReadContext(1).get('UserDataCollectionDefs', definition['InstanceID'], {'$expand': 'DataPoints,SPCChartDefGroup'})
    log(path, 'binding-readback', after)
    if q.identifier(after.get('SPCChartDefGroup')) != group['InstanceID'] or after['DataPoints'] != before['DataPoints']:
        raise ValueError('SPC group binding or original point preservation not verified.')
    log(path, 'binding-after', after)


async def annotate(path):
    value = read(path)
    saved(path, 'binding-after')
    if any(row['stage'] == 'annotation-plan' and row['data'].get('state') == 'ready' for row in records(path)):
        raise ValueError('Annotation already prepared/attempted; use reconcile, never replay.')
    cid = saved(path, 'container')['ContainerId']
    chart = saved(path, 'verified-SPCChartDefs')
    ready = json.loads(await annotations.prepare_spc_annotation(value['container_name'], point_ids(path)[-1],
        chart['Name'], value['annotation'], container_instance_id=cid, spc_chart_instance_id=chart['InstanceID']))
    if ready.get('state') != 'ready':
        log(path, 'annotation-preparation-failed', ready)
        raise ValueError('Annotation preparation stopped: '+json.dumps(ready, ensure_ascii=False))
    log(path, 'annotation-plan', ready)
    log(path, 'annotation-result', json.loads(await plans.execute_quality_transaction(ready['plan_id'], ready['plan_digest'])))


async def reconcile(path):
    read(path)
    prepared = [row['data'] for row in records(path) if row['stage'] == 'annotation-plan' and row['data'].get('state') == 'ready']
    if len(prepared) != 1:
        raise ValueError('Expected exactly one executable annotation plan.')
    ready = prepared[0]
    proof = json.loads(await quality_history.reconcile_quality_execution(ready['plan_id'], ready['plan_digest']))
    log(path, 'annotation-proof', proof)
    if not proof.get('recorded_business_effects_verified'):
        raise ValueError('Annotation native evidence incomplete; keep unknown: '+json.dumps(proof, ensure_ascii=False))
    review = json.loads(await plans.review_quality_transaction(ready['plan_id'], ready['plan_digest'], 'resolve_history',
        proof['candidate_txn_id'], 'C6隔离验收：核对原点、完整注释历史、事务及配置保持；不认证SPC引擎或质量放行。', 'committed'))
    log(path, 'annotation-review', review)
    if review.get('state') != 'history_committed':
        raise ValueError('Final annotation audit did not commit: '+json.dumps(review, ensure_ascii=False))
    print('Native annotation history and final local audit verified.', proof['candidate_txn_id'], flush=True)


async def main(args):
    try:
        if args.action == 'plan':
            await plan(args.prefix, args.manifest)
        else:
            read(args.manifest)
            with attempt_lock(plans.PLAN_DB, 'c6-acceptance:'+str(args.manifest.resolve())):
                await globals()[args.action](args.manifest)
    finally:
        await close_client()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply', 'collect', 'analyze', 'bind', 'annotate', 'reconcile'])
    parser.add_argument('--manifest', type=Path, default=Path('tmp/c6-acceptance/manifest.json'))
    parser.add_argument('--prefix', default='CDX-C6-20261112')
    asyncio.run(main(parser.parse_args()))
