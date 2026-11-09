import asyncio
import copy
import json
import time
from unittest.mock import AsyncMock

import pytest

from tools import bom_modeling as bm
from core.tool_catalog import TOOL_POLICIES
from agent.safety import plan_preview


def run(coro): return asyncio.run(coro)
def decode(coro): return json.loads(run(coro))


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, 'PLAN_DB', tmp_path / 'bom.db')
    before = {'InstanceID': 'B1', 'Name': 'B', 'Revision': '1', 'IsFrozen': False,
        'IsRevOfRcd': True, 'Status': 1, 'Description': 'original', 'MaterialList': [
            {'InstanceID': 'L1', 'Name': 'A', 'QtyRequired': 2, 'IssueControl': 5,
             'Product': {'Name': 'P', 'Revision': '1', 'UseROR': False, 'InstanceId': 'transient'},
             'UOM': {'Name': 'EACH', 'InstanceId': 'transient'}, 'ScrapPercent': 3},
            {'InstanceID': 'L2', 'Name': 'B', 'QtyRequired': 1, 'IssueControl': 5}]}
    async def read(*args): return copy.deepcopy(before)
    monkeypatch.setattr(bm, 'read_bom', read)
    monkeypatch.setattr(bm, 'lookup', AsyncMock(return_value=[]))
    async def resolve(self, entity, ref, revisioned=True):
        return {'InstanceID': entity + '-id', 'Name': ref['Name'], 'Revision': ref.get('Revision'), 'Status': 1}
    monkeypatch.setattr(bm.ReadContext, 'resolve', resolve)
    return before


def test_sparse_update_remove_and_preserve(fixture):
    snap = run(bm.build_bom('update', 'B', '1', {'lines': [{'name': 'A', 'quantity': '3.5'}, {'op': 'remove', 'name': 'B'}]}))
    assert snap['body']['materialList-Expanded'][0] == {'key': {'name': 'A'}, 'value': {'name': 'A', 'qtyRequired': 3.5, 'issueControl': 5}}
    assert snap['body']['materialList-Expanded'][1] == {'key': {'name': 'B'}, 'value': {}}
    assert len(snap['expected']['MaterialList']) == 1
    assert snap['expected']['MaterialList'][0]['ScrapPercent'] == 3
    assert snap['expected']['MaterialList'][0]['Product']['Revision'] == '1'


@pytest.mark.parametrize('changes', [
    {'lines': [{'name': 'A'}, {'name': 'a'}]}, {'lines': [{'name': 'A', 'op': 'add'}]},
    {'lines': [{'name': 'missing', 'op': 'remove'}]}, {'lines': [{'name': 'A', 'new_name': 'B'}]},
    {'lines': [{'name': 'A', 'quantity': 'NaN'}]}, {'lines': [{'name': 'A', 'quantity': 0}]},
    {'lines': [{'name': 'A', 'quantity': True}]}, {'lines': [{'name': 'A', 'quantity': '1.1234567890123456789'}]},
    {'lines': [{'name': 'A', 'issue_control': 7}]}, {'lines': [{'name': 'A', 'instanceId': 'evil'}]},
    {'lines': [{'name': 'A', 'op': 'remove', 'quantity': 1}]}, {'status': True},
    {'unknown': 1}, {'lines': [42]}, {'lines': [{'name': 'A', 'scrap_percent': 101}]},
])
def test_invalid_changes_no_plan(fixture, changes):
    assert 'error' in decode(bm.prepare_bom_change('update', 'B', '1', changes))
    with bm.db_connect() as db: assert db.execute('SELECT count(*) FROM plans').fetchone()[0] == 0


def test_add_validated_refs_and_no_unit_conversion(fixture, monkeypatch):
    async def resolve(self, entity, ref, revisioned=True):
        return {'InstanceID': entity + '-id', 'Name': ref['Name'], 'Revision': ref.get('Revision'), 'Status': 1}
    monkeypatch.setattr(bm.ReadContext, 'resolve', resolve)
    snap = run(bm.build_bom('create', 'NEW', '1', {'lines': [{'name': 'X', 'product': {'name': 'P', 'revision': '1'}, 'quantity': 2, 'unit': 'EACH', 'issue_control': 6}]}))
    assert snap['body']['materialList'][0]['product']['useROR'] is False
    assert snap['body']['materialList'][0]['issueControl'] == 6
    assert len(snap['dependencies']) == 2
    with pytest.raises(ValueError): run(bm.reference_payload('product', {'name': 'P', 'use_ror': True}, []))


