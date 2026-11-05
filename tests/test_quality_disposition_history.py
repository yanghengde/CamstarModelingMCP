import asyncio
import copy
import json
from datetime import datetime, timezone

import pytest

from tools import quality_disposition_history as disposition, quality_history as history, quality_plans as plans


def key(number):
    return f'{number:016x}'


CONTAINER, PRODUCT, SPEC, STEP, OPERATION, UNIT, UNIT2, REASON, ROOT, TXN, SUMMARY, DETAIL = map(key, range(1,13))
MARKER = 'MCP-QUAL:'+'f'*32
TIME = '2026-11-04T15:01:38.127'
EPOCH = datetime.fromisoformat(TIME).replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(plans,'PLAN_DB',tmp_path/'quality.sqlite3')
    reason = {'name': 'SCRAP', 'instanceId': REASON}
    record = {'ContainerId': CONTAINER, 'ContainerName': 'C', 'ProductId': PRODUCT, 'SpecId': SPEC,
              'WorkflowStepId': STEP, 'UOMId': UNIT, 'UOM2Id': UNIT2, 'Qty': '10', 'Qty2': '5', 'ChangeCount': 1}
    snap = {'kind': 'quantity_loss', 'action': 'loss', 'service': 'ChangeQty', 'target_id': CONTAINER, 'name': 'C',
            'environment': plans.environment(), 'marker': MARKER, 'reason': reason, 'expected_loss_quantity': '2',
            'before': {'container': record,'container_status': {'Operation': {'InstanceId': OPERATION}}},
            'arguments': {'close_when_zero': True, 'record_all_quantity': False, 'adjust_throughput': True,
                          'loss_quantity': '2', 'secondary_loss_quantity': '1'},
            'preview': {'projected_remaining_quantity': '8', 'secondary_loss_quantity': '1',
                        'projected_remaining_secondary_quantity': '4', 'projected_close': False},
            'body': {'container': {'name': 'C', 'instanceId': CONTAINER}, 'comments': MARKER+' reason',
                     'closeWhenZero': True, 'disassociateWhenClosed': False, 'generateRollups': False,
                     'recordAllQtySummaryOnly': False, 'serviceDetails': [{'@odata.type': '#shopfloor.LossDetails',
                     'changeQtyType': 2, 'recordAllQty': False, 'adjustThruput': True,
                     'qty': 2.0, 'qty2': 1.0, 'reasonCode': reason}]}}
    root = {'HistoryMainlineId': ROOT, 'HistoryId': CONTAINER, 'TxnId': TXN, 'IntegrationTxnId': ROOT,
            'TxnDateGMT': TIME, 'TxnType': '4000', 'TxnTypeName': 'ChangeQty', 'Comments': snap['body']['comments'],
            'ContainerId': CONTAINER, 'ContainerName': 'C', 'ContainerChangeCount': '1', 'ReversalStatus': '1',
            'ProductId': PRODUCT, 'OperationId': OPERATION, 'SpecId': SPEC, 'WorkflowStepId': STEP,
            'Qty': '10', 'Qty2': '5', 'ToQty': '8', 'ToQty2': '4'}
    summary = {'QtyHistoryId': SUMMARY, 'HistoryMainlineId': ROOT, 'HistoryId': CONTAINER, 'TxnId': TXN,
               'IntegrationTxnId': ROOT, 'CDOTypeId': '5080', 'Qty': '-2', 'Qty2': '-1',
               'CloseWhenZero': 'true', 'ThruputSummary': 'false'}
    detail = {'QtyHistoryDetailsId': DETAIL, 'QtyHistoryId': SUMMARY, 'HistoryId': CONTAINER, 'TxnId': TXN,
              'IntegrationTxnId': ROOT, 'CDOTypeId': '6932', 'ContainerId': CONTAINER, 'ProductId': PRODUCT,
              'OperationId': OPERATION, 'ChargeToStepId': STEP, 'ReasonCodeId': REASON, 'ChangeQtyType': '2',
              'QtyMultiplier': '-1', 'Qty': '2', 'Qty2': '1', 'UOMId': UNIT, 'UOM2Id': UNIT2,
              'AdjustThruput': 'true', 'CountsAgainstProduction': 'true', 'RecordAllQty': 'false',
              'CloseWhenZero': 'true', 'ThisContainerClosed': 'false', 'ThisContainerDisassociated': 'false',
              'IsRollup': 'false'}
    state = {'snapshot': snap, 'roots': [root], 'mainlines': [root], 'summaries': [summary], 'details': [detail],
             'lot': [], 'queries': []}

    async def sql(query, maximum):
        state['queries'].append(query)
        assert query.startswith('SELECT ')
        if 'WHERE Comments=' in query:
            assert disposition.q.literal(state['snapshot']['body']['comments']) in query
            return copy.deepcopy(state['roots'][:maximum])
        assert "WHERE TxnId=N'"+TXN+"'" in query
        table = {'FROM HistoryMainline ': 'mainlines', 'FROM QtyHistory ': 'summaries',
                 'FROM QtyHistoryDetails ': 'details', 'FROM ExecuteSamplingLotHistory ': 'lot'}
        for token, label in table.items():
            if token in query:
                rows = copy.deepcopy(state[label])
                if state.get('changed') == label and sum(token in q and 'WHERE TxnId=' in q for q in state['queries']) > 1:
                    rows[0]['ChangeCount'] = '2'
                return rows
        raise AssertionError(query)

    monkeypatch.setattr(disposition.q,'sql_rows',sql)
    return state


