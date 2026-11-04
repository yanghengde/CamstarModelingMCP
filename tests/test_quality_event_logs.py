import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from tools import mcp, quality_event_logs as l, quality_plans as p, get_tool_func
from tests.test_quality_event_data import env as data_env, draft_env, submission_env, update_env, triage_env, DATA
from tests.test_quality_event_drafts import result,execute,snap,TOKEN
from tests.test_quality_event_triage import EVENT,OWNER,OTHER,echo

LOG='000000000000fa01'


def prepare(**kwargs):
    return result(l.prepare_quality_event_log_append('E','Org','Investigated root cause','Append investigation',**kwargs))


@pytest.fixture
def env(data_env,monkeypatch):
    s=data_env
    row=dict(EventLogId=LOG,EventDataId=DATA,CDOTypeId=8367,ChangeCount=1,Comments='Original',EmployeeId=OWNER,
        CommentTypeId=None,CreationDate='2026-10-29 09:00:00',CreationDateGMT='2026-10-29 01:00:00')
    s.update(logs=[row],log_posts=[],log_hook=None,log_mode=None,log_history=[])
    sql=l.q.sql_rows
    async def read(query,maximum):
        if 'FROM UpdateEventDetailsHistory s JOIN HistoryMainline h' in query:return copy.deepcopy(s['log_history'])
        if ' FROM EventLog WHERE ' in query:return copy.deepcopy(s['logs'])
        return await sql(query,maximum)
    monkeypatch.setattr(l.q,'sql_rows',AsyncMock(side_effect=read))
    old_request=l.e.request
    doc=json.loads(Path(__file__).parent.joinpath('fixtures/event_log_contract.json').read_text(encoding='utf-8'))
    async def request(method,path,**kw):
        if method=='GET':return json.dumps(doc)
        assert path=='/api/UpdateEventLogs/PerformEvent'
        assert kw['body']['event']=={'eventName':'Load'}
        assert 'execute' not in kw['params']
        raw=await old_request(method,'/api/UpdateEventData/PerformEvent',**kw)
        data=json.loads(raw)['data']
        data['User']={'InstanceId':s['actor']}
        data['EventLogDetails']=[]
        for r in s['logs']:
            data['EventLogDetails'].append(dict(CDOTypeId=8371,Comments=r['Comments'],EventLog={'InstanceId':r['EventLogId']},
                Employee={'Name':'Owner','InstanceId':r['EmployeeId']},CommentType=None if not r['CommentTypeId'] else dict(InstanceId=r['CommentTypeId'],Name='Investigation'),
                CreationDate=r['CreationDate'].replace(' ','T')+'+08:00',CreationDateGMT=r['CreationDateGMT'].replace(' ','T')+'Z'))
        if s['log_hook']:s['log_hook'](data)
        return json.dumps({'data':data})
    monkeypatch.setattr(l,'request',AsyncMock(side_effect=request))
    async def submit(method,url,**kw):
        assert url.endswith('/UpdateEventLogs') and kw['params']=={'execute':'true'}
        assert kw['headers']['Authorization']==TOKEN
        s['log_posts'].append(copy.deepcopy(kw['json']))
        if s['log_mode']=='timeout':raise TimeoutError(TOKEN)
        if s['log_mode']=='400':return SimpleNamespace(status_code=400,text=TOKEN)
        new=copy.deepcopy(row);new.update(EventLogId=OTHER,Comments=kw['json']['eventLogDetails'][-1]['comments'],CommentTypeId=(kw['json']['eventLogDetails'][-1]['commentType'] or {}).get('instanceId'))
        s['logs'].append(new)
        return SimpleNamespace(status_code=200,text=TOKEN)
    monkeypatch.setattr(s['client'],'request',AsyncMock(side_effect=submit))
    return s


def test_append_preserves_old_values_single_attempt_and_inspection(env):
    args=dict(event_name='E',organization_name='Org',event_instance_id=None)
    before=asyncio.run(l.source(args))
    asyncio.run(l.contract())
    asyncio.run(l.load(before,'MCP-QUAL:'+'0'*32,l.auth.capture_headers(l.SERVICE)))
    ready=prepare();assert ready.get('state')=='ready',ready
    body=ready['request_preview']['body'];assert len(body['eventLogDetails'])==2
    assert body['eventLogDetails'][0]['eventLog']['instanceId']==LOG
    assert body['eventLogDetails'][-1]==dict(comments='Investigated root cause',commentType=None)
    assert not env['log_posts']
    assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready) and len(env['log_posts'])==1
    proof=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert proof['preservation']['original_values_preserved'] and len(proof['preservation']['new_rows'])==1
    assert not proof['verified_success']
    assert TOKEN.encode() not in p.PLAN_DB.read_bytes()


@pytest.mark.parametrize('mode',['timeout','400'])
def test_uncertain_attempt_never_retries(env,mode):
    env['log_mode']=mode;ready=prepare();assert ready.get('state')=='ready',ready
    assert execute(ready)['state']=='unknown'
    assert 'error' in execute(ready) and len(env['log_posts'])==1


