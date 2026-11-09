"""Isolated hierarchy loss acceptance: plan first; no mutation retry or production edits.

python scripts/quantity_loss_acceptance.py --prefix CDX-L4-YYYYMMDD-UNIQUE --output tmp/l4-new
python scripts/quantity_loss_acceptance.py --apply tmp/l4-new/manifest.json
Includes native carrier setup through FactoryMaint WCF and six loss scenarios.
Creates 20 top-level modeling/runtime objects, including the automatic tracking
Container. No existing objects are adopted; failed/unknown writes never replay.
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from config import CAMSTAR_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL,SAFE_CREATE_THRESHOLD
from core.http_client import get_client,get_headers,request,close_client
from core.transfer_attempt_lock import attempt_lock
from core import carrier_factory_wcf as wcf
from tools import quality as q,quality_plans as p,quality_history as history,quantity_loss_complex as x
from tools.bom_modeling import lookup
from tools.process_context import ReadContext
from tools.modeling_details import raw_json
from tools.container_start import request_container_start_selection_values


def objects(prefix):
    def ref(s,rev=False):return {'name':prefix+'-'+s,**({'revision':'1'} if rev else {})}
    def obj(entity,s,**body):return {'entity':entity,'body':{'name':prefix+'-'+s,'description':'Isolated Part 4 hierarchy loss acceptance',**body}}
    return [obj('ContainerLevels','LEAF',allowMove=True,isNameUnique=True),
        obj('ContainerLevels','PARENT',allowMove=True,isNameUnique=True,childLevels=[ref('LEAF')]),
        obj('LossReasons','LOSS'),obj('LossReasonGroups','GROUP',entries=[ref('LOSS')]),
        obj('Operations','OP',lossReasons=ref('GROUP'),thruputReportingLevel=ref('LEAF')),
        obj('Specs','SPEC',revision='1',status=1,operation=ref('OP')),
        obj('Workflows','WF',revision='1',status=1,steps=[{'@odata.type':'#modeling.SpecStepChanges','name':'Work','stepType':1,'sequence':1,'spec':ref('SPEC',True)}]),
        obj('Products','PRODUCT',revision='1',status=1,productType={'name':'BOX'},workflow=ref('WF',True),stdStartUOM={'name':'EACH'}),
        obj('MfgOrders','ORDER',qty=100,uom={'name':'EACH'},product=ref('PRODUCT',True)),
        obj('Factories','FACTORY'),obj('NumberingRules','CARRULE',lastAssignedSequence=0,lastAssignedSequenceHex='0',numberingRuleType=1,sequenceLength=4,prefix=prefix+'-CAR-',maximumValue=10)]


def containers(prefix):
    return [{'name':prefix+'-'+s,'qty':qty,'level':prefix+'-'+('PARENT' if parent else 'LEAF')} for s,qty,parent in
        [('PA',0,True),('L1',5,False),('L2',3,False),('PB',0,True),('L3',2,False),('L4',1,False),('CA',4,False)]]


async def plan(prefix,out):
    if not re.fullmatch(r'CDX-L4-[A-Z0-9-]{1,48}',prefix):raise ValueError('Use a unique CDX-L4- prefix, uppercase letters/numbers/hyphens.')
    if SAFE_CREATE_THRESHOLD<20:raise ValueError('Twenty new top-level objects exceed the configured threshold.')
    for obj in objects(prefix):
        if await lookup(obj['entity'],obj['body']['name']):raise ValueError('Fixture name exists: '+obj['body']['name'])
    if await lookup('Carriers',prefix+'-CARRIER'):raise ValueError('Carrier exists.')
    fixture_containers=containers(prefix)
    for item in fixture_containers+[{'name':prefix+'-CARRIER'}]:
        if await q.sql_rows('SELECT TOP 2 ContainerId FROM Container WHERE ContainerName='+q.literal(item['name']),2):raise ValueError('Container exists: '+item['name'])
    for entity,name in [('ProductTypes','BOX'),('UOMs','EACH')]:await ReadContext(2).resolve(entity,{'Name':name},revisioned=False)
    payload={'prefix':prefix,'environment':p.environment(),'created':time.time(),'modeling':objects(prefix),'containers':fixture_containers,'object_count':20,
        'scope':'child partial/full; whole-parent cascade; last-child close with AdjustThruput; carrier partial/full with shared-status detachment; native Batch prohibited'}
    out.mkdir(parents=True,exist_ok=True);path=out/'manifest.json'
    with path.open('x',encoding='utf-8') as f:json.dump({'digest':p.digest(payload),'payload':payload},f,ensure_ascii=False,indent=2)
    print('Read-only manifest saved:',path)


async def execute(path):
    envelope=json.loads(path.read_text(encoding='utf-8'));m=envelope['payload'];prefix=m['prefix']
    if envelope['digest']!=p.digest(m) or m['environment']!=p.environment() or time.time()-m['created']>3600:raise ValueError('Manifest changed, expired or targets a different environment.')
    if m['modeling']!=objects(prefix) or m['containers']!=containers(prefix) or m['object_count']!=20 or SAFE_CREATE_THRESHOLD<20 or not re.fullmatch(r'CDX-L4-[A-Z0-9-]{1,48}',prefix):raise ValueError('Unexpected fixture scope.')
    for obj in m['modeling']:
        if await lookup(obj['entity'],obj['body']['name']):raise ValueError('Fixture exists; never adopt/replay.')
    if await lookup('Carriers',prefix+'-CARRIER'):raise ValueError('Carrier exists; never adopt/replay.')
    for item in m['containers']+[{'name':prefix+'-CARRIER'}]:
        if await q.sql_rows('SELECT TOP 2 ContainerId FROM Container WHERE ContainerName='+q.literal(item['name']),2):raise ValueError('Container exists; never replay.')
    with attempt_lock(p.PLAN_DB,'acceptance:'+prefix),path.with_suffix('.journal.jsonl').open('x',encoding='utf-8') as journal:
        def log(stage,data):journal.write(json.dumps({'stage':stage,'at':time.time(),'data':data},ensure_ascii=False)+'\n');journal.flush()
        async def send(label,path,body,model=True,method='POST'):
            log(label+'_attempt',{'method':method,'path':path,'body':body})
            base=CAMSTAR_BASE_URL if model else CAMSTAR_SHOPFLOOR_BASE_URL
            response=await get_client().request(method,base.rstrip('/')+path,headers=get_headers(),json=body,follow_redirects=False)
            log(label+'_response',{'status':response.status_code,'response':response.text})
            if not 200<=response.status_code<300:raise ValueError('Stopped without retry: '+response.text)
        def ref(s,rev=False):return {'name':prefix+'-'+s,**({'revision':'1'}if rev else{})}
        created={}
        for obj in m['modeling']:
            entity,body=obj['entity'],obj['body'];await send(entity+'-'+body['name'],'/api/'+entity,body)
            current=await ReadContext(2).resolve(entity,{'Name':body['name'],**({'Revision':body['revision']}if 'revision'in body else{})},revisioned='revision'in body)
            created[body['name']]=current;log('model_readback',current)
        leaf=created[prefix+'-LEAF'];await send('level-link','/api/ContainerLevels/'+leaf['InstanceID'],{'parentLevels':[ref('PARENT')]},method='PUT')
        workflow=created[prefix+'-WF'];expanded=await ReadContext(1).get('Workflows',workflow['InstanceID'],{'$expand':'Steps'})
        steps=expanded.get('Steps');
        if not isinstance(steps,list) or len(steps)!=1 or steps[0].get('Name')!='Work':raise ValueError('Workflow identity/step mismatch.')
        await send('first-step','/api/Workflows/'+workflow['InstanceID'],{'firstStep':{'name':'Work','instanceId':steps[0]['InstanceID'],'parent':{'instanceId':workflow['InstanceID']}}},method='PUT')
        factory=created[prefix+'-FACTORY'];before=await wcf.load(factory['Name'],factory['InstanceID']);refs={}
        for field,entity,name,rev in [('isCarrierContainerLevel','ContainerLevels',prefix+'-PARENT',False),('isCarrierNumberingRule','NumberingRules',prefix+'-CARRULE',False),('isCarrierOwner','Owners','PRODUCTION',False),('isCarrierStartReason','StartReasons','NORMAL',False),('isCarrierWorkflow','Workflows',prefix+'-WF',True)]:
            obj=await ReadContext(2).resolve(entity,{'Name':name,**({'Revision':'1'} if rev else{})},revisioned=rev)
            refs[field]={'Name':obj['Name'],'ID':obj['InstanceID'],**({'Revision':'1','RevisionOfRecord':False} if rev else{})}
        desired=wcf.changes(prefix,refs);log('factory_carrier_config_attempt',{'before':before,'changes':desired})
        log('factory_carrier_config_verified',await wcf.configure(factory['Name'],factory['InstanceID'],before,desired))
        await send('create-carrier','/api/Carriers',{'name':prefix+'-CARRIER','description':'Isolated Part 4 carrier acceptance','factory':ref('FACTORY')})
        carrier=await ReadContext(2).resolve('Carriers',{'Name':prefix+'-CARRIER'},revisioned=False);log('carrier_readback',carrier)
        identities={}
        for item in m['containers']:
            order=await ReadContext(2).resolve('MfgOrders',{'Name':prefix+'-ORDER'},revisioned=False)
            if (order.get('Product')or{}).get('Name')!=prefix+'-PRODUCT' or order['Product'].get('Revision')!='1':raise ValueError('Order product/version changed.')
            await ReadContext(2).resolve('ContainerLevels',{'Name':item['level']},revisioned=False)
            context=dict(mfg_order_name=prefix+'-ORDER',product_name=prefix+'-PRODUCT',product_revision='1',qty=item['qty'],level_name=item['level'])
            status_context={'currentStatusDetails':{'factory':ref('FACTORY')}}
            context['body_json']=json.dumps(status_context)
            choices={}
            for field in ('Owner','StartReason'):
                data=json.loads(await request_container_start_selection_values('Details.'+field,**context));selection=data.get('Details',{}).get(field,{})
                rows=selection.get('SelectionValuesEx')
                if not isinstance(rows,list) or len(rows)!=1 or str(selection.get('RecordCount'))!='1':raise ValueError('Ambiguous Start selection; choose explicitly before proceeding: '+field)
                choices[field]=rows[0]['Name']
            body={'details':{'mfgOrder':ref('ORDER'),'product':ref('PRODUCT',True),'containerName':item['name'],'autoNumber':False,'qty':item['qty'],
                'level':{'name':item['level']},'owner':{'name':choices['Owner']},'startReason':{'name':choices['StartReason']}},**status_context}
            log('start_preflight',{'name':item['name'],'quantity':item['qty'],'level':item['level'],'owner':choices['Owner'],'start_reason':choices['StartReason'],'numbering':'explicit'})
            await send('Start-'+item['name'],'/api/Start',body,model=False)
            records=await q.sql_rows('SELECT ContainerId,ContainerName FROM Container WHERE ContainerName='+q.literal(item['name']),1)
            if len(records)!=1:raise ValueError('Started Container identity ambiguous.')
            current=await x.base.record(records[0]['ContainerId']);identities[item['name']]=current;log('start_readback',current)
        ca=identities[prefix+'-CA'];log('carrier_bind_attempt',{'carrier_id':carrier['InstanceID'],'container_id':ca['ContainerId'],'factory_id':factory['InstanceID']})
        await wcf.bind_acceptance_container(prefix,ca['ContainerId'],carrier['InstanceID'],factory['InstanceID'])
        view=await x.scope_view(ca['ContainerId']);x.validate_scope(view)
        if carrier['InstanceID'] not in view['carriers']:raise ValueError('Carrier binding did not persist.')
        log('carrier_bind_verified',view)
        for parent,children in [('PA',['L1','L2']),('PB',['L3','L4'])]:
            body={'container':ref(parent),'childContainers':[ref(s) for s in children],'comments':prefix+'-'+parent+'-ASSOC'}
            raw=await request('POST','/api/Associate',body=body,base_url=CAMSTAR_SHOPFLOOR_BASE_URL,params={'execute':'false','$select':'Container,ChildContainers'},response_formatter=raw_json)
            preview=json.loads(raw);log('associate_preview',preview)
            if preview.get('error') or not preview.get('data',{}).get('ChildContainers'):raise ValueError('Associate preview incomplete.')
            await send('Associate-'+parent,'/api/Associate',body,model=False)
            log('associate_readback',await x.scope_view(identities[prefix+'-'+parent]['ContainerId']))
        for label,target,allqty,quantity,close,adjust in [('child_partial','L1',False,'2',False,False),('child_full','L1',True,None,True,False),('parent_full','PB',True,None,True,False),('last_child_full','L2',True,None,True,True),('carrier_partial','CA',False,'1',False,False),('carrier_full','CA',True,None,True,False)]:
            preview=json.loads(await x.prepare_related_container_quantity_loss(prefix+'-'+target,prefix+'-LOSS',close,adjust,not label.startswith('carrier_'),True,loss_quantity=quantity,record_all_quantity=allqty));log(label+'_plan',preview)
            if not preview.get('plan_id'):raise ValueError(preview)
            args={k:preview[k] for k in ('plan_id','plan_digest')};result=json.loads(await p.execute_quality_transaction(**args));log(label+'_execute',result)
            proof=json.loads(await history.reconcile_quality_execution(**args));log(label+'_history',proof)
            if not proof.get('recorded_business_effects_verified'):raise ValueError('Unknown result; never replay: '+str(proof.get('checks',proof)))
            resolved=json.loads(await p.review_quality_transaction(**args,action='resolve_history',evidence_reference=proof['candidate_txn_id'],review_note='Isolated Part 4 original native history and complete hierarchy effects verified',conclusion='committed'));log(label+'_verified',resolved)
            if not resolved.get('verified_success'):raise ValueError('Current scope evidence incomplete; no retry.')
            print(label,'verified',flush=True)
        log('complete',{'passed':True});print('Hierarchy and carrier loss acceptance passed.')


async def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--prefix');ap.add_argument('--output',type=Path,default=Path('tmp/quantity-loss-new'));ap.add_argument('--apply',type=Path);args=ap.parse_args()
    try:
        if args.apply:await execute(args.apply)
        elif args.prefix:await plan(args.prefix,args.output)
        else:ap.error('--prefix to prepare or --apply to execute is required')
    finally:await close_client()


if __name__=='__main__':asyncio.run(main())