def install(env, state='unknown', started=EPOCH-1, finished=EPOCH+1, attempted=True):
    snap = env['snapshot']
    digest = plans.digest(snap)
    with plans.db_connect() as db:
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,NULL)', ('plan',digest,json.dumps(snap),state,started-10))
        if attempted:
            db.execute('INSERT INTO attempts VALUES (?,?,?,?)', ('plan','a'*32,started,finished))
    return digest


def reconcile(digest, candidate=None):
    return json.loads(asyncio.run(history.reconcile_quality_execution('plan',digest,candidate)))


def test_exact_dual_loss_history_attributed_without_unlocking(env):
    digest = install(env)
    result = reconcile(digest)
    assert result['comparison'] == 'native_history_match'
    assert result['request_correlation_verified'] and result['recorded_business_effects_verified']
    assert result['candidate_txn_id'] == TXN
    assert not result['verified_success'] and not result['state_changed'] and not result['retry_allowed']
    with plans.db_connect() as db:
        assert db.execute('SELECT state,outcome FROM plans').fetchone() == ('unknown',None)
        assert not db.execute('SELECT 1 FROM reviews').fetchall()


@pytest.mark.parametrize(('table','field','bad'), [
    ('roots','ReversalStatus','2'), ('roots','ContainerChangeCount','2'), ('roots','ToQty2','3'),
    ('summaries','Qty2','0'), ('summaries','HistoryMainlineId',key(99)), ('summaries','CloseWhenZero','false'),
    ('details','IntegrationTxnId',key(99)), ('details','ReasonCodeId',key(99)), ('details','UOM2Id',UNIT),
    ('details','AdjustThruput','false'), ('details','ThisContainerClosed','true'), ('details','IsRollup','true'),
])
def test_same_marker_does_not_verify_wrong_history(env, table, field, bad):
    env[table][0][field] = bad
    result = reconcile(install(env))
    assert result['comparison'] == 'mismatch' and not result['request_correlation_verified']


@pytest.mark.parametrize('case', ['absent', 'duplicate', 'wrong_candidate', 'extra_detail', 'readback_changed', 'wrong_time', 'unfinished'])
def test_ambiguous_or_unstable_history_is_never_attributed(env, case):
    candidate = None
    started, finished = EPOCH-1, EPOCH+1
    if case == 'absent':
        env['roots'] = []
    elif case == 'duplicate':
        env['roots'].append(env['roots'][0] | {'HistoryMainlineId': key(99)})
    elif case == 'wrong_candidate':
        candidate = key(99)
    elif case == 'extra_detail':
        env['details'].append(env['details'][0] | {'QtyHistoryDetailsId': key(99)})
    elif case == 'readback_changed':
        env['changed'] = 'details'
    elif case == 'wrong_time':
        started, finished = EPOCH+20, EPOCH+21
    else:
        finished = None
    assert not reconcile(install(env,started=started,finished=finished),candidate)['request_correlation_verified']


