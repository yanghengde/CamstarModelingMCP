import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from tools import quality as q,mcp,get_tool_func
from core.tool_catalog import TOOL_POLICIES
from agent.tool_routing import select_tools
from tools.tool_discovery import load_tool_group,get_capabilities

CID='0000000000000101';RID='0000000000000201'
def result(coro):return json.loads(asyncio.run(coro))


@pytest.mark.parametrize('offset,limit',[(True,20),(-1,20),(0,0),(0,101),(0,False)])
def test_bad_paging_never_queries(monkeypatch,offset,limit):
    call=AsyncMock();monkeypatch.setattr(q,'sql_rows',call)
    assert 'error' in result(q.list_nonconformance_reports(offset=offset,limit=limit))
    call.assert_not_awaited()


def test_fixed_sql_escapes_unicode_name_and_bounds(monkeypatch):
    name="检验' OR 1=1 --"
    row={'NonconformanceReportId':RID,'NonconformanceReportName':name}
    call=AsyncMock(side_effect=[[row],[],[row]])
    monkeypatch.setattr(q,'sql_rows',call)
    out=result(q.get_nonconformance_report(name))
    assert out['found'] and name==out['data']['NonconformanceReportName']
    query=call.await_args_list[0].args[0]
    assert "N'检验'' OR 1=1 --'" in query and 'TOP 2' in query
    assert out['data']['failure_code_assignments']==[]


def test_mismatched_ncr_identity_not_returned(monkeypatch):
    monkeypatch.setattr(q,'sql_rows',AsyncMock(return_value=[{'NonconformanceReportId':RID,'NonconformanceReportName':'other'}]))
    assert 'error' in result(q.get_nonconformance_report('NCR-A'))
    assert 'error' in result(q.get_nonconformance_report(instance_id=CID))


def assignment():return {'NonconformanceReportId':RID,'FieldId':10019,'Sequence':0,'NCRFailureCodesId':CID,'NCRFailureCodeName':'Failure-A','FailureCodeChangeCount':1}


def test_ncr_detail_reads_verified_join_and_checks_root_again(monkeypatch):
    row={'NonconformanceReportId':RID,'NonconformanceReportName':'N','ChangeCount':1}
    call=AsyncMock(side_effect=[[row],[assignment()],[row]]);monkeypatch.setattr(q,'sql_rows',call)
    out=result(q.get_nonconformance_report('N'))
    assert out['complete'] and out['data']['failure_code_assignments'][0]['NCRFailureCodeName']=='Failure-A'
    query=call.await_args_list[1].args[0]
    assert 'TOP 51' in query and 'LEFT JOIN NCRFailureCode' in query and 'a.FieldId=10019' in query
    call.side_effect=[[row],[assignment()],[{**row,'ChangeCount':2}]]
    assert 'error' in result(q.get_nonconformance_report('N'))


@pytest.mark.parametrize('field,value',[('NonconformanceReportId',CID),('FieldId',999),('Sequence',True),('Sequence',-1),('NCRFailureCodesId','bad'),('NCRFailureCodeName',None)])
def test_ncr_assignment_parent_field_order_and_reference_are_checked(monkeypatch,field,value):
    row=assignment();row[field]=value;monkeypatch.setattr(q,'sql_rows',AsyncMock(return_value=[row]))
    with pytest.raises(ValueError):asyncio.run(q.ncr_failure_assignments(RID))


@pytest.mark.parametrize('mode',['duplicate','overflow','order','missing_reference'])
def test_ncr_assignment_completeness_is_required(monkeypatch,mode):
    row=assignment();other={**row,'Sequence':1,'NCRFailureCodesId':'0000000000000501','NCRFailureCodeName':'Failure-B'}
    rows={'duplicate':[row,row],'overflow':[row]*51,'order':[other,row],'missing_reference':[{**row,'NCRFailureCodeName':None}]}[mode]
    monkeypatch.setattr(q,'sql_rows',AsyncMock(return_value=rows))
    with pytest.raises(ValueError):asyncio.run(q.ncr_failure_assignments(RID))


def selection():return {'ResolutionAction':{'RecordCount':3,'RowCount':3,'SelectionValuesEx':[
    {'Value':'2','Name':'AttachToCAPA','Label':'Attach to Existing CAPA'},
    {'Value':'1','Name':'Close','Label':'Close - No Action Required'},
    {'Value':'3','Name':'CreateCAPA','Label':'Create New CAPA'}]}}