@pytest.mark.parametrize('mode',['content','author','date','missing','duplicate','extra','native_user'])
def test_load_must_match_complete_persistent_logs(env,mode):
    def hook(d):
        rows=d['EventLogDetails']
        if mode=='content':rows[0]['Comments']='changed'
        elif mode=='author':rows[0]['Employee']['InstanceId']=OTHER
        elif mode=='date':rows[0]['CreationDateGMT']='2026-10-30T01:00:00Z'
        elif mode=='missing':rows.clear()
        elif mode=='duplicate':rows.append(copy.deepcopy(rows[0]))
        elif mode=='extra':rows[0]['Unknown']='extension'
        elif mode=='native_user':d['User']={'InstanceId':OTHER}
    env['log_hook']=hook
    assert 'error' in prepare() and not env['log_posts']


def test_stale_plan_rejected_before_submission(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    env['logs'][0]['Comments']='Concurrent change'
    assert 'error' in execute(ready) and not env['log_posts']


@pytest.mark.parametrize('field,value',[('EmployeeId',None),('CreationDate',None),('CDOTypeId',1),('EventDataId',OTHER)])
def test_incomplete_original_provenance_rejected(env,field,value):
    env['logs'][0][field]=value
    assert 'error' in prepare() and not env['log_posts']


def test_original_loss_and_rewrite_detected(env):
    original=copy.deepcopy(env['logs']);changed=copy.deepcopy(original)
    changed[0]['Comments']='rewritten'
    assert not l.preservation(original,changed)['original_values_preserved']
    assert l.preservation(original,[])['missing_original_ids']==[LOG]


def test_tools_registered_and_transport_tampering_rejected(env):
    assert get_tool_func('prepare_quality_event_log_append')
    assert get_tool_func('get_quality_event_log_context')
    ready=prepare();assert ready.get('state')=='ready',ready
    saved=snap(ready);saved['body']['eventLogDetails'].pop(0)
    with pytest.raises(ValueError):l.validate_transport(saved)


def test_target_transaction_ban_checked(env):
    env['disallowed']=[dict(EventId=EVENT,FieldId=25978,Sequence=0,DisallowedTxns=8761)]
    assert 'error' in prepare() and not env['log_posts']


def test_unknown_blocks_another_append_plan(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    execute(ready)
    next_plan=prepare();assert next_plan.get('state')=='ready',next_plan
    assert 'error' in execute(next_plan) and len(env['log_posts'])==1


def test_full_list_limit_reserves_one_slot_for_append(env):
    old=env['logs'][0]
    env['logs']=[dict(old,EventLogId=f'{2000+i:016x}') for i in range(100)]
    assert 'error' in prepare() and not env['log_posts']


@pytest.mark.parametrize('wrong',[False,True])
def test_history_candidate_checks_original_transaction(env,wrong):
    ready=prepare();assert ready.get('state')=='ready',ready
    execute(ready)
    env['log_history']=[dict(UpdateEventDetailsHistoryId='000000000000fb01',CDOTypeId=8754,HistoryId=EVENT,
        HistoryMainlineId='000000000000fb02',TxnId='000000000000fb03',ChangeCount=1,TxnType=8764 if wrong else 8761,
        Comments=ready['request_preview']['body']['comments'],ReversalStatus=0)]
    proof=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    if wrong:assert 'error' in proof
    else:assert len(proof['history_candidates'])==1 and not proof['verified_success']


def test_data_relink_never_follows_replacement(env):
    ready=prepare();assert ready.get('state')=='ready',ready
    execute(ready);env['extra']['EventDataId']=OTHER
    proof=result(p.inspect_quality_transaction(ready['plan_id'],ready['plan_digest']))
    assert not proof['complete'] and proof['reason']=='original_data_link_changed'


def test_mcp_closed_schema_and_context_to_plan(env):
    async def flow():
        async with Client(mcp) as client:
            registry={t.name:t for t in await client.list_tools()}
            schema=registry['prepare_quality_event_log_append'].input_schema
            assert set(schema['properties'])=={'event_name','organization_name','log_text','comments','event_instance_id','comment_type_name','comment_type_instance_id'}
            read=await client.call_tool('get_quality_event_log_context',dict(event_name='E',organization_name='Org'))
            assert json.loads(read.content[0].text)['complete']
            reply=await client.call_tool('prepare_quality_event_log_append',dict(event_name='E',organization_name='Org',log_text='Reviewed',comments='Review'))
            assert json.loads(reply.content[0].text)['state']=='ready'
    asyncio.run(flow())
    assert not env['log_posts']


TYPE_ID = '000000000000fc01'


@pytest.fixture
def type_env(env, monkeypatch):
    env['type_root'] = dict(InstanceID=TYPE_ID, Name='Investigation', IsFrozen=False)
    env['type_version'] = 1
    env['type_cdo'] = 8414
    env['type_matches'] = 1
    env['type_reads'] = 0
    env['type_change_on_read'] = None
    original_context = l.e.t.ReadContext
    original_sql = l.q.sql_rows

    class Context(original_context):
        async def get(self, entity, key=None, params=None):
            if entity != 'CommentTypes':
                return await super().get(entity, key, params)
            if key is None:
                assert params['$top'] == 2
                return {'value': [copy.deepcopy(env['type_root'])] * env['type_matches']}
            return copy.deepcopy(env['type_root'])

    async def sql(query, maximum):
        if ' FROM CommentType WHERE ' in query:
            env['type_reads'] += 1
            if env['type_change_on_read'] == env['type_reads']:
                env['type_version'] += 1
            return [dict(CommentTypeId=TYPE_ID, CDOTypeId=env['type_cdo'], ChangeCount=env['type_version'])]
        return await original_sql(query, maximum)

    monkeypatch.setattr(l.e.t, 'ReadContext', Context)
    monkeypatch.setattr(l.q, 'sql_rows', AsyncMock(side_effect=sql))
    return env


def test_typed_log_plan_execution_and_matching_evidence(type_env):
    ready = prepare(comment_type_name='Investigation', comment_type_instance_id=TYPE_ID)
    assert ready.get('state') == 'ready', ready
    saved = snap(ready)
    assert saved['body']['eventLogDetails'][-1]['commentType'] == dict(name='Investigation', instanceId=TYPE_ID)
    assert 'modeling:CommentTypes:id:' + TYPE_ID in saved['resource_reads']
    assert saved['body']['eventLogDetails'][0]['commentType'] is None
    assert execute(ready)['state'] == 'unknown'
    proof = result(p.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert proof['matching_new_log_ids'] == [OTHER]
    assert not proof['verified_success']


@pytest.mark.parametrize('mode', ['missing', 'ambiguous', 'frozen', 'wrong_id', 'wrong_name', 'subtype', 'changing'])
def test_invalid_type_selection_blocks_business_attempt(type_env, mode):
    args = dict(comment_type_name='Investigation')
    if mode == 'missing':type_env['type_matches'] = 0
    elif mode == 'ambiguous':type_env['type_matches'] = 2
    elif mode == 'frozen':type_env['type_root']['IsFrozen'] = True
    elif mode == 'wrong_id':args['comment_type_instance_id'] = OTHER
    elif mode == 'wrong_name':args['comment_type_name'] = 'Other name'
    elif mode == 'subtype':type_env['type_cdo'] = 8415
    elif mode == 'changing':type_env['type_change_on_read'] = 2
    assert 'error' in prepare(**args)
    assert not type_env['log_posts']


def test_type_version_change_invalidates_ready_plan(type_env):
    ready = prepare(comment_type_name='Investigation')
    assert ready.get('state') == 'ready', ready
    type_env['type_version'] += 1
    assert 'error' in execute(ready)
    assert not type_env['log_posts']


def test_id_without_explicit_name_rejected(env):
    assert 'error' in prepare(comment_type_instance_id=TYPE_ID)
    assert not env['log_posts']


def test_typed_log_context_and_mcp_prepare(type_env):
    async def flow():
        async with Client(mcp) as client:
            args = dict(event_name='E', organization_name='Org', comment_type_name='Investigation')
            reply = await client.call_tool('get_quality_event_log_context', args)
            context = json.loads(reply.content[0].text)
            assert context['selected_comment_type']['InstanceID'] == TYPE_ID
            reply = await client.call_tool('prepare_quality_event_log_append', args | dict(log_text='Review', comments='Reviewed'))
            assert json.loads(reply.content[0].text)['state'] == 'ready'
    asyncio.run(flow())
    assert not type_env['log_posts']


def test_existing_classified_logs_are_preserved(type_env):
    type_env['logs'][0]['CommentTypeId'] = TYPE_ID
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    items = ready['request_preview']['body']['eventLogDetails']
    assert items[0]['commentType']['instanceId'] == TYPE_ID
    assert items[-1]['commentType'] is None


def test_category_frozen_after_prepare_blocks_submission(type_env):
    ready = prepare(comment_type_name='Investigation')
    assert ready.get('state') == 'ready', ready
    type_env['type_root']['IsFrozen'] = True
    assert 'error' in execute(ready)
    assert not type_env['log_posts']


def test_type_payload_tampering_rejected(type_env):
    ready = prepare(comment_type_name='Investigation')
    assert ready.get('state') == 'ready', ready
    saved = snap(ready)
    saved['body']['eventLogDetails'][-1]['commentType']['instanceId'] = OTHER
    with pytest.raises(ValueError):
        l.validate_transport(saved)


def test_mismatched_new_record_category_is_visible(type_env):
    ready = prepare(comment_type_name='Investigation')
    assert ready.get('state') == 'ready', ready
    execute(ready)
    type_env['logs'][-1]['CommentTypeId'] = None
    proof = result(p.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))
    assert proof['matching_new_log_ids'] == []
    assert len(proof['preservation']['new_rows']) == 1
    assert not proof['verified_success']
