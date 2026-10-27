"""Prepare isolated mode-5 component requirements using existing creation tools.

Planning is read-only. --apply creates exactly two Products, one MfgOrder and
one explicit-name Container, then reads ComponentIssueInquiry. Never issues
materials, retries mutations, edits source objects or deletes test evidence.
"""
import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from config import CAMSTAR_BASE_URL, CAMSTAR_QUERY_BASE_URL, SAFE_CREATE_THRESHOLD
from core.http_client import request, close_client
from tools.modeling_details import raw_json, _has_next_link
from tools.process_context import ReadContext
from tools.products import create_product
from tools.mfgorders import create_mfgorder
from tools.container_start import container_start, request_container_start_selection_values
from tools.container_inquiry import get_container_details
from tools.component_issue import get_component_requirement


def named(ref,revisioned=False):
    out={'name':ref['Name']}
    if revisioned:out['revision']=ref['Revision']
    return out


def selection(raw,field):
    data=json.loads(raw)
    table=data.get('Details',{}).get(field,{})
    rows=table.get('SelectionValuesEx')
    if (not isinstance(rows,list) or len(rows)!=1 or str(table.get('RecordCount'))!='1'
            or str(table.get('RowCount'))!='1' or _has_next_link(data)
            or not rows[0].get('Name') or not rows[0].get('InstanceId')):
        raise ValueError('Start '+field+'不是完整唯一选择，不能自主选择或开始创建。')
    return rows[0]['Name']