def test_closure_options_fixed_read_only_endpoint_and_raw_codes(monkeypatch):
    call=AsyncMock(return_value=json.dumps(selection()));monkeypatch.setattr(q,'request',call)
    out=result(q.get_quality_closure_options())
    assert out['complete'] and out['options'][1]['Value']=='1' and not out['target_permission_verified']
    assert out['supported_plan_actions']==['Close']
    assert call.await_args.args==('POST','/api/CloseQualityObject/RequestSelectionValues')
    assert call.await_args.kwargs['params']=={'selectionValuesExpression':'ResolutionAction'}
    assert call.await_args.kwargs['body']=={} and call.await_args.kwargs['retry_read_only'] is True


def test_archived_live_closure_options_are_parsed_without_inventing_enum(monkeypatch):
    from pathlib import Path
    archive=json.loads(Path('docs/quality-fixtures/phase2-contracts.json').read_text(encoding='utf-8'))
    reply=archive['close_resolution_options']
    assert reply['status']==200
    monkeypatch.setattr(q,'request',AsyncMock(return_value=reply['body']))
    out=result(q.get_quality_closure_options())
    assert {r['Name']:r['Value'] for r in out['options']}=={'Close':'1','AttachToCAPA':'2','CreateCAPA':'3'}


@pytest.mark.parametrize('mode',['truncated','duplicate','bad_code','boolean_code','missing_label','error','next_link','bad_count','bad_shape'])
def test_closure_options_fail_closed_on_incomplete_or_ambiguous_reply(monkeypatch,mode):
    data=selection();s=data['ResolutionAction'];rows=s['SelectionValuesEx']
    if mode=='truncated':s['RecordCount']=4
    elif mode=='duplicate':rows[0]=rows[1].copy()
    elif mode=='bad_code':rows[1]['Value']='01'
    elif mode=='boolean_code':rows[1]['Value']=True
    elif mode=='missing_label':rows[1].pop('Label')
    elif mode=='error':data['ErrorCode']='rejected'
    elif mode=='next_link':s['@odata.nextLink']='next'
    elif mode=='bad_count':s['RowCount']=True
    else:data=[]
    monkeypatch.setattr(q,'request',AsyncMock(return_value=json.dumps(data)))
    assert 'error' in result(q.get_quality_closure_options())


def test_quality_object_requires_detail_not_merely_reference_echo(monkeypatch):
    reply={'QualityObject':{'Name':'A','InstanceId':RID},'QualityObjectInquiryDetail':{}}
    monkeypatch.setattr(q,'request',AsyncMock(side_effect=lambda *a,**k:json.dumps(reply)))
    assert 'error' in result(q.get_quality_object_details('A'))
    reply['QualityObjectInquiryDetail'].update(Status='7',Category='5')
    assert result(q.get_quality_object_details('A'))['complete']


def test_list_ncr_pagination_and_filter_identity(monkeypatch):
    monkeypatch.setattr(q,'container_identity',AsyncMock(return_value=({'name':'C','instanceId':CID},{})))
    rows=[{'NonconformanceReportId':f'{i+500:016x}','ContainerId':CID,'NCRStatus':1} for i in range(3)]
    monkeypatch.setattr(q,'sql_rows',AsyncMock(return_value=rows))
    out=result(q.list_nonconformance_reports('C',ncr_status=1,limit=2))
    assert out['has_more'] and out['next_offset']==2 and len(out['rows'])==2 and not out['complete']
    rows[0]['ContainerId']=RID
    assert 'error' in result(q.list_nonconformance_reports('C',ncr_status=1,limit=2))


def test_quality_view_not_inventing_names_or_accepting_wrong_filters(monkeypatch):
    rows=[{'Id':RID,'Type':5,'Category':5,'Status':7}]
    monkeypatch.setattr(q,'sql_rows',AsyncMock(return_value=rows))
    out=result(q.list_quality_objects(category=5))
    assert out['rows']==rows and 'Name' not in out['rows'][0]
    assert 'error' in result(q.list_quality_objects(category=1))


def test_quality_history_bounds_echo_and_mapping(monkeypatch):
    data={'QualityObject':{'Name':'Q','InstanceId':RID},'Histories':[{'InstanceId':'0000000000000501'}],'HistoryDisplayTexts':[]}
    call=AsyncMock(side_effect=lambda *a,**kw:json.dumps(data));monkeypatch.setattr(q,'request',call)
    out=result(q.get_quality_history('quality_object','Q',RID,start_row=3,limit=1))
    assert out['page_complete'] and not out['complete'] and not out['labels_aligned']
    body=call.await_args.kwargs['body']
    assert body['resultsetStartRow']==3 and body['resultsetSizeLimit']==1
    assert call.await_args.args[1]=='/api/QualityObjectHistoryInquiry'
    data['QualityObject']['InstanceId']=CID
    assert 'error' in result(q.get_quality_history('quality_object','Q',RID))


