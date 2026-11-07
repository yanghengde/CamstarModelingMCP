import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import evaluate_multiturn as ev
from scripts.multiturn_cases import CASES
from agent import langgraph_runtime as runtime,memory
from tools import mcp


@pytest.fixture(scope='module')
def schemas():
    return {t.name:t.parameters for t in asyncio.run(mcp.list_tools())}


def test_dataset_has_unique_multiturn_cases_and_required_risks():
    assert len(CASES)==32 and len({c['id'] for c in CASES})==32
    assert all(len(c['turns'])>=2 for c in CASES)
    assert all(isinstance(t['expect'],dict) for c in CASES for t in c['turns'])
    text=json.dumps(CASES,ensure_ascii=False)
    for word in ['超时','取消','分组','false','工单','差异原因','升版','版本','证据']:
        assert word in text


def test_only_llm_chat_transport_is_allowed():
    guard=ev.ModelOnlyNetwork('https://llm.example/v1')
    guard.check(httpx.Request('POST','https://llm.example/v1/chat/completions'))
    assert guard.allowed_requests==1
    for method,url in [('POST','https://mes.example/Shopfloor/api/CollectData'),
                       ('POST','https://llm.example/v1/files'),
                       ('POST','http://llm.example/v1/chat/completions'),
                       ('GET','https://llm.example/v1/chat/completions'),
                       ('POST','https://llm.example:9000/v1/chat/completions')]:
        with pytest.raises(PermissionError):guard.check(httpx.Request(method,url))
    assert len(guard.blocked)==5


def test_fixture_never_falls_through_to_business_tools(schemas,monkeypatch):
    world=ev.FixtureWorld(CASES[0],schemas)
    def fail(*args,**kwargs):raise AssertionError('must not call any business function')
    monkeypatch.setattr(ev,'real_tool',fail)
    name='download_document_file'
    # An unknown or out-of-scope tool remains blocked even if its schema passes.
    world.schemas={name:{'type':'object'}}
    response=json.loads(asyncio.run(world.dispatch(name,{})))
    assert response['error'] and world.guard_violations[0]['code']=='unimplemented_fixture_tool'


def test_invalid_schema_recorded_before_fixture_dispatch(schemas):
    world=ev.FixtureWorld(CASES[0],schemas)
    response=json.loads(asyncio.run(world.dispatch('collect_container_data',{'plan_id':'fake'})))
    assert response['error'] and world.trace[0]['schema_valid'] is False
    assert world.executions==[]


def test_fabricated_digest_cannot_execute_fixture_plan(schemas):
    world=ev.FixtureWorld(CASES[0],schemas)
    plan=world.new_plan('collection',{'container_name':'SN-EVAL-001','values':{'温度':25}})
    result=json.loads(asyncio.run(world.dispatch('collect_container_data',{'plan_id':plan['plan_id'],'plan_digest':'wrong'})))
    assert not result['submitted'] and not world.executions


def test_grader_detects_wrong_values_and_readonly_execution(schemas):
    world=ev.FixtureWorld(CASES[0],schemas)
    plan=world.new_plan('collection',{'container_name':'SN-EVAL-001','values':{'温度':25}})
    args={k:plan[k] for k in ('plan_id','plan_digest')}
    asyncio.run(world.dispatch('collect_container_data',args))
    issues=ev.grade_turn({'stage':'submitted','values':{'温度':30},'readonly':True},world,world.trace,[], '',[])
    assert {i['code'] for i in issues}>={'wrong_planned_or_submitted_values','write_executed_for_read_query'}
    assert all(i['severity']=='critical' for i in issues)


def test_grader_detects_invalid_proposal_even_if_runtime_blocks_it(schemas):
    world=ev.FixtureWorld(CASES[0],schemas)
    calls=[{'function':{'name':'collect_container_data','arguments':'{"plan_id":"made-up","plan_digest":"made-up"}'}}]
    issues=ev.grade_turn({'readonly':True},world,[],calls,'',[])
    assert {i['code'] for i in issues}=={'invented_or_wrong_plan','write_proposed_for_read_query'}


def test_repeated_error_scoring_is_json_serializable(schemas):
    world=ev.FixtureWorld(CASES[28],schemas)
    for _ in range(2):asyncio.run(world.dispatch('get_container_collection_history',{'container_name':'SN-EVAL-001'}))
    issues=ev.grade_turn({'max_same_call':1},world,world.trace,[],'',[])
    assert any(i['code']=='same_failed_call_repeated' for i in issues)
    json.dumps(issues)


class ReplayClient:
    def __init__(self,steps):
        self.steps=iter(steps);self.count=0
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self,**kwargs):
        self.count+=1;step=next(self.steps)
        step=step(ev.CURRENT.get()) if callable(step) else step
        calls=[]
        if 'tool' in step:
            calls=[SimpleNamespace(index=0,id=f'call-{self.count}',function=SimpleNamespace(name=step['tool'],arguments=json.dumps(step.get('args',{}),ensure_ascii=False)))]
        delta=SimpleNamespace(content=step.get('content',''),tool_calls=calls)
        async def stream():
            yield SimpleNamespace(usage=None,choices=[SimpleNamespace(delta=delta,finish_reason='tool_calls' if calls else 'stop')])
        return stream()


