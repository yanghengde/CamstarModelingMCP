"""Exact native counted-inspection + internal ChangeQty evidence, versioned.

One submitted CollectSamplingData, no second quantity write. Native history
stores group rejection counters cumulatively; reason child values retain the
individual increments. Never sum the running group counters as reject units.
"""
from collections import Counter
from decimal import Decimal
import re

from tools import quality as q,quality_inspection as i,quality_inspection_effects as fx
from tools import quality_quantity_loss as loss,quality_disposition_history as d,quality_history as h,quality_plans as p

SUMMARY=('CollectSamplingDataHistoryId,HistoryId,HistoryMainlineId,TxnId,CDOTypeId,SampleTestId,SamplingPlanId,'
         'ProductId,SpecId,InspectionLevelId,AQLLevelId,RequiredSamples,SamplesTested,TotalSamplesCompleted,TotalRejects,TotalPassedSamples,AllowedRejects,SamplingStatus')


def supported(snap):
    return (snap.get('kind')=='inspection' and snap.get('inspection_evidence_version')==1
        and snap.get('inspection_input_version')==2 and snap.get('definition',{}).get('test',{}).get('SampleType')==1
        and snap.get('inspection_effects',{}).get('context',{}).get('mode') in fx.LOSS_MODES
        and not snap['arguments'].get('allow_full_inspection',False))


async def roots(snap,offset=0,limit=20):
    q.paging(offset,limit)
    if not supported(snap):raise ValueError('该检验快照未开放原生组合核对，保留原人工取证流程。')
    rows=await q.sql_rows('SELECT '+d.MAINLINE+' FROM HistoryMainline WHERE ContainerId='+q.literal(snap['target_id'])
        +' AND TxnType=8452 AND Comments='+q.literal(d.comments(snap))
        +f' ORDER BY HistoryMainlineId OFFSET {offset} ROWS FETCH NEXT {limit+1} ROWS ONLY',limit+1)
    return rows[:limit],len(rows)>limit


async def transaction(root):
    native=await d.transaction(root['TxnId'],'quantity_loss')
    rows=await q.sql_rows('SELECT '+SUMMARY+' FROM CollectSamplingDataHistory WHERE HistoryMainlineId='+q.literal(root['HistoryMainlineId'])
        +' AND TxnId='+q.literal(root['TxnId']),2)
    if len(rows)!=1:raise ValueError('检验父汇总缺少或重复。')
    summary=rows[0]
    samples=await h.sampling_details({**summary,'SummaryId':summary['CollectSamplingDataHistoryId']})
    return {'native':native,'summary':summary,'samples':samples}


def number(value):return i.numeric(str(value),'原生组合数量')
def boolean(value):return i.wire_bool(value,'原生组合标志')
def integer(value):return i.wire_int(value,'原生组合计数')


def counted_rows(snap):
    """Match reason increments and native running Rejects without assuming order IDs."""
    rows=[];running=0
    for line in snap['body']['serviceDetails']:
        points=[];reported=0
        for point in line.get('samplingDataPointDetails',[]):
            amount=number(point['dataValue']);reported+=int(amount)
            points.append((h.ref_id(point['rejectReason']),amount,False))
        running+=reported
        # CountedSample sets a reason-bearing detail's Rejects to the running
        # parent FailedSamples; zero groups have no child invocation.
        rows.append((h.ref_id(line['sampleTest']),running if points else line['rejects'],bool(running) if points else line['rejected'],tuple(sorted(points))))
    return Counter(rows)