async def prepare(prefix,output,apply,resume=False,resume_start=False):
    if not re.fullmatch(r'CDX-T05-[A-Z0-9-]{1,45}',prefix):
        raise ValueError('Prefix must be a short CDX-T05-... uppercase name.')
    output.mkdir(parents=True,exist_ok=True)
    journal_path=output/(prefix+'.json')
    previous=None
    if resume and resume_start:raise ValueError('Choose one controlled continuation mode.')
    if resume or resume_start:
        previous=json.loads(journal_path.read_text(encoding='utf-8'))
        attempts=previous.get('attempts',[])
        if resume_start:
            orders=[a for a in attempts if a.get('kind')=='MfgOrder']
            if (previous.get('environment')!=CAMSTAR_BASE_URL.rstrip('/') or not orders
                    or any(a.get('kind')=='Container' for a in attempts)):
                raise ValueError('Continue-Start requires a recorded order and no previous Container attempt.')
            try:reply=json.loads(orders[-1]['response'])
            except (ValueError,KeyError):raise ValueError('Order response is ambiguous; cannot continue Start.') from None
            if not isinstance(reply,dict) or not isinstance(reply.get('value'),str) or ' added ' not in reply['value']:
                raise ValueError('Order creation is not acknowledged; no Start allowed.')
    if resume:
        failed=[a for a in attempts if a.get('state')!='created_and_read_verified']
        if (previous.get('environment')!=CAMSTAR_BASE_URL.rstrip('/') or previous.get('state')!='stopped_without_retry'
                or len(failed)!=1 or failed[0].get('kind')!='MfgOrder'):
            raise ValueError('Resume only supports the recorded pre-Start order schema failure.')
        try:error=json.loads(failed[0]['response'].split('Response: ',1)[1])
        except (ValueError,KeyError,IndexError):raise ValueError('Ambiguous result cannot be replayed.') from None
        if str(error.get('ErrorCode'))!='13369346' or not any(x.get('Message')=='Write access denied on field IsPhantom' for x in error.get('Details',[])):
            raise ValueError('Only the diagnosed IsPhantom field-access failure may be corrected by this resume path.')
    if apply and journal_path.exists() and not (resume or resume_start):
        raise ValueError('Journal already exists; inspect previous attempt rather than replaying mutations.')
    ctx=ReadContext(max_requests=20)
    source=await ctx.resolve('Products',{'Name':'WM-900A','Revision':'A'})
    workflow=await ctx.resolve('Workflows',source['Workflow'])
    product_type=await ctx.resolve('ProductTypes',source['ProductType'],revisioned=False)
    uom=await ctx.resolve('UOMs',{'Name':'EACH'},revisioned=False)
    level=await ctx.resolve('ContainerLevels',{'Name':'BOX'},revisioned=False)
    status=await ctx.resolve('OrderStatuses',{'Name':'Released'},revisioned=False)
    names={'component':prefix+'-COMP','target':prefix+'-TARGET','order':prefix+'-MO','container':prefix+'-C01'}
    if previous and previous.get('names')!=names:raise ValueError('Journal names do not match the isolated fixture prefix.')
    for entity,key in [('Products','component'),('Products','target'),('MfgOrders','order')]:
        found=await ctx.get(entity,params={'$filter':"Name eq '"+names[key]+"'",'$top':2})
        if previous and key in ('component','target'):
            verified=[a for a in previous['attempts'] if a.get('name')==names[key] and a.get('state')=='created_and_read_verified']
            rows=found.get('value')
            if len(verified)!=1 or not isinstance(rows,list) or len(rows)!=1 or rows[0].get('InstanceID')!=verified[0]['verified_object']['InstanceID'] or _has_next_link(found):
                raise ValueError('Previously created product identity is not uniquely preserved.')
            continue
        if resume_start and key=='order':
            if not isinstance(found.get('value'),list) or len(found['value'])!=1 or _has_next_link(found):
                raise ValueError('Recorded test order must still uniquely exist.')
            continue
        if not isinstance(found.get('value'),list) or found['value'] or _has_next_link(found):
            raise ValueError('Name already exists or absence cannot be verified: '+names[key])
    raw=await request('POST','/api/AdHoc',base_url=CAMSTAR_QUERY_BASE_URL,
        body={'queryText':"SELECT TOP 2 ContainerId,ContainerName FROM Container WHERE ContainerName=N'"+names['container']+"'"},
        response_formatter=raw_json,retry_read_only=True)
    if json.loads(raw)!=[]:raise ValueError('Container name is occupied or lookup failed.')
    # Resolve a unique selection in an existing, validated Start context before
    # any mutation. Repeat in the new context before the actual Start.
    start_context={'mfg_order_name':'MO-100076435','product_name':source['Name'],'product_revision':source['Revision'],
                   'qty':1,'level_name':level['Name']}
    owner=selection(await request_container_start_selection_values('Details.Owner',**start_context),'Owner')
    reason=selection(await request_container_start_selection_values('Details.StartReason',**start_context),'StartReason')
    # Current ProductEntity has StdStartUOM, but no UOM property. The older
    # Product OpenAPI incorrectly exposed uom; use live entity properties.
    product_extra={'StdStartUOM':named(uom)}
    material={'name':'T05-QTY','product':{'name':names['component'],'revision':'1'},'qtyRequired':2,
              'qty2Required':0,'issueControl':5,'UOM':named(uom)}
    plan={'created_at_utc':datetime.now(timezone.utc).isoformat(),'environment':CAMSTAR_BASE_URL.rstrip('/'),
          'authorization':'User explicitly delegated creation of isolated test fixtures.',
          'names':names,'count':4,'quantity_per_container':1,'material_quantity':2,'issue_control':5,
          'source_workflow':{k:workflow.get(k) for k in ('Name','Revision','InstanceID')},
          'product_type':product_type['Name'],'uom':uom['Name'],'level':level['Name'],'owner':owner,'start_reason':reason,
          'order_status':status['Name'],'material_row':material,'attempts':previous['attempts'] if previous else [],
          'note':'No ComponentIssue consumption. No source edits, mutation retries or automatic deletion.'}
    if not apply:
        (output/(prefix+'-plan.json')).write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(plan,ensure_ascii=True));return
    if plan['count']>SAFE_CREATE_THRESHOLD:
        raise ValueError('Fixture count exceeds the configured confirmation threshold; explicit batch review required.')
    def save():journal_path.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    async def create(kind,name,fn,args,verify):
        record={'kind':kind,'name':name,'arguments':args,'state':'attempting'}
        plan['attempts'].append(record);save()
        try:
            record['response']=await fn(**args);save()
            record['verified_object']=await verify()
            record['state']='created_and_read_verified';save()
        except BaseException as error:
            record.update(state='unknown_or_failed',error=type(error).__name__+': '+str(error));save()
            raise
    async def verify_product(name,target):
        obj=await ReadContext().resolve('Products',{'Name':name,'Revision':'1'})
        if obj.get('StdStartUOM',{}).get('Name')!=uom['Name'] or obj.get('ProductType',{}).get('Name')!=product_type['Name']:
            raise ValueError('Created product units/type mismatch.')
        if target and (obj.get('Workflow',{}).get('Name')!=workflow['Name'] or obj['Workflow'].get('Revision')!=workflow['Revision']):
            raise ValueError('Target product workflow mismatch.')
        return {k:obj.get(k) for k in ('Name','Revision','InstanceID','StdStartUOM','Workflow')}
    async def verify_order():
        c=ReadContext();obj=await c.resolve('MfgOrders',{'Name':names['order']},revisioned=False)
        expanded=await c.get('MfgOrders',obj['InstanceID'],{'$expand':'MaterialList'})
        rows=expanded.get('MaterialList')
        # This server projects non-expanded root fields to null when $expand is
        # used. Validate the root from the ordinary read and rows from expand.
        if ((obj.get('Product') or {}).get('Name')!=names['target'] or obj.get('Qty')!=1
                or expanded.get('InstanceID')!=obj['InstanceID'] or not isinstance(rows,list) or len(rows)!=1
                or rows[0].get('Name')!='T05-QTY' or rows[0].get('IssueControl')!=5
                or rows[0].get('QtyRequired')!=2 or rows[0].get('Product',{}).get('Name')!=names['component']
                or rows[0].get('UOM',{}).get('Name')!=uom['Name'] or _has_next_link(expanded)):
            raise ValueError('Created order/material row mismatch; stop before Start.')
        return {**{k:obj.get(k) for k in ('Name','InstanceID','Product','Qty')},'MaterialList':rows}
    try:
        for key,target in [('component',False),('target',True)]:
            if previous:
                await verify_product(names[key],target)
                continue
            extra=dict(product_extra)
            if target:extra['workflow']=named(workflow,True)
            args={'name':names[key],'revision':'1','product_type_name':product_type['Name'],'status':1,
                  'description':'Codex isolated T05 acceptance fixture; no production use.', 'body_json':json.dumps(extra)}
            await create('Product',names[key],create_product,args,lambda key=key,target=target:verify_product(names[key],target))
        args={'name':names['order'],'product_name':names['target'],'product_revision':'1','qty':1,
              'description':'Codex isolated T05 test order; quantity-only material requirement.',
              'order_status_name':status['Name'],'body_json':json.dumps({'UOM':named(uom),'materialList':[material]})}
        if resume_start:
            verified=await verify_order()
            latest=next(a for a in reversed(plan['attempts']) if a['kind']=='MfgOrder')
            latest['original_verification_error']=latest.get('error')
            latest.update(verified_object=verified,state='created_and_read_verified',verification_repaired_without_reposting=True)
            save()
        else:
            await create('MfgOrder',names['order'],create_mfgorder,args,verify_order)
        start_context.update(mfg_order_name=names['order'],product_name=names['target'],product_revision='1')
        for expr,field,wanted in [('Details.Owner','Owner',owner),('Details.StartReason','StartReason',reason)]:
            if selection(await request_container_start_selection_values(expr,**start_context),field)!=wanted:
                raise ValueError('New-context Start reference differs; stop before creating Container.')
        async def verify_container():
            result=json.loads(await get_container_details(names['container'],view='status'))
            data=result.get('data',{}).get('ContainerStatusDetails',{})
            if (result.get('error') or data.get('Container',{}).get('Name')!=names['container'] or data.get('Qty')!=1
                    or data.get('Product',{}).get('Name')!=names['target'] or data.get('Product',{}).get('Revision')!='1'
                    or data.get('MfgOrder',{}).get('Name')!=names['order'] or data.get('ContainerLevel',{}).get('Name')!=level['Name']
                    or data.get('UOM',{}).get('Name')!=uom['Name']):
                raise ValueError('Container state identity/context mismatch.')
            return data
        args={'mfg_order_name':names['order'],'product_name':names['target'],'product_revision':'1','qty':1,
              'level_name':level['Name'],'owner_name':owner,'start_reason_name':reason,
              'container_name':names['container'],'comments':'Isolated Codex T05 acceptance fixture.'}
        await create('Container',names['container'],container_start,args,verify_container)
        plan['requirement_result']=json.loads(await get_component_requirement(names['container'],names['component']))
        plan['state']='fixtures_created_requirement_'+plan['requirement_result'].get('status','unknown');save()
    except BaseException:
        plan['state']='stopped_without_retry';save();raise
    finally:await close_client()
    print(json.dumps({'state':plan['state'],'names':names,'created_verified':sum(a['state']=='created_and_read_verified' for a in plan['attempts'])},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'docs'/'t05-fixtures')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--resume',action='store_true',help='Only resume the journaled IsPhantom schema failure after read-verifying object identities/absence.')
    parser.add_argument('--resume-start',action='store_true',help='Read-verify an acknowledged existing order and continue only an unattempted Start; no Model creates are replayed.')
    args=parser.parse_args()
    asyncio.run(prepare(args.prefix,args.output,args.apply,args.resume,args.resume_start))
