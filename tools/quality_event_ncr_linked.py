"""Bind the native manufacturing NCR child-close path without extra writes."""
from decimal import Decimal,InvalidOperation,localcontext
from tools import quality as q,quality_event_update as u,quality_event_triage as t,quality_ncr_closure as n
from tools.quality_spc_annotations import iid,schema_check

SCHEMA={'EventData':'EventDataId,EventId,CDOTypeId,NonconformanceNumber,EventCustomDataId,ChangeCount',
 'EventLot':'EventLotId,EventDataId,ContainerId,IsContainer,Qty,ChangeCount',
 'EventDisposition':'EventDispositionId,ParentId,Qty,DispositionId,ChangeCount',
 'EventLotEventDispositions':'DispositionsId,EventLotId,FieldId,Sequence',
 'NCRResolutionCode':'NCRResolutionCodeId,ChangeCount'}

def number(value):
    if type(value) not in (str,int,float,Decimal):raise ValueError('批次/处置数量必须为明确十进制数。')
    if len(str(value))>128:raise ValueError('数量表达过长。')
    try:out=Decimal(str(value))
    except InvalidOperation:raise ValueError('数量不是十进制数。') from None
    if not out.is_finite() or out<=0 or out>Decimal('1e12'):raise ValueError('数量必须有限且大于0，不超过1e12。')
    if out.as_tuple().exponent < -28:raise ValueError('受限数量最多28位小数，不按容差补齐处置。')
    return out

def version(value):
    n=u.i.wire_int(value,'变更计数')
    if n<0:raise ValueError('变更计数不能为负。')
    return n

async def source(row,args,links):
    if row['Category']!=3 or not row['EventDataId']:raise ValueError('制造NCR联动仅支持Nonconformance类别3且有EventData的原事件。')
    await schema_check(SCHEMA)
    data=await q.sql_rows('SELECT TOP 1 '+SCHEMA['EventData']+f" FROM EventData WHERE EventDataId='{row['EventDataId']}'",1)
    if len(data)!=1 or not set(SCHEMA['EventData'].split(','))<=data[0].keys():raise ValueError('原事件数据缺失或字段不完整。')
    data=data[0]
    if iid(data.get('EventDataId'))!=row['EventDataId'] or iid(data.get('EventId'))!=row['EventId'] or u.i.wire_int(data.get('CDOTypeId'),'事件数据类型')!=8085:
        raise ValueError('事件数据身份/归属/类型不符。')
    if data.get('EventCustomDataId') not in (None,''):raise ValueError('存在自定义事件数据，不能使用受限NCR联动。')
    data['ChangeCount']=version(data.get('ChangeCount'))
    name=q.text(data.get('NonconformanceNumber'),'事件数据中的原NCR编号')
    if data['NonconformanceNumber']!=name:raise ValueError('事件中的NCR编号含额外空白，不猜测规范名称。')
    report=await q.ncr_snapshot(name)
    if not report or report.get('NonconformanceReportName')!=name or len(links)!=1 or iid(report.get('NonconformanceReportId'))!=links[0]['NonconformanceReportId']:
        raise ValueError('事件数据与唯一关联NCR不一致，不关闭替代报告。')
    if (iid(report.get('EscalatedEventId'))!=row['EventId'] or iid(report.get('ContainerId'))!=links[0]['ContainerId']
        or version(report.get('ChangeCount'))!=links[0]['ChangeCount'] or u.i.wire_int(report.get('NCRStatus'),'NCR状态')!=1
        or report.get('CloseDateGMT') not in (None,'') or not report.get('failure_code_assignments')):
        raise ValueError('原NCR必须Open、未关闭且原事件/容器/版本及完整失败码一致。')
    container=await n.container_state(report['ContainerId'])
    code=await n.resolution_state(args['quality_resolution_code_name'])
    if code['IsFrozen'] or code['ChangeCount']<0:raise ValueError('同名NCR解决代码已冻结或版本无效。')
    # The native child explicitly disables child signatures. Reject a required
    # 8081 signature rather than using that native shortcut to bypass policy.
    org=await t.asset('organization',key=row['OrganizationId'],name=args['organization_name'],signature_txn_type=8081)
    lots=await q.sql_rows('SELECT TOP 21 '+SCHEMA['EventLot']+f" FROM EventLot WHERE EventDataId='{row['EventDataId']}' ORDER BY EventLotId",21)
    if len(lots)>20:raise ValueError('关联批次超过20条，不用部分清单准备联动。')
    seen=set();details=[]
    for lot in lots:
        key=iid(lot.get('EventLotId'));qty=number(lot.get('Qty'))
        if key in seen or iid(lot.get('EventDataId'))!=row['EventDataId']:raise ValueError('批次重复或父级不符。')
        seen.add(key)
        if u.i.wire_bool(lot.get('IsContainer'),'容器批标志') is not True or iid(lot.get('ContainerId'))!=report['ContainerId']:
            raise ValueError('首版仅处理原NCR同一容器的批次，不接受其他容器或非容器批。')
        lot['ChangeCount']=version(lot.get('ChangeCount'))
        assignments=await q.sql_rows('SELECT TOP 51 '+SCHEMA['EventLotEventDispositions']+
            f" FROM EventLotEventDispositions WHERE EventLotId='{key}' ORDER BY Sequence,DispositionsId",51)
        dispositions=await q.sql_rows('SELECT TOP 51 '+SCHEMA['EventDisposition']+
            f" FROM EventDisposition WHERE ParentId='{key}' ORDER BY EventDispositionId",51)
        if not assignments or len(assignments)>50 or len(dispositions)>50:raise ValueError('批次处置清单为空或超过50条。')
        ids=[];sequences=[]
        for a in assignments:
            if iid(a.get('EventLotId'))!=key or u.i.wire_int(a.get('FieldId'),'处置字段')!=21122:raise ValueError('批次处置关联父级或字段不符。')
            ids.append(iid(a.get('DispositionsId')));sequences.append(version(a.get('Sequence')))
        total=Decimal(0);child_ids=[]
        for d in dispositions:
            child_ids.append(iid(d.get('EventDispositionId')))
            if iid(d.get('ParentId'))!=key:raise ValueError('处置父级不符。')
            iid(d.get('DispositionId'));d['ChangeCount']=version(d.get('ChangeCount'));
            with localcontext() as ctx:
                ctx.prec=160
                total+=number(d.get('Qty'))
        if len(set(ids))!=len(ids) or len(set(sequences))!=len(sequences) or len(set(child_ids))!=len(child_ids) or set(ids)!=set(child_ids):
            raise ValueError('处置关联重复或与完整子表不一致。')
        if total!=qty:raise ValueError('原生处置量未完整覆盖批次数量或存在超额处置；不自动补量、扣量或报废。')
        details.append(dict(lot=lot,assignments=assignments,dispositions=dispositions,covered_qty=str(total)))
    return dict(event_data=data,report=report,container=container,ncr_resolution=code,child_signature_policy=org,lots=details,
        child_service='NCRCloseQuality',child_service_type=8081,native_child_only=True,
        disposition_execution_verified=False,compound_transaction_atomicity_verified=False)
