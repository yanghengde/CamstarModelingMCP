import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tests.test_mfgorder_tasks import env, ORDER, LIST
from tools import mfgorder_task_history as h


@pytest.fixture
def history_env(env, monkeypatch):
    data = {'Name':'MO','InstanceID':ORDER,'MfgOrderExecutedTask':[], 'MfgOrderExecutedTaskList':[]}
    result = {'complete':True,'data':data,'pages':{}}
    mock = AsyncMock(side_effect=lambda *a, **kw: json.dumps(result))
    monkeypatch.setattr(h, 'get_mfgorder_details', mock)
    return result, mock


def run():
    return json.loads(asyncio.run(h.get_mfgorder_task_history('MO')))


@pytest.mark.parametrize('number,label', [(0,'not_started'),(1,'in_progress'),(2,'failed'),(3,'complete')])
def test_distinct_modeling_enumeration(history_env, number, label):
    result, mock = history_env
    result['data']['MfgOrderExecutedTask'] = [{'Task':{'Name':'T','InstanceId':'0000000000000004'},
                                              'TaskState':number,'ExecutionCount':1}]
    out = run()
    assert out['complete'] and out['tasks'][0]['state'] == label
    assert not out['transaction_verified']
    assert out['record_scope'] == 'current_execution_records_not_transaction_audit'
    assert mock.call_args.args == (ORDER,)
    assert mock.call_args.kwargs == {'view':'executed_tasks','limit':200}


@pytest.mark.parametrize('bad', ['identity','missing','page','state','bool','count','row'])
def test_incomplete_history(history_env, bad):
    result, _ = history_env
    row = {'Task':{'Name':'T','InstanceId':'0000000000000004'},'TaskState':0,'ExecutionCount':0}
    result['data']['MfgOrderExecutedTask'] = [row]
    if bad == 'identity': result['data']['InstanceID'] = 'other'
    if bad == 'missing': del result['data']['MfgOrderExecutedTaskList']
    if bad == 'page': result['data']['MfgOrderExecutedTask@odata.nextLink'] = 'untrusted'
    if bad == 'state': row['TaskState'] = 4
    if bad == 'bool': row['TaskState'] = False
    if bad == 'count': row['ExecutionCount'] = True
    if bad == 'row': result['data']['MfgOrderExecutedTask'] = [None]
    assert not run()['complete']


def test_list_state_is_raw_not_task_enum(history_env):
    result, _ = history_env
    result['data']['MfgOrderExecutedTaskList'] = [{'MfgOrderTaskList':{'InstanceId':LIST},'MfgOrderTaskState':9}]
    out = run()
    assert out['complete'] and out['task_lists'][0]['MfgOrderTaskState'] == 9


def test_source_failure_preserved(history_env):
    result, _ = history_env
    result['error'] = 'denied'
    out = run()
    assert not out['complete'] and out['source_error']['error'] == 'denied'