def test_history_empty_is_not_success_or_complete(monkeypatch):
    monkeypatch.setattr(q,'request',AsyncMock(return_value=json.dumps({'NonconformanceReport':{'Name':'N','InstanceId':RID},'Histories':[],'HistoryDisplayTexts':[]})))
    out=result(q.get_quality_history('ncr','N',RID))
    assert out['submission_outcome']=='unknown' and not out['complete']


def test_reference_details_keep_root_and_detect_partial_expand(monkeypatch):
    root={'Name':'P','Revision':'1','InstanceID':RID,'Description':'saved root'}
    expanded={'InstanceID':RID,'Description':None,'SamplingPlanDetails':[]}
    class Context:
        def __init__(self,*a,**k):pass
        async def resolve(self,*a):return copy.deepcopy(root)
        async def get(self,*a,**k):return copy.deepcopy(expanded)
    monkeypatch.setattr(q,'ReadContext',Context)
    out=result(q.get_quality_reference_details('sampling_plan','P','1'))
    assert out['data']['Description']=='saved root' and out['complete']
    expanded['SamplingPlanDetails@odata.nextLink']='next'
    assert not result(q.get_quality_reference_details('sampling_plan','P','1'))['complete']


def test_context_sampling_parent_and_no_quality_verdict(monkeypatch):
    monkeypatch.setattr(q,'container_identity',AsyncMock(return_value=({'name':'C','instanceId':CID},{})))
    row={'ContainerId':CID,'ContainerName':'C','CurrentHoldCount':0,'SamplingPassed':False}
    sample={'ContainerId':CID,'ContainerSampleDataId':RID,'TotalSamplesRequired':3,'CompletedSamples':1,'Rejects':0}
    monkeypatch.setattr(q,'sql_rows',AsyncMock(side_effect=[[row],[sample]]))
    out=result(q.get_container_quality_context('C'))
    assert out['quality_verdict'] is None and out['release_authorized'] is False
    assert out['status']['CurrentHoldCount']==0 and out['status']['SamplingPassed'] is False


def test_inventory_does_not_offer_container_scrap():
    out=result(q.get_quality_api_inventory())
    scrap=next(x for x in out['families'] if x['domain']=='报废')
    assert set(scrap['tools'])=={'prepare_container_quantity_loss','get_sampling_lot_scrap_readiness'} and 'PartScrap' in scrap['discovered']
    assert 'scrap_container' not in scrap['tools']


def test_quality_registry_loading_routing_and_confirmations():
    names={n for n,p in TOOL_POLICIES.items() if p.group=='quality'}
    assert len(names)==77
    registered={t.name for t in asyncio.run(mcp.list_tools())}
    assert names<=registered and all(get_tool_func(n) for n in names)
    reads={n for n in names if TOOL_POLICIES[n].effect=='read'}
    assert set(result(load_tool_group('quality'))['tools'])==reads
    assert all(TOOL_POLICIES[n].always_confirm for n in names-reads)
    assert result(get_capabilities(group='quality'))['groups'][0]['group']=='quality'
    tools=[{'function':{'name':n}} for n in TOOL_POLICIES]
    for question,tool in [('查询不合格NCR历史','get_quality_history'),('查看CAPA质量对象','get_quality_object_details'),
                          ('查询抽检状态','get_container_quality_context'),('查询SPC控制配置','list_quality_references'),
                          ('查询CAPA关闭处置选项','get_quality_closure_options'),
                          ('查询质量检验任务','get_container_inspection_tasks'),('查询质量检验历史','get_container_inspection_history'),
                          ('查询质量检验执行记录','get_container_inspection_history'),('查询抽样批详情','get_sampling_lot_details'),
                          ('查询抽样批就绪状态','get_sampling_lot_readiness'),('查询抽样批历史','get_sampling_lot_history'),
                          ('查询批采样历史','get_sampling_lot_history')]:
        selected={t['function']['name'] for t in select_tools(tools,[{'role':'user','content':question}])}
        assert tool in selected
        assert 'execute_quality_transaction' not in selected
    selected={t['function']['name'] for t in select_tools(tools,[{'role':'user','content':'请记录这个容器的质量检验结果'}])}
    assert 'prepare_container_inspection' in selected and 'execute_quality_transaction' in selected
    batch={t['function']['name'] for t in select_tools(tools,[{'role':'user','content':'请登记这批采样结果'}])}
    assert 'prepare_lot_inspection' in batch and 'execute_quality_transaction' in batch
    assert 'collect_container_data' not in batch