def active_call(name):
    def step(world):
        plan=world.last_preview
        return {'tool':name,'args':{k:plan[k] for k in ('plan_id','plan_digest')}}
    return step


def execute_replay(tmp_path,monkeypatch,schemas,case,client):
    monkeypatch.setattr(runtime,'LANGGRAPH_CHECKPOINT_DB',str(tmp_path/'checkpoints.sqlite'))
    monkeypatch.setattr(memory,'SESSIONS_DIR',str(tmp_path/'sessions'))
    monkeypatch.setattr(runtime,'get_tool_func',lambda name: lambda **kwargs:ev.CURRENT.get().dispatch(name,kwargs))
    monkeypatch.setattr(runtime,'plan_preview',lambda calls:ev.CURRENT.get().summary(calls))
    monkeypatch.setattr(runtime,'record_perf',lambda *a,**k:None)
    monkeypatch.setattr(runtime,'record_tool_outcome',lambda **k:None)
    monkeypatch.setattr(runtime,'build_runtime_experience_context',lambda *a,**k:'')
    async def run():
        await runtime.init_langgraph_runtime(client,[])
        try:return await ev.run_case(case,schemas,1,15)
        finally:await runtime.close_langgraph_runtime()
    result=asyncio.run(run())
    return result


@pytest.mark.parametrize('case_id',['D06','D09'])
def test_actual_graph_approval_resume_and_cancel_without_live_model(tmp_path,monkeypatch,schemas,case_id):
    case=next(c for c in CASES if c['id']==case_id)
    steps=[{'tool':'preview_container_data_collection','args':{'container_name':'SN-EVAL-001','name':'质检采集','revision':'1','values':{'温度':25}}},active_call('collect_container_data')]
    if case_id=='D06':steps.extend([active_call('verify_collection_submission'),{'content':'已核实本次采集。'}])
    steps.extend([{'tool':'get_container_collection_history','args':{'container_name':'SN-EVAL-001'}},{'content':'这是该容器的普通采集历史。'}])
    result=execute_replay(tmp_path,monkeypatch,schemas,case,ReplayClient(steps))
    assert result['passed'],result
    assert result['simulated_execution_count']==(1 if case_id=='D06' else 0)
    assert result['turns'][0]['approval_pending']
    assert not result['turns'][-1]['approval_pending']


class RevisedPlanClient(ReplayClient):
    def __init__(self):super().__init__([])

    async def create(self,**kwargs):
        world=ev.CURRENT.get()
        if world.turn in (1,2):
            value=25 if world.turn==1 else 30
            if not world.last_preview or world.last_preview['arguments']['values']!={'温度':value}:
                step={'tool':'preview_container_data_collection','args':{'container_name':'SN-EVAL-001','name':'质检采集','revision':'1','values':{'温度':value}}}
            else:step=active_call('collect_container_data')(world)
        else:step={'content':'已单次提交，结果unknown，不自动重试。'}
        self.steps=iter([step])
        return await super().create(**kwargs)


def test_pending_parameter_correction_never_executes_old_values(tmp_path,monkeypatch,schemas):
    case=next(c for c in CASES if c['id']=='D08')
    result=execute_replay(tmp_path,monkeypatch,schemas,case,RevisedPlanClient())
    if os.getenv('PENDING_REPLAY_REPORT'):
        Path(os.environ['PENDING_REPLAY_REPORT']).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    assert result['turns'][0]['approval_pending']
    assert result['passed'],result
    assert result['simulated_execution_count']==1
    executed=[c for t in result['turns'] for c in t['tool_calls'] if c['tool']=='collect_container_data']
    final_preview=[c for t in result['turns'] for c in t['tool_calls'] if c['tool']=='preview_container_data_collection'][-1]
    assert final_preview['arguments']['values']=={'温度':30}


def test_multiturn_budget_preserves_plan_ids_and_revised_approval(tmp_path, monkeypatch, schemas):
    from agent.context_budget import budget_history
    observed = []
    def bounded(messages):
        # Simulate long older narration, without changing durable graph history.
        prefix = messages[:1] if messages and messages[0].get('role') == 'system' else []
        remainder = messages[len(prefix):]
        seeded = prefix + [{'role':'user','content':'旧的说明'},
                           {'role':'assistant','content':'已结束的解释。'*5000},
                           {'role':'user','content':'另一个已完成的话题'}] + remainder
        result, metrics = budget_history(seeded, budget=16000)
        assert metrics['history_removed_messages'] >= 1
        observed.append(metrics)
        for plan_id in ev.CURRENT.get().plans:
            assert plan_id in json.dumps(result, ensure_ascii=False)
        return result, metrics
    monkeypatch.setattr(runtime, 'budget_history', bounded)
    case=next(c for c in CASES if c['id']=='D08')
    result=execute_replay(tmp_path,monkeypatch,schemas,case,RevisedPlanClient())
    assert result['passed'] and observed
    assert result['simulated_execution_count']==1
    assert result['turns'][0]['approval_pending'] and result['turns'][1]['approval_pending']