def compare(snap,attempt,root,evidence):
    row=snap['inspection_effects']['container'];task=snap['before']['task'];test=snap['definition']['test']
    context=snap['inspection_effects']['context']['automatic_quantity_loss'];defaults=context['native_child_defaults']
    cid=snap['target_id'];rootid=root['HistoryMainlineId'];txn=root['TxnId'];native=evidence['native'];summary=evidence['summary']
    amount=number(context['quantity']);remaining=number(context['projected_remaining_quantity']);closed=context['projected_close']
    count=snap['preview']['samples_this_submission'];rejects=snap['preview']['rejects_this_submission']
    operation=q.identifier(snap['inspection_effects']['container_status']['Operation'])
    def mainline(r,typ):
        return (r['ContainerId']==r['HistoryId']==cid and r['ContainerName']==snap['name'] and integer(r['TxnType'])==typ
            and r['TxnTypeName']=={8452:'CollectSamplingData',4000:'ChangeQty'}[typ] and r['TxnId']==txn and r['IntegrationTxnId']==rootid
            and integer(r['ReversalStatus'])==1 and integer(r['ContainerChangeCount'])==row['ChangeCount']
            and r['ProductId']==row['ProductId'] and r['OperationId']==operation and r['SpecId']==row['SpecId'] and r['WorkflowStepId']==row['WorkflowStepId']
            and number(r['Qty'])==number(row['Qty']) and number(r['ToQty'])==remaining
            and d.quantity(r['Qty2'],nullable_zero=True)==d.quantity(r['ToQty2'],nullable_zero=True)==0 and not boolean(r['InRework']))
    children=[r for r in native['mainlines']if r['HistoryMainlineId']!=rootid]
    checks={'attempt_time_window':d.attempt_time_matches(attempt,root),'exact_request_comments':root['Comments']==d.comments(snap),
        'exact_parent_and_single_child':len(native['mainlines'])==2 and root in native['mainlines']and len(children)==1,
        'parent_identity_context_quantity':mainline(root,8452),'internal_changeqty_identity':len(children)==1 and mainline(children[0],4000),
        'task_history_identity':summary['HistoryId']==cid and summary['HistoryMainlineId']==rootid and summary['TxnId']==txn
            and summary['SampleTestId']==test['InstanceID']and summary['SamplingPlanId']==task['SamplingPlanId']
            and summary['ProductId']==row['ProductId']and summary['SpecId']==row['SpecId']
            and summary['InspectionLevelId']==task['InspectionLevelId']and summary['AQLLevelId']==task['AQLLevelId'],
        'task_history_totals':integer(summary['RequiredSamples'])==task['TotalSamplesRequired']and integer(summary['AllowedRejects'])==task['AllowedRejects']
            and integer(summary['SamplesTested'])==count and integer(summary['TotalSamplesCompleted'])==task['CompletedSamples']+count
            and integer(summary['TotalRejects'])==task['Rejects']+rejects
            and integer(summary['TotalPassedSamples'])==task['CompletedSamples']+count-task['Rejects']-rejects}
    observed=[]
    for item in evidence['samples']:
        sample=item['sample_history'];points=[]
        for point in item['points']:
            if point['SampleDataPointId']not in ('',None):raise ValueError('计数历史出现测量点，不能混用。')
            points.append((d.identifier(point['RejectReasonId']),number(point['DataValue']),boolean(point['Passed'])))
        observed.append((sample['SampleTestId'],integer(sample['Rejects']),boolean(sample['Rejected']),tuple(sorted(points))))
    checks['all_groups_and_reason_increments']=Counter(observed)==counted_rows(snap)
    quantity_summaries=native['summaries'];details=native['details']
    checks['one_child_quantity_summary']=len(quantity_summaries)==1 and len(children)==1
    if checks['one_child_quantity_summary']:
        s=quantity_summaries[0]
        checks['quantity_summary_parent_and_options']=(s['HistoryMainlineId']==children[0]['HistoryMainlineId']and s['HistoryId']==cid and s['TxnId']==txn
            and s['IntegrationTxnId']==rootid and integer(s['CDOTypeId'])==5080 and number(s['Qty'])==-amount and d.quantity(s['Qty2'],nullable_zero=True)==0
            and boolean(s['CloseWhenZero'])==defaults['root']['CloseWhenZero']and not boolean(s['ThruputSummary']))
        wanted=context.get('reason_breakdown')or[{'reason':context['reason'],'quantity':amount}];actual=[];valid=True
        for line in details:
            valid=valid and (line['QtyHistoryId']==s['QtyHistoryId']and line['HistoryId']==cid and line['TxnId']==txn and line['IntegrationTxnId']==rootid
                and integer(line['CDOTypeId'])==6932 and integer(line['ChangeQtyType'])==2 and str(line['QtyMultiplier'])=='-1'
                and line['ContainerId']==cid and line['ProductId']==row['ProductId']and line['OperationId']==operation and line['ChargeToStepId']==row['WorkflowStepId']
                and line['UOMId']==row['UOMId']and not line['UOM2Id']and d.quantity(line['Qty2'],nullable_zero=True)==0
                and boolean(line['AdjustThruput'])==defaults['adjust_throughput']and boolean(line['CountsAgainstProduction'])and not boolean(line['RecordAllQty'])
                and boolean(line['CloseWhenZero'])==defaults['root']['CloseWhenZero']and boolean(line['ThisContainerClosed'])==closed
                and not boolean(line['ThisContainerDisassociated'])and not boolean(line['IsRollup']))
            actual.append((line['ReasonCodeId'],number(line['Qty'])))
        checks['all_internal_loss_reasons_and_flags']=valid and Counter(actual)==Counter((v['reason']['instanceId'],number(v['quantity']))for v in wanted)
    return checks


