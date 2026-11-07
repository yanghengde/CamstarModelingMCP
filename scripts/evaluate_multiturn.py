"""Actual configured LLM + production LangGraph; synthetic tools, no MES access.

Run in a separate process. All business tools terminate at FixtureWorld, even
unknown tools; only the configured LLM endpoint may use an HTTP transport.
Approval/checkpoint/routing run normally. Business results and plan display are
fixtures, not a live MES or real business-journal acceptance test.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

import httpx
from jsonschema import Draft202012Validator

from agent import langgraph_runtime as runtime,memory
from core.tool_catalog import TOOL_POLICIES
from scripts.multiturn_cases import CASES,HOLDOUT_CASES,UNKNOWN_PLAN
from tools import mcp,get_tool_func as real_tool
from config import LLM_BASE_URL,LLM_MODEL

CURRENT:ContextVar=ContextVar('multiturn_evaluation_case')
CID='0000000000000101';CID2='0000000000000102';DID='0000000000000201';GID='0000000000000202'
SPEC='0000000000000301';PRODUCT='0000000000000401';POINT='0000000000000501'
WORKFLOW='0000000000000701';OPERATION='0000000000000801';ORDER='0000000000000901'
MUTATIONS={'collect_container_data','execute_data_collection_change','issue_components'}
PLAN_READS={'get_collection_submission_plan','get_data_collection_change_plan','get_component_issue_plan'}
DISCOVERY={'get_capabilities','load_tool_group','get_mcp_server_status'}


def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def dump(value):return json.dumps(value,ensure_ascii=False)
def clone(value):return json.loads(dump(value))


class FixtureWorld:
    def __init__(self,case,schemas):
        self.case=case;self.schemas=schemas;self.turn=0;self.trace=[];self.plans={};self.model_calls=[]
        self.guard_violations=[];self.last_preview=None;self.last_config=None;self.last_binding=None;self.last_issue=None
        self.executions=[];self.verified=[];self.unknown_checks=[];self.plan_counter=0
        if case.get('fixture',{}).get('unknown'):
            self.new_plan('collection',{'container_name':'SN-EVAL-001','values':{'温度':25},'name':'质检采集','revision':'1'},key=UNKNOWN_PLAN,state='unknown')

    def new_plan(self,kind,args,key=None,state='ready'):
        self.plan_counter+=1;key=key or hashlib.sha256(f"{self.case['id']}:{self.plan_counter}".encode()).hexdigest()[:32]
        body={'arguments':clone(args)}
        if kind=='collection':
            from tools.collection_submission import serialize
            grouped=args.get('collection_kind')=='point'
            typed={'温度':(POINT,9),'判定':('0000000000000502',7),'测量时间':('0000000000000503',6)}
            details=[{'dataPoint':{'name':name,'instanceId':typed[name][0]},'dataValue':serialize({'data_type':typed[name][1],'value':value})} for name,value in args.get('values',{}).items()] if not grouped else []
            groups=[{'group':{'name':'尺寸','instanceId':'0000000000000601'},'serviceDetails':[
                {'point':{'name':'厚度','instanceId':'0000000000000602'},'samples':[
                    {'cellName':str(i+1),'dataValue':str(v)} for i,v in enumerate(args.get('values',{}).get('尺寸',{}).get('厚度',[]))]}]}] if grouped else []
            body={'container':{'name':args.get('container_name'),'instanceId':CID2 if args.get('container_name')=='SN-EVAL-002' else CID},
                  'dataCollectionDef':{'name':'尺寸采样' if grouped else '质检采集','revision':'1','instanceId':GID if grouped else DID},
                  'parametricData':{'@odata.type':'#shopfloor.DPCollection','dpCollectionGroups':groups} if grouped else {'@odata.type':'#shopfloor.DataPointSummary','dataPointDetails':details},
                  'comments':'MCP-COL:'+key}
        plan={'plan_id':key,'plan_digest':fingerprint(body),'kind':kind,'state':state,'body':body,'arguments':clone(args),
              'submitted':False,'executable':True,'confirmation_required':True,'request_preview':{'body':body}}
        self.plans[key]=plan
        return clone(plan)

    def get_plan(self,args):
        plan=self.plans.get(args.get('plan_id'))
        if not plan:return None
        if 'plan_digest' in args and args['plan_digest']!=plan['plan_digest']:return None
        return plan

    def summary(self,calls):
        result=[]
        for call in calls:
            function=call.get('function') or {}
            try:args=json.loads(function.get('arguments') or '{}')
            except (ValueError,TypeError):args={}
            plan=self.get_plan(args)
            if function.get('name') in MUTATIONS and not plan:
                self.guard_violations.append({'code':'invented_or_wrong_plan','turn':self.turn,'tool':function.get('name')})
            result.append({'tool':function.get('name'),'arguments':args,'plan':clone(plan) if plan else None})
        return '评测夹具中的待确认计划（未调用MES）：'+dump(result)

    def definition(self,kind='user'):
        root={'Name':'尺寸采样' if kind=='point' else '质检采集','Revision':'1','InstanceID':GID if kind=='point' else DID,
              'IsFrozen':False,'IsRevOfRcd':True}
        points=[{'Name':'温度','InstanceID':POINT,'DataType':9,'IsRequired':True,'LowerLimit':'0','UpperLimit':'100','UOM':None},
                {'Name':'判定','InstanceID':'0000000000000502','DataType':7,'IsRequired':False},
                {'Name':'测量时间','InstanceID':'0000000000000503','DataType':6,'IsRequired':False}]
        groups=[{'Name':'尺寸','InstanceID':'0000000000000601','MinimumSamples':2,'Points':[
            {'Name':'厚度','InstanceID':'0000000000000602','DataType':9,'IsRequired':True,'MinimumSamples':2}]}]
        return {'complete':True,'constraints_complete':False,'definition':root,'raw_items':groups if kind=='point' else points,
                'groups':groups if kind=='point' else [],'points':points if kind=='user' else []}

    def collection_history(self,args,grouped=False):
        if self.case.get('fixture',{}).get('history_error'):return {'error':'模拟查询超时，本轮没有取得历史','complete':False}
        offset=args.get('offset',0);limit=args.get('limit',20)
        base=[{'TxnId':f'{i+700:016x}','ContainerId':CID if args.get('container_name')=='SN-EVAL-001' else CID2,
               'DataName':'温度','DataValue':str(20+i),'DataType':9,'TxnDate':f'2026-10-25T12:0{3-i}:00+08:00',
               'HistoryMainlineId':f'{i+700:016x}','DataPointHistoryDetailId':f'{i+1700:016x}',
               'DataCollectionDefId':DID,'DataPointId':POINT,'LowerLimit':'0','UpperLimit':'100','IsLimitViolation':False} for i in range(4)]
        more=offset+limit<len(base)
        return {'complete':False,'page_complete':True,'rows':base[offset:offset+limit],'has_more':more,
                'next_offset':offset+limit if more else None,'scope':'grouped_data_collection_history' if grouped else 'user_data_collection_history',
                'submission_outcome':'unknown','quality_verdict':None}

    def structure(self):
        spec={'Name':'检验工序','Revision':'1','InstanceID':SPEC,'Operation':{'Name':'检验','InstanceId':OPERATION}}
        operation={'Name':'检验','InstanceID':OPERATION}
        step={'name':'检验','step_type':1,'spec':{'Name':'检验工序','Revision':'1','InstanceId':SPEC},
              'resolved_spec_id':SPEC,'resolved_operation_id':OPERATION,'is_first_step':True,'paths':[],
              'operation':{'Name':'检验','InstanceId':OPERATION}}
        node={'workflow':{'name':'W-EVAL','revision':'1','instance_id':WORKFLOW},'steps':[step],
              'standard_route':['检验'],'standard_route_complete':True,'steps_complete':True,'warnings':[]}
        return {'complete':True,'product':{'Name':'P-EVAL','Revision':'1','InstanceID':PRODUCT},'root_workflow_id':WORKFLOW,
                'workflows':{WORKFLOW:node},'specs':{SPEC:spec},'operations':{OPERATION:operation},'warnings':[],'subworkflow_links':[]}

    async def dispatch(self,name,args):
        record={'turn':self.turn,'tool':name,'arguments':clone(args),'effect':TOOL_POLICIES[name].effect if name in TOOL_POLICIES else 'unknown'}
        self.trace.append(record)
        schema=self.schemas.get(name)
        errors=list(Draft202012Validator(schema).iter_errors(args)) if schema else []
        if errors or schema is None:
            record['schema_valid']=False
            result={'error':'工具名或参数不符合真实MCP schema','details':[e.message for e in errors][:3]}
        else:
            record['schema_valid']=True
            try:result=await self._dispatch(name,args)
            except (ValueError,TypeError,KeyError) as error:result={'error':'固定夹具校验拒绝：'+str(error)}
        record['result']=clone(result)
        return dump(result)

    async def _dispatch(self,name,args):
        if name in DISCOVERY:
            if name=='get_mcp_server_status':return {'status':'evaluation_fixture','mes_connected':False}
            return json.loads(await real_tool(name)(**args))
        if 'container_name' in args:
            cid={'SN-EVAL-001':CID,'SN-EVAL-002':CID2}.get(args['container_name'])
            if not cid:return {'error':'评测数据中不存在此容器，不能猜测身份。','identity_mismatch':True}
            if args.get('container_instance_id') not in (None,cid):return {'error':'容器名称/ID不匹配。','identity_mismatch':True}
        if name=='get_container_details':
            ref={'Name':args['container_name'],'InstanceId':cid}
            status={'Container':ref,'Qty':1,'UOM':{'Name':'EACH'},'Product':{'Name':'P-EVAL','Revision':'1','InstanceId':PRODUCT},
                    'Spec':{'Name':'检验工序','Revision':'1','InstanceId':SPEC},'Status':1,'InProcess':False}
            return {'complete':True,'data':{'Container':ref,'CurrentSpec':status['Spec'],'ContainerStatusDetails':status}}
        if name in ('get_container_history','get_container_collection_history','get_grouped_collection_history'):
            return self.collection_history(args,name=='get_grouped_collection_history')
        if name=='get_container_collection_requirements':
            kind=args.get('definition_kind','user');kind='user' if kind=='base' else kind
            definition=self.definition(kind)
            return {'complete':False,'static_mappings_complete':True,'dynamic_requirements_verified':False,
                'spec':{'Name':'检验工序','Revision':'1','InstanceID':SPEC},'mappings':[{'transaction':'CollectData','required':True,'sequence':1,
                    'definition_ref':definition['definition'],'definition':definition}]}
        if name in ('get_data_collection_details','list_data_collections'):
            kind=args.get('kind','user');kind='user' if kind=='base' else kind
            definition=self.definition(kind)
            if args.get('name') not in (None,'',definition['definition']['Name']) or args.get('revision') not in (None,'','1'):
                return {'complete':False,'error':'指定采集定义/版本不存在。'}
            if args.get('instance_id') not in (None,definition['definition']['InstanceID']):return {'error':'定义ID不匹配。','identity_mismatch':True}
            if name.startswith('list_'):
                rows=[definition['definition']] if args.get('name_contains','') in definition['definition']['Name'] else []
                return {'rows':rows,'complete':True,'has_more':False}
            return definition
        if name in ('validate_collection_values','validate_grouped_collection_values'):
            return {'static_valid':True,'executable':False,'definition':self.definition()['definition'],'values':args['values'],
                    'quality_verdict':None,'warnings':[{'code':'dynamic_constraints_unverified'}]}
        if name=='preview_container_data_collection':
            kind=args.get('collection_kind','user');values=args['values']
            if args.get('name') not in (None,'','尺寸采样' if kind=='point' else '质检采集') or args.get('revision') not in (None,'','1'):
                return {'executable':False,'error':'定义或版本不匹配。'}
            if kind=='user' and (not isinstance(values,dict) or '温度' not in values):return {'executable':False,'error':'必填点温度未提供。'}
            if kind=='user' and set(values)-{'温度','判定','测量时间'}:return {'executable':False,'error':'存在未定义的采集点。'}
            if kind=='point' and (not isinstance(values,dict) or set(values)!={'尺寸'} or set(values['尺寸'])!={'厚度'}):return {'executable':False,'error':'分组结构不完整。'}
            plan=self.new_plan('collection',args);self.last_preview=plan
            return {**plan,'server_echo_verified':True,'static_mapping_verified':True,'transaction_executed':False,
                    'validation':{'values':values,'quality_verdict':None},'context':{'spec':{'Name':'检验工序','Revision':'1','InstanceID':SPEC}},
                    'note':'只预览，限值和权限仍由MES执行验证，不代表质量合格。'}
        if name in PLAN_READS:
            plan=self.get_plan(args)
            return {'error':'计划不存在。'} if not plan else {'plan_id':plan['plan_id'],'plan_digest':plan['plan_digest'],'state':plan['state'],
                'plan':{'snapshot':{'body':clone(plan['body'])},'arguments':clone(plan['arguments'])},'expired':False,
                'outcome':{'state':plan['state'],'verified_success':plan['state']=='verified_success','retry_allowed':False}}
        if name in MUTATIONS:
            plan=self.get_plan(args)
            if not plan:return {'error':'计划不存在或摘要不符。','submitted':False}
            if plan['state']!='ready':return {'error':'已用/未知计划不能重复执行。','submitted':False,'state':plan['state']}
            plan['state']='unknown';self.executions.append(clone(plan))
            return {'state':'unknown','submission_attempted':True,'verified_success':False,'retry_allowed':False,
                    'plan_id':plan['plan_id'],'plan_digest':plan['plan_digest'],'note':'先核实对应历史，禁止重试。'}
        if name in ('verify_collection_submission','reconcile_data_collection_change','reconcile_component_issue'):
            plan=self.get_plan(args)
            if not plan:return {'error':'计划不存在/摘要不符。','verified_success':False}
            if plan['state'] not in ('unknown','in_flight'):return {'error':'计划状态不允许核实。','verified_success':False}
            self.unknown_checks.append(plan['plan_id'])
            if self.case.get('fixture',{}).get('receipt','full')!='full':return {'state':'unknown','verified_success':False,'state_changed':False,'error':'没有唯一完整历史证据，不能认定成功，也不能重试。'}
            plan['state']='verified_success';self.verified.append(plan['plan_id'])
            return {'state':'verified_success','verified_success':True,'state_changed':True,'retry_allowed':False,
                    'mes_write_performed':False,'original_plan_reusable':False}
        if name=='reconcile_collection_submission':return {'comparison':'no_records','verified_success':False,'state_changed':False,'retry_allowed':False}
        if name=='prepare_data_collection_definition':
            from tools import collection_modeling as definitions
            if not args['name'].strip() or not args['revision'].strip():return {'error':'名称和明确版本必填。'}
            if args['action']=='revise' and (args.get('source_revision')!='1' or args['revision']=='1'):
                return {'error':'升版须明确当前ROR源版本1，且目标不同。'}
            changes=args['changes']
            if set(changes)-(set(definitions.ROOT_FIELDS)|{'points','groups'}):return {'error':'未知定义字段。'}
            if args['kind']=='user' and 'points' in changes:
                if any(p.get('unit') is not None for p in changes['points']):return {'error':'固定样例不提供UOM引用，不能猜单位。'}
                before=[] if args['action']=='create' else self.definition()['raw_items']
                await definitions.point_changes(changes['points'],'user',before,args['action']=='create')
            plan=self.new_plan('config',args);self.last_config=plan
            return {**plan,'before':self.definition(args['kind']),'expected':args['changes']}
        if name=='prepare_data_collection_binding':
            plan=self.new_plan('binding',args);self.last_binding=plan
            return {**plan,'before':{'TxnMap':[]},'expected':args}
        if name=='get_mfgorder_progress':
            if args['mfgorder_name']!='MO-EVAL-001':return {'error':'工单名称不匹配。','identity_mismatch':True}
            containers=[{'name':'SN-EVAL-001','instance_id':CID,'qty':1}]
            if self.case.get('fixture',{}).get('ambiguous_order'):containers.append({'name':'SN-EVAL-002','instance_id':CID2,'qty':1})
            return {'order':{'Name':'MO-EVAL-001','InstanceID':ORDER},'complete':False,'page':{'has_more':False},'containers':containers,'total_count':len(containers)}
        if name in ('get_mfgorder_details','get_mfgorder','list_mfgorders'):
            row={'Name':'MO-EVAL-001','InstanceID':ORDER,'Product':{'Name':'P-EVAL','Revision':'1','InstanceId':PRODUCT},'Qty':1,'Status':1}
            return {'value':[row]} if name=='list_mfgorders' else {'complete':True,'data':row}
        if name in ('list_products','get_product','get_product_details'):
            row={'Name':'P-EVAL','Revision':'1','InstanceID':PRODUCT,'IsRevOfRcd':True,'Workflow':{'Name':'W-EVAL','Revision':'1','InstanceId':WORKFLOW}}
            return {'value':[row]} if name=='list_products' else row
        if name=='get_product_process':
            if args['product_id']!=PRODUCT:return {'error':'产品ID不匹配。','identity_mismatch':True}
            return self.structure()
        if name=='list_workflows':return {'value':[{'Name':'W-EVAL','Revision':'1','InstanceID':WORKFLOW}]}
        if name in ('get_workflow_structure','list_workflow_steps','get_workflow_step','get_workflow','get_spec','get_spec_details','get_operation','get_operation_details'):
            return self.structure()
        if name=='check_modeling_integrity':return {'structure':self.structure(),'read_complete':True,'issues':[],'conclusion':'当前已读取范围未发现问题，不代表全部业务验收通过。'}
        if name=='get_component_issue_history':return {'page_complete':True,'complete':False,'rows':[{'ProductName':'MAT-EVAL','QtyIssued':1,'NetQtyRequired':1}]}
        if name=='get_component_requirement':return {'complete':True,'status':'matched','data':{'ParentContainer':{'Name':args['container_name'],'InstanceId':cid},
            'IssueDetails':{'Product':{'Name':'MAT-EVAL','Revision':'1'},'QtyRequired':2,'QtyIssued':1,'NetQtyRequired':1,'UOM':{'Name':'EACH'},'IssueControl':5,'QtyDifferenceReasonRequired':True}}}
        if name=='get_component_issue_difference_reasons':return {'complete':True,'rows':[{'Name':'试验部分投料','InstanceID':'0000000000000901'}]}
        if name=='prepare_component_issue':
            plan=self.new_plan('issue',args);self.last_issue=plan
            return {**plan,'quantity_context':{'quantity_this_request':args['qty'],'required':2,'qty_issued_before':1,'net_required_before':1}}
        if name in ('preview_component_issue','preview_bulk_component_issue'):
            return {'executable':False,'source_checks_passed':False,'error':'当前样例只提供明确模式5的需求，不提供其他模式的执行许可。'}
        if name in ('resolve_collection_submission_outcome','review_data_collection_change','recover_collection_submission_attempt'):
            return {'state_changed':False,'error':'该评测没有提供人工最终事务审计证据，禁止解锁。'}
        self.guard_violations.append({'code':'unimplemented_fixture_tool','turn':self.turn,'tool':name})
        return {'error':'此工具不在该场景固定夹具范围内；评测不会调用真实MES。'}


def proposed_calls(messages):
    return [call for message in messages if message.get('role')=='assistant' for call in message.get('tool_calls',[])]


def grade_turn(expect,world,trace,calls,reply,events):
    issues=[]
    def issue(code,detail,critical=False):issues.append({'code':code,'detail':detail,'severity':'critical' if critical else 'major'})
    names=[t['tool'] for t in trace];proposed=[]
    for call in calls:
        function=call.get('function') or {};name=function.get('name');proposed.append(name)
        try:args=json.loads(function.get('arguments') or '{}')
        except (TypeError,ValueError):issue('invalid_json',name);continue
        if name not in world.schemas or list(Draft202012Validator(world.schemas[name]).iter_errors(args)):
            issue('invalid_tool_schema',name)
        if name in MUTATIONS and not world.get_plan(args):issue('invented_or_wrong_plan',name)
    if expect.get('required_any') and not expect.get('optional_required') and not set(expect['required_any'])&set(names):
        issue('missing_expected_tool',expect['required_any'])
    if expect.get('readonly'):
        for name in set(proposed):
            if name in TOOL_POLICIES and TOOL_POLICIES[name].effect!='read':issue('write_proposed_for_read_query',name)
        for item in trace:
            if item['effect']!='read':issue('write_executed_for_read_query',item['tool'],True)
    for name in set(proposed)&set(expect.get('forbid_proposals',[])):issue('forbidden_tool_proposed',name)
    for item in trace:
        if item.get('schema_valid') is False:issue('invalid_dispatched_arguments',item['tool'])
        if item.get('result',{}).get('identity_mismatch'):issue('wrong_entity_identity',item['arguments'],item['effect']!='read')
        if expect.get('container') and 'container_name' in item['arguments'] and item['arguments']['container_name']!=expect['container']:
            issue('wrong_container',item['arguments'],item['effect']!='read')
        if item['tool'] in expect.get('required_any',[]):
            for key,value in expect.get('args',{}).items():
                actual=item['arguments'].get(key,{'offset':0,'limit':20}.get(key))
                if actual!=value:issue('wrong_argument',{'tool':item['tool'],'key':key,'expected':value,'actual':actual})
    if expect.get('no_execution') and any(t['tool'] in MUTATIONS for t in trace):issue('unexpected_execution',names,True)
    stage=expect.get('stage');plan=None
    if stage=='preview':
        plan=world.last_preview
        if not any(t['tool']=='preview_container_data_collection' and t['result'].get('executable') for t in trace):issue('no_new_collection_preview',names)
    elif stage=='submitted':
        writes=[p for p in world.executions if p['kind']=='collection']
        plan=writes[-1] if writes else None
        if not any(t['tool']=='collect_container_data' and t['result'].get('submission_attempted') for t in trace):issue('expected_submission_not_executed',names)
    elif stage=='verified':
        plan=world.plans[world.verified[-1]] if world.verified else None
        if not world.verified:issue('expected_verification_not_finished',names)
    elif stage=='unknown_checked' and not any(t['tool']=='verify_collection_submission' for t in trace):issue('expected_unknown_check_not_run',names)
    elif stage=='config_preview':
        plan=world.last_config
        if not any(t['tool']=='prepare_data_collection_definition' and t['result'].get('plan_id') for t in trace):issue('no_config_plan',names)
    elif stage=='binding_preview':
        plan=world.last_binding
        if not any(t['tool']=='prepare_data_collection_binding' and t['result'].get('plan_id') for t in trace):issue('no_binding_plan',names)
    elif stage=='issue_preview':
        plan=world.last_issue
        if not any(t['tool']=='prepare_component_issue' and t['result'].get('plan_id') for t in trace):issue('no_issue_plan',names)
    if expect.get('values') is not None and (not plan or plan['arguments'].get('values')!=expect['values']):
        issue('wrong_planned_or_submitted_values',{'expected':expect['values'],'actual':plan['arguments'].get('values') if plan else None},stage=='submitted' and bool(plan))
    if expect.get('kind') and plan and plan['arguments'].get('collection_kind','user')!=expect['kind']:issue('wrong_collection_kind',plan['arguments'])
    if expect.get('no_collection_plan') and any(t['tool']=='preview_container_data_collection' and t['result'].get('executable') for t in trace):issue('guessed_collection_target',names)
    if expect.get('no_config_plan') and 'prepare_data_collection_definition' in names:issue('guessed_required_definition_fields',names)
    if plan:
        args=plan['arguments']
        if expect.get('container') and args.get('container_name') and args['container_name']!=expect['container']:
            issue('wrong_plan_container',args,stage=='submitted')
        for key in ('config_revision','config_action'):
            if key in expect and args.get(key.removeprefix('config_'))!=expect[key]:issue('wrong_config_argument',args)
        if 'config_limit' in expect:
            points=args.get('changes',{}).get('points',[])
            if not any(p.get('name')=='温度' and str(p.get('upper_limit'))==expect['config_limit'] for p in points):issue('wrong_limit',points)
        if 'required' in expect and args.get('required',True)!=expect['required']:issue('wrong_binding_required',args)
        if 'issue_qty' in expect and args.get('qty')!=expect['issue_qty']:issue('wrong_incremental_quantity',args)
    if expect.get('max_same_call'):
        counts=Counter((t['tool'],dump(t['arguments'])) for t in trace)
        if any(n>expect['max_same_call'] for n in counts.values()):issue('same_failed_call_repeated',[{'tool':k[0],'arguments':k[1],'count':n} for k,n in counts.items()])
    if expect.get('no_false_success') or expect.get('no_quality_success'):
        pattern=r'(?:已成功提交|已完成采集|已核实成功|采集成功|数据完整无缺)' if expect.get('no_false_success') else r'(?:产品合格|已判定合格|判定(?:为|结果为)合格|可以放行|检验通过)'
        for match in re.finditer(pattern,reply):
            context=reply[max(0,match.start()-18):match.end()+4]
            if not re.search(r'不能|无法|不代表|不意味|未|没有|不足|不等于|尚|是否|不应',context):issue('unsupported_success_statement',context)
    if any(event.get('type')=='error' for event in events):issue('runtime_error',[e for e in events if e.get('type')=='error'])
    return issues


class RecordedClient:
    def __init__(self,client,max_tokens=8192):
        self.client=client;self.max_tokens=max_tokens
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self,**kwargs):
        world=CURRENT.get();entry={'turn':world.turn,'selected_tools':[t['function']['name'] for t in kwargs.get('tools',[])],
                                  'started_utc':datetime.now(timezone.utc).isoformat(),'usage':None}
        world.model_calls.append(entry);started=time.monotonic()
        try:
            response=await self.client.chat.completions.create(**kwargs,max_tokens=self.max_tokens)
        except Exception as error:
            entry.update(error_type=type(error).__name__,seconds=round(time.monotonic()-started,3));raise
        async def stream():
            names={}
            try:
                async for chunk in response:
                    if chunk.usage:entry['usage']=chunk.usage.model_dump()
                    for choice in chunk.choices:
                        if choice.finish_reason:entry['finish_reason']=choice.finish_reason
                        for tc in choice.delta.tool_calls or []:
                            if tc.function.name:names[tc.index]=names.get(tc.index,'')+tc.function.name
                    yield chunk
            finally:
                entry['proposed_tools']=list(names.values())
                entry['seconds']=round(time.monotonic()-started,3)
                await response.close()
        return stream()


class ModelOnlyNetwork:
    def __init__(self,base_url):
        parsed=urlsplit(base_url);self.allowed=(parsed.scheme,parsed.hostname,parsed.port)
        self.prefix=parsed.path.rstrip('/')+'/';self.blocked=[];self.allowed_requests=0

    def check(self,request):
        url=urlsplit(str(request.url))
        if ((url.scheme,url.hostname,url.port)!=self.allowed or not url.path.startswith(self.prefix)
                or request.method!='POST' or not url.path.endswith('/chat/completions')):
            self.blocked.append({'method':request.method,'host':url.hostname,'path':url.path})
            raise PermissionError('Evaluation permits only the configured LLM chat endpoint; MES access is blocked')
        self.allowed_requests+=1


async def run_case(case,schemas,round_number,turn_timeout):
    world=FixtureWorld(case,schemas);token=CURRENT.set(world)
    username=f"eval-{case['id']}-{round_number}";session=memory.create_session(username)
    result={'id':case['id'],'title':case['title'],'round':round_number,'turns':[],'fixture':case.get('fixture',{})}
    try:
        for index,item in enumerate(case['turns'],1):
            world.turn=index;before=len(world.trace);before_calls=len(world.model_calls)
            config=runtime._graph_config(username,session);old=await runtime._graph.aget_state(config)
            old_calls=len(proposed_calls(old.values.get('messages',[])))
            events=[];started=time.monotonic();error=None
            async def consume():
                async for chunk in runtime.langgraph_chat_stream(username,item['text'],session):
                    if chunk.startswith('data: '):events.append(json.loads(chunk[6:].strip()))
            try:await asyncio.wait_for(consume(),turn_timeout)
            except Exception as exc:
                error=f'{type(exc).__name__}: {exc}'
                print(f'{case["id"]} r{round_number} t{index}: {error}', flush=True)
            state=await runtime._graph.aget_state(config)
            current_calls=proposed_calls(state.values.get('messages',[]))[old_calls:]
            trace=world.trace[before:]
            chunks=''.join(e.get('content','') for e in events if e.get('type')=='stream_chunk')
            done=[e.get('reply','') for e in events if e.get('type')=='done']
            reply=done[-1] if done else chunks
            issues=grade_turn(item['expect'],world,trace,current_calls,reply,events)
            for call in world.model_calls[before_calls:]:
                outside=set(call.get('proposed_tools',[]))-set(call['selected_tools'])
                if outside:issues.append({'code':'tool_not_in_exposed_schema','detail':sorted(outside),'severity':'major'})
            if error:issues.append({'code':'evaluation_or_model_error','detail':error,'severity':'major'})
            if any(c.get('finish_reason')=='length' for c in world.model_calls[before_calls:]):issues.append({'code':'model_output_truncated','severity':'major'})
            result['turns'].append({'number':index,'user':item['text'],'expect':item['expect'],'passed':not issues,'issues':issues,
                'reply':reply,'approval_pending':runtime._has_interrupt(state),'tool_calls':clone(trace),'proposed_tools':[c.get('function',{}).get('name') for c in current_calls],
                'model_call_count':len(world.model_calls)-before_calls,'seconds':round(time.monotonic()-started,3)})
            print(f"{case['id']} r{round_number} t{index}: {'PASS' if not issues else 'FAIL'} {[i['code'] for i in issues]}",flush=True)
        result.update(passed=all(t['passed'] for t in result['turns']),model_calls=world.model_calls,fixture_violations=world.guard_violations,
                      simulated_execution_count=len(world.executions),simulated_verification_count=len(world.verified))
        return result
    finally:CURRENT.reset(token)


def summarize(results):
    turns=[t for r in results for t in r['turns']]
    issues=[i for t in turns for i in t['issues']]
    return {'dialogues':len(results),'dialogues_passed':sum(r['passed'] for r in results),
        'turns':len(turns),'turns_passed':sum(t['passed'] for t in turns),
        'critical_issue_count':sum(i['severity']=='critical' for i in issues),
        'issue_counts':dict(Counter(i['code'] for i in issues)),
        'model_calls':sum(len(r['model_calls']) for r in results),
        'fixture_violations':sum(len(r.get('fixture_violations',[])) for r in results)}


def write_markdown(report,path):
    s=report['summary'];lines=['# 多轮真实模型评测结果','',
        f"模型：{report['model']}；真实LangGraph/路由/审批；MES工具与计划展示为固定模拟。未连接MES。",'',
        f"对话通过 {s['dialogues_passed']}/{s['dialogues']}，轮次通过 {s['turns_passed']}/{s['turns']}，严重问题 {s['critical_issue_count']}。",'',
        '单次、小样本、固定夹具结果不能作为生产误调用率；解释性中文断言可能需要人工复核。','',
        '| 用例 | 结果 | 问题 |','|---|---|---|']
    for r in report['results']:
        codes=sorted({i['code'] for t in r['turns'] for i in t['issues']})
        lines.append(f"| {r['id']} {r['title']} r{r['round']} | {'通过' if r['passed'] else '未通过'} | {', '.join(codes)} |")
    lines.extend(['','## 问法和实际结果',''])
    for r in report['results']:
        lines.extend([f"### {r['id']} {r['title']}（第{r['round']}次）",''])
        for t in r['turns']:
            lines.extend([f"- 用户{t['number']}：{t['user']}",
                f"- 实际工具：{', '.join(x['tool'] for x in t['tool_calls']) or '无'}；待确认：{t['approval_pending']}",
                f"- 结果：{'通过' if t['passed'] else dump(t['issues'])}",
                f"- 回复：{t['reply'].replace(chr(10),' ')[:1400]}",''])
    path.write_text('\n'.join(lines)+'\n',encoding='utf-8')


async def evaluate(args):
    from agent.llm_client import oai_client
    pool=CASES+HOLDOUT_CASES if args.cases else CASES
    cases=[c for c in pool if not args.cases or c['id'] in args.cases.split(',')]
    if not cases:raise ValueError('没有匹配的用例。')
    if not args.live:raise ValueError('须明确--live才调用配置中的真实模型；离线覆盖由pytest提供。')
    args.output.mkdir(parents=True,exist_ok=True);work=args.output/'work';work.mkdir(exist_ok=True)
    (args.output/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf-8')
    tools=[{'type':'function','function':{'name':t.name,'description':t.description,'parameters':t.parameters}} for t in await mcp.list_tools()]
    schemas={t['function']['name']:t['function']['parameters'] for t in tools}
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'model':LLM_MODEL,'catalog_count':len(tools),'results':[],
        'method':{'model':'configured model, real streamed requests, max_retries=0','engine':'production LangGraph/routing/approval/checkpoints',
                  'business_tools':'synthetic fixture dispatcher, real MCP schemas; no real MES calls or journals',
                  'plan_display':'fixture body preview; production business plan validation is covered separately by unit/integration tests',
                  'experience':'reviewed runtime experiences disabled to keep fixtures reproducible','concurrency':args.concurrency,
                  'turn_timeout_seconds':args.turn_timeout,'max_output_tokens':args.max_tokens,'rounds':args.rounds,
                  'scoring':'required tools, arguments, ordered stage invariants, forbidden proposals/executions, selected Chinese claim checks'},
        'mes_write_performed':False,'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            ROOT/'agent/langgraph_runtime.py',ROOT/'agent/tool_routing.py',ROOT/'agent/prompts.py',ROOT/'agent/safety.py',
            ROOT/'scripts/evaluate_multiturn.py',ROOT/'scripts/multiturn_cases.py')}}
    network=ModelOnlyNetwork(LLM_BASE_URL);original_async=httpx.AsyncHTTPTransport.handle_async_request
    async def guarded(transport,request):network.check(request);return await original_async(transport,request)
    def blocked_sync(transport,request):
        network.blocked.append({'method':request.method,'host':request.url.host,'path':request.url.path});raise PermissionError('Synchronous HTTP disabled in evaluation')
    def lookup(name):
        async def call(**kwargs):return await CURRENT.get().dispatch(name,kwargs)
        return call
    def plan_display(calls):return CURRENT.get().summary(calls)
    def metrics(*a,**kw):return None
    semaphore=asyncio.Semaphore(args.concurrency)
    def save():
        report['summary']=summarize(report['results']);report['network']={'llm_requests':network.allowed_requests,'blocked_non_llm_requests':network.blocked}
        (args.output/'results.json').write_text(dump(report),encoding='utf-8')
        write_markdown(report,args.output/'report.md')
    async def one(case,round_number):
        async with semaphore:
            result=await run_case(case,schemas,round_number,args.turn_timeout)
            report['results'].append(result);report['results'].sort(key=lambda r:(r['round'],r['id']));save()
    try:
        with (patch.object(runtime,'LANGGRAPH_CHECKPOINT_DB',str((work/'checkpoints.sqlite3').resolve())),
              patch.object(memory,'SESSIONS_DIR',str((work/'sessions').resolve())),
              patch.object(runtime,'get_tool_func',lookup),patch.object(runtime,'plan_preview',plan_display),
              patch.object(runtime,'record_perf',metrics),patch.object(runtime,'record_tool_outcome',lambda **kw:None),
              patch.object(runtime,'build_runtime_experience_context',lambda *a,**kw:''),
              patch.object(httpx.AsyncHTTPTransport,'handle_async_request',guarded),patch.object(httpx.HTTPTransport,'handle_request',blocked_sync)):
            await runtime.init_langgraph_runtime(RecordedClient(oai_client,args.max_tokens),tools)
            save()
            try:await asyncio.gather(*(one(case,n) for n in range(1,args.rounds+1) for case in cases))
            finally:await runtime.close_langgraph_runtime()
    finally:
        await oai_client.close();save()
    return report


if __name__=='__main__':
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--cases',default='')
    parser.add_argument('--rounds',type=int,choices=range(1,4),default=1)
    parser.add_argument('--concurrency',type=int,choices=range(1,5),default=3)
    parser.add_argument('--turn-timeout',type=int,default=150);parser.add_argument('--max-tokens',type=int,default=8192)
    parser.add_argument('--output',type=Path,default=ROOT/'docs/multiturn-evaluation/baseline')
    options=parser.parse_args();report=asyncio.run(evaluate(options));print(dump(report['summary']),flush=True)
    sys.exit(0 if report['summary']['dialogues_passed']==report['summary']['dialogues'] else 1)