@pytest.mark.parametrize(('state','attempted'), [('ready',False), ('unknown',False), ('human_committed',True)])
def test_no_history_read_without_eligible_local_attempt(env,state,attempted):
    assert 'error' in reconcile(install(env,state=state,attempted=attempted))
    assert not env['queries']


def test_candidate_listing_and_wrong_fingerprint(env):
    digest = install(env)
    assert 'error' in reconcile('0'*64)
    assert not env['queries']
    result = json.loads(asyncio.run(history.find_quality_execution_candidates('plan',digest)))
    assert result['rows'][0]['TxnId'] == TXN and result['page_complete']
    assert not result['request_correlation_verified']


@pytest.mark.parametrize('case', ['match', 'wrong_lot', 'missing_member', 'wrong_result'])
def test_batch_scrap_requires_parent_lot_and_exact_members(env, case):
    lot = key(30)
    snap = env['snapshot']
    original = snap['before']['container']
    root = env['roots'][0] | {'ContainerId': '', 'HistoryId': '', 'TxnTypeName': 'ExecuteSamplingLot', 'TxnType': '8980'}
    env['roots'] = [root]
    before, dispositions = [], []
    for n in (40,50):
        cid, mid, sid, did = map(key,range(n,n+4))
        record = original | {'ContainerId': cid, 'ContainerName': f'C{n}', 'Qty': '1', 'Qty2': '', 'UOM2Id': ''}
        before.append(record)
        dispositions.append({'container': {'name': f'C{n}', 'instanceId': cid}, 'record': record,
            'status': snap['before']['container_status'], 'expected_loss_quantity': '1', 'expected_remaining_quantity': '0',
            'native_defaults': {'root': {'CloseWhenZero': True}, 'adjust_throughput': True}})
        env['mainlines'].append(env['mainlines'][0] | {'HistoryMainlineId': mid,'ContainerId': cid,'HistoryId': cid,
            'ContainerName': f'C{n}', 'Qty': '1','Qty2': '', 'ToQty': '0', 'ToQty2': '0', 'Comments': ''})
        env['summaries'].append(env['summaries'][0] | {'HistoryMainlineId': mid,'QtyHistoryId': sid,'HistoryId': cid,
            'Qty': '-1','Qty2': '0'})
        env['details'].append(env['details'][0] | {'QtyHistoryId': sid,'QtyHistoryDetailsId': did,'ContainerId': cid,
            'HistoryId': cid,'Qty': '1','Qty2': '', 'UOM2Id': '', 'ThisContainerClosed': 'true', 'RecordAllQty': 'true'})
    env['mainlines'][0] = root
    env['summaries'].pop(0)
    env['details'].pop(0)
    env['lot'] = [{'ExecuteSamplingLotHistoryId': key(31), 'HistoryMainlineId': ROOT, 'HistoryId': lot, 'TxnId': TXN,
        'IntegrationTxnId': ROOT, 'CDOTypeId': '8989', 'SamplingLotResult': '3', 'SamplingCompleted': 'true',
        'ScrapReasonId': REASON, 'ReworkReasonId': ''}]
    snap.update(kind='lot_scrap', action='scrap', service='ExecuteSamplingLot', name='L', target_id=lot,
                before={'members': before}, body={'samplingLot': {'name': 'L', 'instanceId': lot}, 'samplingLotResult': 3,
                'scrapReason': snap['reason'], 'comments': root['Comments']},
                preview={'member_dispositions': dispositions, 'reason_binding':
                         {'concrete_type': 'LossReason', 'instance_id': REASON, 'load_verified': True}})
    if case == 'wrong_lot':
        env['lot'][0]['HistoryId'] = key(999)
    elif case == 'missing_member':
        env['mainlines'].pop()
    elif case == 'wrong_result':
        env['lot'][0]['SamplingLotResult'] = '2'
    result = reconcile(install(env))
    assert result['request_correlation_verified'] is (case == 'match')
    assert result['comparison'] == ('native_history_match' if case == 'match' else 'mismatch')
