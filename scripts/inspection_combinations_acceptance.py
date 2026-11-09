"""Isolated Part 6 fixture and single-attempt inspection runner.

--prefix CDX-L6-YYYYMMDD --output tmp/inspection-combinations creates a read-only
manifest. --apply <manifest> creates 17 roots. Five further roots are reserved
for the explicitly linked Event and its isolated configuration, total22.
--inspect <manifest> --case <case> submits one plan once; never replays a case.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from config import CAMSTAR_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL,SAFE_CREATE_THRESHOLD
from core.http_client import get_client,get_headers,close_client
from core.transfer_attempt_lock import attempt_lock
from tools import quality as q,quality_plans as p,quality_inspection as inspection,quality_quantity_loss as loss
from tools.process_context import ReadContext
from tools.bom_modeling import lookup
from tools.container_start import request_container_start_selection_values


def ref(prefix,suffix,revision=False):return {'name':prefix+'-'+suffix,**({'revision':'1'}if revision else{})}


def models(prefix):
    r=lambda s,rev=False:ref(prefix,s,rev)
    def obj(entity,suffix,**fields):return {'entity':entity,'body':{'name':prefix+'-'+suffix,'description':'Part 6 isolated inspection acceptance',**fields}}
    return [obj('LossReasons','A'),obj('LossReasons','B'),obj('LossReasonGroups','GROUP',entries=[r('A'),r('B')]),
        obj('Operations','OP',lossReasons=r('GROUP'),thruputReportingLevel={'name':'BOX'}),
        obj('Specs','SPEC',revision='1',status=1,operation=r('OP'),validateLotSamplingComplete=False,validateSamplingOverallResult=False),
        obj('Workflows','WF',revision='1',status=1,steps=[{'@odata.type':'#modeling.SpecStepChanges','name':'Inspect','stepType':1,'sequence':1,'spec':r('SPEC',True)}]),
        *[obj('SampleTests',test,revision='1',status=1,sampleType=1,aqlRejectReasons=r('GROUP'),allowMoveOnFailure=False,
            scrapCountedRejectsByReason=test=='REASONS',**({'decreaseBySampleSizeReason':r('A')}if test=='SAMPLE'else{'decreaseByRejectCountReason':r('A')}if test=='REJECT'else{}))for test in ('SAMPLE','REJECT','REASONS')],
        obj('AQLLevels','AQL',sampleSizeDetails=[{'sampleSizeCode':'2','sampleSize':'10','accepts':0,'rejects':1}]),
        obj('SamplingPlans','PLAN',revision='1',status=1,sampleRate=1,inspectionLevel={'name':'IQC_DEMO_LVL'},aqlLevel=r('AQL'),spec=r('SPEC',True),sampleSizeBasedOnStartQty=False,
            samplingPlanDetails=[{'sampleTest':r(test,True),'sampleRate':1,'inspectAll':True,'inspectAllContQty':False,'inspectAllQtyOnFailure':False,'spec':r('SPEC',True),'sequence':number}for number,test in enumerate(('SAMPLE','REJECT','REASONS'),1)]),
        obj('Products','PRODUCT',revision='1',status=1,productType={'name':'BOX'},workflow=r('WF',True),samplingPlan=r('PLAN',True),stdStartUOM={'name':'EACH'}),
        obj('MfgOrders','ORDER',qty=33,product=r('PRODUCT',True),uom={'name':'EACH'})]


def containers(prefix):return [{'name':prefix+'-'+s,'qty':qty}for s,qty in [('S',10),('R',10),('M',10),('Z',3)]]


def load(path):
    envelope=json.loads(path.read_text(encoding='utf8'));m=envelope['payload'];prefix=m['prefix']
    if not re.fullmatch(r'CDX-L6-[A-Z0-9-]{1,40}',prefix) or envelope['digest']!=p.digest(m) or m['environment']!=p.environment():raise ValueError('Manifest identity/environment changed.')
    canonical=models(prefix)
    legacy=[]
    for item in canonical:
        if item['entity']=='AQLLevels':continue
        item=json.loads(json.dumps(item))
        if item['entity']=='SamplingPlans':item['body']['aqlLevel']={'name':'IQC_DEMO_AQL'}
        legacy.append(item)
    original= m['models']==legacy and m['max_root_objects']==19 and any(v['stage']=='fixture_addendum'and v['data'].get('max_roots')==20 for v in saved(path))
    if (m['models']!=canonical or m['max_root_objects']not in (20,22)) and not original:raise ValueError('Unexpected fixture scope.')
    if m['containers']!=containers(prefix):raise ValueError('Unexpected Container scope.')
    return m


def log(path,stage,data):
    with path.with_suffix('.journal.jsonl').open('a',encoding='utf8') as f:
        f.write(json.dumps({'stage':stage,'at':time.time(),'data':data},ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())


def saved(path):
    file=path.with_suffix('.journal.jsonl')
    return [json.loads(s)for s in file.read_text(encoding='utf8').splitlines()]if file.exists()else[]


async def preflight(prefix):
    if not re.fullmatch(r'CDX-L6-[A-Z0-9-]{1,40}',prefix) or SAFE_CREATE_THRESHOLD<17:raise ValueError('Isolated prefix and threshold17 required for inspection fixtures; Event scope22 needs separate explicit authorization.')
    for obj in models(prefix):
        if await lookup(obj['entity'],obj['body']['name']):raise ValueError('Existing fixture model; no adoption.')
    for obj in containers(prefix):
        if await q.sql_rows('SELECT TOP 2 ContainerId FROM Container WHERE ContainerName='+q.literal(obj['name']),2):raise ValueError('Container exists.')
    for entity,name in [('ContainerLevels','BOX'),('UOMs','EACH'),('ProductTypes','BOX'),('InspectionLevels','IQC_DEMO_LVL'),('AQLLevels','IQC_DEMO_AQL')]:await ReadContext(2).resolve(entity,{'Name':name},revisioned=False)


async def plan(prefix,out):
    await preflight(prefix);out.mkdir(parents=True,exist_ok=True)
    m={'prefix':prefix,'created':time.time(),'environment':p.environment(),'models':models(prefix),'containers':containers(prefix),'fixture_root_objects':17,'reserved_event_roots':5,'max_root_objects':22}
    path=out/'manifest.json'
    with path.open('x',encoding='utf8')as f:json.dump({'digest':p.digest(m),'payload':m},f,ensure_ascii=False,indent=2)
    print('Read-only manifest:',path,'Explicit BOX/EACH Containers:',json.dumps(m['containers']),flush=True)


async def send(path,label,api,body,model=False,method='POST'):
    if any(v['stage']==label+'_attempt'for v in saved(path)):raise ValueError('Already attempted; reconcile, never replay: '+label)
    log(path,label+'_attempt',{'api':api,'method':method,'body':body})
    response=await get_client().request(method,(CAMSTAR_BASE_URL if model else CAMSTAR_SHOPFLOOR_BASE_URL).rstrip('/')+api,
        headers=get_headers(),json=body,follow_redirects=False)
    log(path,label+'_response',{'http_status':response.status_code,'body':response.text})
    if not 200<=response.status_code<300:raise ValueError('Stopped without retry: '+response.text[:2000])
    return response


async def apply(path):
    m=load(path);prefix=m['prefix'];r=lambda s,rev=False:ref(prefix,s,rev)
    if time.time()-m['created']>3600:raise ValueError('Fixture manifest expired.')
    await preflight(prefix)
    with attempt_lock(p.PLAN_DB,'l6-fixtures:'+prefix):
        objects={}
        for item in m['models']:
            entity,body=item['entity'],item['body'];await send(path,'model-'+body['name'],'/api/'+entity,body,True)
            obj=await ReadContext(2).resolve(entity,{'Name':body['name'],**({'Revision':'1'}if 'revision'in body else{})},revisioned='revision'in body)
            objects[body['name']]=obj;log(path,'model_verified',{'entity':entity,'object':obj})
        wf=objects[prefix+'-WF'];steps=(await ReadContext(1).get('Workflows',wf['InstanceID'],{'$expand':'Steps'}))['Steps']
        if len(steps)!=1 or steps[0]['Name']!='Inspect':raise ValueError('Unexpected workflow step set.')
        await send(path,'workflow-first','/api/Workflows/'+wf['InstanceID'],{'firstStep':{'name':'Inspect','instanceId':steps[0]['InstanceID'],'parent':{'instanceId':wf['InstanceID']}}},True,'PUT')
        level=await ReadContext(2).resolve('ContainerLevels',{'Name':'BOX'},revisioned=False)
        for item in m['containers']:
            product=await ReadContext(2).resolve('Products',{'Name':prefix+'-PRODUCT','Revision':'1'})
            order=await ReadContext(2).resolve('MfgOrders',{'Name':prefix+'-ORDER'},revisioned=False)
            if q.identifier(order['Product'])!=product['InstanceID'] or product['Revision']!='1' or float(order['Qty'])<33:raise ValueError('Order/product/version/capacity changed.')
            context=dict(mfg_order_name=order['Name'],product_name=product['Name'],product_revision='1',qty=item['qty'],level_name=level['Name'],
                body_json=json.dumps({'details':{'containerName':item['name'],'autoNumber':False}}))
            choices={}
            for field in ('Owner','StartReason'):
                values=json.loads(await request_container_start_selection_values('Details.'+field,**context))['Details'][field]
                if str(values['RecordCount'])!='1'or len(values['SelectionValuesEx'])!=1:raise ValueError('Ambiguous Start '+field)
                choices[field]=values['SelectionValuesEx'][0]['Name']
            body={'details':{'mfgOrder':r('ORDER'),'product':r('PRODUCT',True),'containerName':item['name'],'autoNumber':False,'qty':item['qty'],
                'level':{'name':level['Name']},'owner':{'name':choices['Owner']},'startReason':{'name':choices['StartReason']}}}
            log(path,'start_preflight',{'body':body,'mode':'explicit','owner':choices['Owner'],'start_reason':choices['StartReason']})
            await send(path,'start-'+item['name'],'/api/Start',body)
            rows=await q.sql_rows('SELECT ContainerId FROM Container WHERE ContainerName='+q.literal(item['name']),1)
            if len(rows)!=1:raise ValueError('Container identity missing/ambiguous.')
            record=await loss.record(rows[0]['ContainerId']);tasks=await q.sql_rows('SELECT '+inspection.TASK_SELECT+' FROM ContainerSampleData WHERE ContainerId='+q.literal(record['ContainerId'])+' ORDER BY SampleTestId',4)
            inspection.check_tasks(tasks,record['ContainerId'])
            if record['ProductId']!=product['InstanceID'] or float(record['Qty'])!=item['qty'] or len(tasks)!=3:raise ValueError('Start/task readback mismatch.')
            if any(v['TotalSamplesRequired']<min(item['qty'],6)or v['InspectAllContQty']or v['InspectAllQtyOnFailure']for v in tasks):raise ValueError('Native configured sampling requirement does not cover the declared fixture.')
            log(path,'container_created',{'record':record,'tasks':tasks});print('Started',item['name'],item['qty'],'tasks',len(tasks),flush=True)
        log(path,'fixtures_complete',{'created_roots':17,'reserved_roots':5});print('Isolated fixtures verified.',flush=True)


def case_args(prefix,case):
    a=lambda n,rej,reason=None:dict(samples_collected=n,rejects=rej,**({'reject_reason_name':prefix+'-'+reason}if reason else{}))
    args=dict(container_name=prefix+'-'+{'sample_warmup':'S','sample_groups':'S','reject_groups':'R','reason_groups':'M','zero':'Z'}[case],
        sample_test_name=prefix+'-'+{'sample_warmup':'SAMPLE','sample_groups':'SAMPLE','reject_groups':'REJECT','reason_groups':'REASONS','zero':'SAMPLE'}[case],sample_test_revision='1',allow_automatic_quantity_loss=True)
    if case=='sample_warmup':args['counted_groups']=[a(1,0)]
    elif case=='sample_groups':args['counted_groups']=[a(1,0)]
    elif case=='reject_groups':args['counted_groups']=[a(2,1,'A'),a(3,2,'B'),a(1,0)]
    elif case=='reason_groups':args.update(samples_collected=4,counted_reject_reasons=[{'reject_reason_name':prefix+'-A','rejects':1},{'reject_reason_name':prefix+'-B','rejects':2}])
    else:args.update(counted_groups=[a(3,0)],allow_zero_quantity_loss=True)
    return args


async def inspect_case(path,case):
    m=load(path);prefix=m['prefix'];entries=saved(path)
    if not any(v['stage']=='fixtures_complete'for v in entries):raise ValueError('Fixtures incomplete; no inspection.')
    if any(v['stage']==case+'_plan'and 'plan_id'in v['data']for v in entries):raise ValueError('Case already prepared/attempted; inspect its original plan, never replay.')
    ready=json.loads(await inspection.prepare_container_inspection(**case_args(prefix,case)));log(path,case+'_plan',ready)
    if 'plan_id'not in ready:raise ValueError(ready)
    args={k:ready[k]for k in ('plan_id','plan_digest')};log(path,case+'_execute',json.loads(await p.execute_quality_transaction(**args)))
    record=await loss.record(ready['request_preview']['body']['container']['instanceId'])
    log(path,case+'_current',record);print('Submitted once; reconciliation required:',json.dumps(args),flush=True)


async def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--prefix');ap.add_argument('--output',type=Path,default=Path('tmp/inspection-combinations'))
    ap.add_argument('--apply',type=Path);ap.add_argument('--inspect',type=Path);ap.add_argument('--case',choices=['sample_warmup','sample_groups','reject_groups','reason_groups','zero']);args=ap.parse_args()
    try:
        if args.apply:await apply(args.apply)
        elif args.inspect and args.case:await inspect_case(args.inspect,args.case)
        elif args.prefix:await plan(args.prefix,args.output)
        else:ap.error('Use --prefix, --apply, or --inspect and --case.')
    finally:await close_client()


if __name__=='__main__':asyncio.run(main())