def test_revise_and_freeze_guards(fixture):
    snap = run(bm.build_bom('revise', 'B', '2', {}, '1'))
    assert snap['method'] == 'PATCH' and snap['body']['isRevOfRcd'] is True
    assert snap['expected']['MaterialList'] == bm.stable(fixture['MaterialList'])
    assert len(snap['body']['materialList']) == 2
    assert 'materialList-Expanded' not in snap['body']
    assert snap['body']['materialList'][0]['qtyRequired'] == 2
    assert snap['body']['description'] == 'original'
    fixture['IsRevOfRcd'] = False
    with pytest.raises(ValueError): run(bm.build_bom('revise', 'B', '2', {}, '1'))
    fixture['IsFrozen'] = True
    with pytest.raises(ValueError): run(bm.build_bom('update', 'B', '1', {'notes': 'x'}))


def test_product_binding_preserves_other_config(fixture, monkeypatch):
    product = {'InstanceID': 'P1', 'Name': 'P', 'Revision': '1', 'IsFrozen': False, 'BOM': None, 'StdStartQty': 12}
    monkeypatch.setattr(bm, 'read_product', AsyncMock(return_value=product))
    snap = run(bm.build_binding('P', '1', 'B', '1'))
    assert set(snap['body']) == {'bom'} and snap['body']['bom']['revision'] == '1'
    assert snap['expected']['StdStartQty'] == 12
    product['BOM'] = {'Name': 'B', 'Revision': '1', 'UseROR': False}
    snap = run(bm.build_binding('P', '1', action='unbind'))
    assert snap['body'] == {'bom': None} and 'bom:b' in snap['resources']


def test_attempt_not_replayed_ack_and_readback(fixture, monkeypatch):
    class Response:
        status_code = 200
        text = '{"value":"B updated"}'
        def json(self): return json.loads(self.text)
    client = type('Client', (), {'request': AsyncMock(return_value=Response())})()
    monkeypatch.setattr(bm, 'get_client', lambda: client)
    preview = decode(bm.prepare_bom_change('update', 'B', '1', {'description': 'new'}))
    args = {k: preview[k] for k in ('plan_id', 'plan_digest')}
    assert 'original' in plan_preview([{'function': {'name': 'execute_bom_change', 'arguments': json.dumps(args)}}])
    assert decode(bm.execute_bom_change(**args))['state'] == 'unknown'
    assert 'error' in decode(bm.execute_bom_change(**args))
    assert 'error' in decode(bm.prepare_bom_change('update', 'B', '1', {'notes': 'next'}))
    assert decode(bm.reconcile_bom_change(**args))['verified_success'] is False
    fixture['Description'] = 'new'
    assert decode(bm.reconcile_bom_change(**args))['verified_success'] is True
    assert client.request.await_count == 1
    assert decode(bm.get_bom_change_plan(args['plan_id']))['state'] == 'verified_applied'


def test_stale_plan_and_environment_rejected(fixture, monkeypatch):
    client = type('Client', (), {'request': AsyncMock()})()
    monkeypatch.setattr(bm, 'get_client', lambda: client)
    plan = decode(bm.prepare_bom_change('update', 'B', '1', {'notes': 'next'}))
    fixture['Description'] = 'external change'
    assert 'error' in decode(bm.execute_bom_change(plan['plan_id'], plan['plan_digest']))
    client.request.assert_not_awaited()
    monkeypatch.setattr(bm, 'CAMSTAR_BASE_URL', 'https://other/')
    assert 'error' in decode(bm.execute_bom_change(plan['plan_id'], plan['plan_digest']))