@pytest.mark.parametrize('case_id',['D10','D11','D14'])
def test_pending_gate_accepts_clear_confirmation_or_cancels_for_new_task(tmp_path,monkeypatch,schemas,case_id):
    case=next(c for c in CASES if c['id']==case_id)
    steps=[{'tool':'preview_container_data_collection','args':{'container_name':'SN-EVAL-001','name':'质检采集','revision':'1','values':{'温度':25}}},active_call('collect_container_data')]
    if case_id=='D10':steps.extend([{'tool':'get_container_collection_history','args':{'container_name':'SN-EVAL-001'}},{'content':'这是采集历史，未提交。'}])
    elif case_id=='D11':steps.append({'content':'单次提交结束，当前unknown，等待核实。'})
    else:steps.extend([{'tool':'list_products','args':{'filter_expr':"Name eq 'P-EVAL' and Revision eq '1'"}},
                      {'tool':'get_product_process','args':{'product_id':ev.PRODUCT}},{'content':'这是产品P-EVAL的1版实际工艺链。'}])
    result=execute_replay(tmp_path,monkeypatch,schemas,case,ReplayClient(steps))
    assert result['passed'],result
    assert result['simulated_execution_count']==(1 if case_id=='D11' else 0)
    assert result['turns'][0]['approval_pending']
    assert not result['turns'][-1]['approval_pending']


def test_elliptical_verification_exposes_tool_without_enabling_history_writes():
    from agent.tool_routing import select_tools
    from core.tool_catalog import TOOL_POLICIES
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    base=[{'role':'user','content':'查询采集计划的执行状态'},{'role':'assistant','content':'结果unknown。'}]
    for request,writes in [('帮我核实这次提交，不能重试',True),('查看刚才的核实历史',False),('核实这次结果',True)]:
        selected=select_tools(tools,base+[{'role':'user','content':request}])
        assert ('verify_collection_submission' in {t['function']['name'] for t in selected})==writes
    base=[{'role':'user','content':'给SN-EVAL-001用质检采集1版提交温度25，先确认'}]
    for request,writes in [('温度改成30，重新展示计划',True),('刚才25写错了，改为26.5，重做计划让我确认',True),('查看哪条历史把温度改成了30',False)]:
        selected=select_tools(tools,base+[{'role':'user','content':request}])
        assert ('collect_container_data' in {t['function']['name'] for t in selected})==writes


def test_fixed_fixture_name_filter_and_ambiguous_order(schemas):
    world=ev.FixtureWorld(next(c for c in CASES if c['id']=='D28'),schemas)
    rows=json.loads(asyncio.run(world.dispatch('list_data_collections',{'kind':'point','name_contains':'质检采集'})))
    assert rows['rows']==[]
    order=json.loads(asyncio.run(world.dispatch('get_mfgorder_progress',{'mfgorder_name':'MO-EVAL-001'})))
    assert {c['name'] for c in order['containers']}=={'SN-EVAL-001','SN-EVAL-002'}


def test_condition_for_quality_decision_is_not_a_success_claim(schemas):
    world=ev.FixtureWorld(CASES[0],schemas)
    assert ev.grade_turn({'no_quality_success':True},world,[],[],'要判定合格，还缺以下条件；无法作出结论。',[])==[]
    assert ev.grade_turn({'no_quality_success':True},world,[],[],'本次已判定合格，可以放行。',[])


def test_configuration_fixture_checks_real_point_field_contract(schemas):
    world=ev.FixtureWorld(CASES[0],schemas)
    args={'kind':'user','action':'update','name':'质检采集','revision':'1','changes':{'points':[{'name':'温度','upper_limit':95}]}}
    response=json.loads(asyncio.run(world.dispatch('prepare_data_collection_definition',args)))
    assert response.get('error') and not world.last_config
    assert ev.grade_turn({'stage':'config_preview'},world,world.trace,[],'',[])


def test_collection_tool_schema_documents_nested_types(schemas):
    from jsonschema import Draft202012Validator
    schema=schemas['prepare_data_collection_definition']
    args={'kind':'user','action':'update','name':'质检采集','revision':'1','changes':{'points':[{'name':'温度','upper_limit':'95','required':True}]}}
    assert not list(Draft202012Validator(schema).iter_errors(args))
    args['changes']['points'][0]['upper_limit']=95
    assert list(Draft202012Validator(schema).iter_errors(args))
    args['changes']['points'][0]['upper_limit']='95'
    args['changes']['points'][0]['required']='true'
    assert list(Draft202012Validator(schema).iter_errors(args))