async def current_evidence(snap,txn_id=None):
    cid=snap['target_id'];before=snap['inspection_effects']['container'];task=snap['before']['task'];context=snap['inspection_effects']['context']['automatic_quantity_loss']
    row=await loss.record(cid)
    tasks=await q.sql_rows('SELECT '+i.TASK_SELECT+' FROM ContainerSampleData WHERE ContainerSampleDataId='+q.literal(task['ContainerSampleDataId'])+' AND ContainerId='+q.literal(cid),1)
    i.check_tasks(tasks,cid);now=tasks[0]if len(tasks)==1 else None
    checks={'original_container_effect':bool(row and all(row[k]==before[k]for k in ('ContainerId','ContainerName','CDOTypeId','ProductId','UOMId','UOM2Id','SpecId','WorkflowStepId','ParentContainerId','ChildCount','CarrierId','SamplingLotId'))
        and row['Status']==(2 if context['projected_close']else 1)and number(row['Qty'])==number(context['projected_remaining_quantity'])
        and d.quantity(row['Qty2'],nullable_zero=True)==0 and row['CurrentHoldCount']==0 and row['InRework'] is False and row['ChangeCount']==before['ChangeCount']+1),
        'original_task_identity_and_counters':bool(now and all(now[k]==task[k]for k in ('ContainerSampleDataId','ContainerId','SamplingPlanId','SampleTestId','InspectionLevelId','AQLLevelId','TotalSamplesRequired','AllowedRejects','InspectAllContQty','InspectAllQtyOnFailure'))
            and now['CompletedSamples']==task['CompletedSamples']+snap['preview']['samples_this_submission']
            and now['Rejects']==task['Rejects']+snap['preview']['rejects_this_submission']and now['ChangeCount']==task['ChangeCount']+1)}
    return checks,{'container':row,'original_task':now,'task_missing':now is None,'note':'原任务ID、完整累计及恰好一次版本变化核对；同值回读仍须唯一父子原生历史。'}


async def verify(snap,attempt,candidate_txn_id=None):
    first,more=await roots(snap,limit=1)
    if more or not first:return {'comparison':'ambiguous'if more else'no_records','complete':False,'request_correlation_verified':False}
    root=first[0]
    if candidate_txn_id is not None and root['TxnId']!=candidate_txn_id:return {'comparison':'candidate_mismatch','complete':False,'request_correlation_verified':False}
    evidence=await transaction(root);checks=compare(snap,attempt,root,evidence)
    if await roots(snap,limit=1)!=(first,False)or await transaction(root)!=evidence:raise ValueError('检验组合父子历史读取期间变化。')
    matched=all(checks.values())
    return {'comparison':'native_inspection_effects_match'if matched else'mismatch','complete':True,'candidate_txn_id':root['TxnId'],
        'request_correlation_verified':matched,'recorded_business_effects_verified':matched,'checks':checks,'evidence':evidence,
        'group_semantics':'reason-bearing Rejects is a running parent total; reason child DataValue is the per-group increment; samples per group are declared input, root total and original task deltas are verified'}