def test_missing_response_never_proves_success(fixture, monkeypatch):
    client = type('Client', (), {'request': AsyncMock(side_effect=TimeoutError('lost'))})()
    monkeypatch.setattr(bm, 'get_client', lambda: client)
    plan = decode(bm.prepare_bom_change('update', 'B', '1', {'notes': 'x'})); args = {k: plan[k] for k in ('plan_id','plan_digest')}
    decode(bm.execute_bom_change(**args)); fixture['Notes'] = 'x'
    assert decode(bm.reconcile_bom_change(**args))['verified_success'] is False
    assert 'error' in decode(bm.execute_bom_change(**args))


def test_crash_recovery_and_immutable_original_attempt(fixture):
    plan = decode(bm.prepare_bom_change('update', 'B', '1', {'notes': 'x'})); args = {k: plan[k] for k in ('plan_id','plan_digest')}
    audit = dict(action='recover', evidence_reference='audit/1', review_note='operator verified terminated worker')
    assert 'error' in decode(bm.review_bom_change(**args, **audit))
    with bm.db_connect() as db:
        db.execute("UPDATE plans SET state='in_flight',outcome=?", (json.dumps({'original': 'kept'}),))
        db.execute('INSERT INTO attempts VALUES (?,?,?,NULL)', (plan['plan_id'], 'attempt', time.time()))
    recovered = decode(bm.review_bom_change(**args, **audit))
    assert recovered.get('state') == 'unknown', recovered
    audit.update(action='resolve', conclusion='not_applied')
    assert decode(bm.review_bom_change(**args, **audit))['state'] == 'human_not_applied'
    assert decode(bm.get_bom_change_plan(plan['plan_id']))['outcome']['original'] == 'kept'
    assert 'error' in decode(bm.execute_bom_change(**args))


def test_catalog_and_stable_transient_ids():
    assert TOOL_POLICIES['execute_bom_change'].always_confirm
    assert TOOL_POLICIES['review_bom_change'].always_confirm
    assert bm.stable({'Name': 'P','Revision': '1','InstanceId': 'a'}) == bm.stable({'Name': 'P','Revision': '1','InstanceId': 'b'})


def test_revise_rejects_unsupported_source_configuration(fixture):
    fixture['MaterialList'][0]['PhantomBill'] = {'Name': 'PH', 'Revision': '1'}
    with pytest.raises(ValueError, match='PhantomBill'): run(bm.build_bom('revise', 'B', '2', {}, '1'))


def test_revise_requires_old_source_unchanged(fixture, monkeypatch):
    plan = decode(bm.prepare_bom_change('revise', 'B', '2', {}, source_revision='1'))
    args = {k: plan[k] for k in ('plan_id','plan_digest')}
    with bm.db_connect() as db:
        db.execute("UPDATE plans SET state='unknown',outcome=?", (json.dumps({'http_status':200,'response':{'value':'B revised'}}),))
        db.execute('INSERT INTO attempts VALUES (?,?,?,?)', (plan['plan_id'],'attempt',time.time(),time.time()))
    new = copy.deepcopy(fixture); new.update(InstanceID='B2',Revision='2')
    fixture['IsRevOfRcd'] = False
    async def read(n, rev): return copy.deepcopy(new if rev == '2' else fixture)
    monkeypatch.setattr(bm,'read_bom',read)
    fixture['MaterialList'][0]['QtyRequired'] = 9
    result = decode(bm.reconcile_bom_change(**args))
    assert result['configuration_matches'] is True and result['source_version_preserved'] is False
    assert not result['verified_success']


def test_plan_pages_do_not_crop_stored_execution(fixture):
    fixture['MaterialList'] = [{'Name':str(i),'QtyRequired':1,'IssueControl':5} for i in range(30)]
    plan = decode(bm.prepare_bom_change('update','B','1',{'notes':'paged'}))
    assert len(plan['before']['MaterialList']) == 20
    assert not plan['pages']['before.MaterialList']['complete']
    page = decode(bm.get_bom_change_plan(plan['plan_id'],offset=20,limit=10))
    assert len(page['plan']['snapshot']['before']['MaterialList']) == 10
    with bm.db_connect() as db:
        original = json.loads(bm.load_row(db,plan['plan_id'])[1])['snapshot']
    assert len(original['before']['MaterialList']) == 30
    assert bm.digest(original) == plan['plan_digest']
