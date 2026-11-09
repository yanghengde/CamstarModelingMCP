"""Part 5 isolated positive/negative acceptance; plan first, never replay writes.

--prefix CDX-L5-YYYYMMDD --output tmp/lot-scrap-new prepares a read-only manifest.
--apply <manifest.json> creates exactly 17 new root objects and tests both lots.
The intentional held-member rejection uses a native validation request in this
bounded test harness. It is not an MCP bypass or a general retry facility.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from config import CAMSTAR_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL,SAFE_CREATE_THRESHOLD
from core.http_client import get_client,get_headers,close_client
from core.transfer_attempt_lock import attempt_lock
from tools import quality as q,quality_plans as p,quality_lots as lots,quality_lot_execution as e
from tools import quality_lot_scrap as scrap,quality_quantity_loss as loss,quality_history as history
from tools import quality_disposition_history as native,quality_lot_rework_evidence as cleanup
from tools.process_context import ReadContext
from tools.bom_modeling import lookup
from tools.container_start import request_container_start_selection_values


def ref(prefix,suffix,revision=False):
    return {'name':prefix+'-'+suffix,**({'revision':'1'} if revision else{})}


def models(prefix):
    r=lambda s,rev=False:ref(prefix,s,rev)
    def item(entity,s,**fields):return {'entity':entity,'body':{'name':prefix+'-'+s,'description':'Part 5 isolated scrap acceptance',**fields}}
    return [item('LossReasons','LOSS'),item('LossReasonGroups','GROUP',entries=[r('LOSS')]),
        item('HoldReasons','HOLD'),item('ReleaseReasons','RELEASE'),
        item('Operations','OP',thruputReportingLevel={'name':'BOX'},lossReasons=r('GROUP')),
        item('Specs','SPEC',revision='1',status=1,operation=r('OP'),validateLotSamplingComplete=False,validateSamplingOverallResult=False),
        item('Workflows','WF',revision='1',status=1,steps=[{'@odata.type':'#modeling.SpecStepChanges','name':'Inspect','stepType':1,'sequence':1,'spec':r('SPEC',True)}]),
        item('SampleTests','TEST',revision='1',status=1,sampleType=1,aqlRejectReasons=r('GROUP'),allowMoveOnFailure=False,scrapCountedRejectsByReason=False),
        item('SamplingPlans','PLAN',revision='1',status=1,sampleRate=1,inspectionLevel={'name':'IQC_DEMO_LVL'},aqlLevel={'name':'IQC_DEMO_AQL'},
             spec=r('SPEC',True),sampleSizeBasedOnStartQty=False,samplingPlanDetails=[{'sampleTest':r('TEST',True),'sampleRate':1,'inspectAll':False,'inspectAllContQty':False,'inspectAllQtyOnFailure':False,'spec':r('SPEC',True),'sequence':1}]),
        item('Products','PRODUCT',revision='1',status=1,productType={'name':'BOX'},workflow=r('WF',True),samplingPlan=r('PLAN',True),stdStartUOM={'name':'EACH'}),
        item('MfgOrders','ORDER',qty=10,product=r('PRODUCT',True),uom={'name':'EACH'})]


def containers(prefix):
    return [{'name':prefix+'-'+s,'lot':prefix+'-'+lot,'qty':qty} for s,lot,qty in [('N01','NEG',2),('N02','NEG',3),('P01','POS',2),('P02','POS',3)]]


async def preflight(prefix):
    if not re.fullmatch(r'CDX-L5-[A-Z0-9-]{1,40}',prefix) or SAFE_CREATE_THRESHOLD<17:raise ValueError('Isolated prefix/17-object threshold required.')
    for item in models(prefix):
        if await lookup(item['entity'],item['body']['name']):raise ValueError('Existing model; do not adopt or replay.')
    for item in containers(prefix):
        if await q.sql_rows('SELECT TOP 2 ContainerId FROM Container WHERE ContainerName='+q.literal(item['name']),2):raise ValueError('Container name exists.')
    for suffix in ('NEG','POS'):
        if await lots.lot_record(prefix+'-'+suffix):raise ValueError('Lot name exists.')
    for entity,name in [('InspectionLevels','IQC_DEMO_LVL'),('AQLLevels','IQC_DEMO_AQL'),('ContainerLevels','BOX'),('ProductTypes','BOX'),('UOMs','EACH')]:
        await ReadContext(2).resolve(entity,{'Name':name},revisioned=False)


async def plan(prefix,output):
    await preflight(prefix)
    payload={'prefix':prefix,'environment':p.environment(),'created':time.time(),'models':models(prefix),'containers':containers(prefix),'lots':[prefix+'-NEG',prefix+'-POS'],'object_count':17}
    output.mkdir(parents=True,exist_ok=True)
    with (output/'manifest.json').open('x',encoding='utf8') as f:json.dump({'digest':p.digest(payload),'payload':payload},f,ensure_ascii=False,indent=2)
    print('Read-only manifest:',output/'manifest.json',flush=True)
    print('17 root objects; explicit BOX Containers:',json.dumps(containers(prefix),ensure_ascii=False),flush=True)


async def capture(lot_id,ids):
    clause=','.join(q.literal(cid) for cid in ids)
    queries={
        'mainlines':'SELECT TOP 101 '+native.MAINLINE+' FROM HistoryMainline WHERE ContainerId IN ('+clause+') OR HistoryId='+q.literal(lot_id)+' ORDER BY HistoryMainlineId',
        'summaries':'SELECT TOP 101 '+native.SUMMARY+' FROM QtyHistory WHERE HistoryId IN ('+clause+') ORDER BY QtyHistoryId',
        'details':'SELECT TOP 101 '+native.DETAIL+' FROM QtyHistoryDetails WHERE ContainerId IN ('+clause+') ORDER BY QtyHistoryDetailsId',
        'lot_history':'SELECT TOP 101 '+native.LOT+' FROM ExecuteSamplingLotHistory WHERE HistoryId='+q.literal(lot_id)+' ORDER BY ExecuteSamplingLotHistoryId'}
    view=await e.lot_view('',lot_id)
    if sorted(r['ContainerId'] for r in view['members'])!=sorted(ids):raise ValueError('Original full membership changed.')
    out={'lot':view,'members':[await loss.record(cid) for cid in ids],'member_tasks':await cleanup.sampling_tasks(view['members'])}
    for key,sql in queries.items():
        out[key]=await q.sql_rows(sql,101)
        if len(out[key])>100:raise ValueError('Evidence bound exceeded; no partial comparison.')
    return out


async def execute(path):
    envelope=json.loads(path.read_text(encoding='utf8'));m=envelope['payload'];prefix=m['prefix']
    if envelope['digest']!=p.digest(m) or m['environment']!=p.environment() or time.time()-m['created']>3600:raise ValueError('Changed/expired manifest or different environment.')
    if m['models']!=models(prefix) or m['containers']!=containers(prefix) or m['lots']!=[prefix+'-NEG',prefix+'-POS'] or m['object_count']!=17:raise ValueError('Unexpected fixture scope.')
    await preflight(prefix)
    r=lambda s,rev=False:ref(prefix,s,rev)
    with attempt_lock(p.PLAN_DB,'l5-acceptance:'+prefix),path.with_suffix('.journal.jsonl').open('x',encoding='utf8') as journal:
        def log(stage,data):
            journal.write(json.dumps({'stage':stage,'at':time.time(),'data':data},ensure_ascii=False)+'\n');journal.flush();os.fsync(journal.fileno())
        attempted=set()
        async def send(label,api,body,model=False,method='POST',allow_rejection=False):
            if label in attempted:raise ValueError('Never replay a mutation.')
            attempted.add(label);log(label+'_attempt',{'method':method,'api':api,'body':body})
            response=await get_client().request(method,(CAMSTAR_BASE_URL if model else CAMSTAR_SHOPFLOOR_BASE_URL).rstrip('/')+api,
                headers=get_headers(),json=body,follow_redirects=False)
            log(label+'_response',{'http_status':response.status_code,'body':response.text})
            if not 200<=response.status_code<300 and not allow_rejection:raise ValueError('Stopped without retry: '+response.text[:2000])
            return response
        async def native_setup(label,kind,args):
            snap=await p.build(kind,args,'MCP-QUAL:'+uuid.uuid4().hex);route=p.transport(snap)
            log(label+'_validated_input',snap)
            await send(label,route['path'],route.get('body',snap['body']))
            return snap
        created={}
        for item in m['models']:
            entity,body=item['entity'],item['body'];await send('model-'+body['name'],'/api/'+entity,body,model=True)
            obj=await ReadContext(2).resolve(entity,{'Name':body['name'],**({'Revision':'1'} if 'revision'in body else{})},revisioned='revision'in body)
            created[body['name']]=obj;log('model_verified',{'entity':entity,'object':obj})
        wf=created[prefix+'-WF'];steps=(await ReadContext(1).get('Workflows',wf['InstanceID'],{'$expand':'Steps'}))['Steps']
        if len(steps)!=1 or steps[0]['Name']!='Inspect':raise ValueError('Unexpected workflow step.')
        await send('workflow-first','/api/Workflows/'+wf['InstanceID'],{'firstStep':{'name':'Inspect','instanceId':steps[0]['InstanceID'],'parent':{'instanceId':wf['InstanceID']}}},model=True,method='PUT')
        lot_records={}
        for name in m['lots']:
            await native_setup('create-'+name,'sampling_lot',dict(lot_name=name,product_name=prefix+'-PRODUCT',product_revision='1',quantity='5',uom_name='EACH',sample_rate=1,allow_adjust_to_lot_size=False))
            lot=await lots.lot_record(name)
            if not lot or lot['ProductId']!=created[prefix+'-PRODUCT']['InstanceID'] or loss.i.numeric(lot['Qty'],'lot qty')!=5:raise ValueError('Lot create identity mismatch.')
            lot_records[name]=lot;log('lot_created',lot)
        level=await ReadContext(2).resolve('ContainerLevels',{'Name':'BOX'},revisioned=False)
        identities={}
        for item in m['containers']:
            product=await ReadContext(2).resolve('Products',{'Name':prefix+'-PRODUCT','Revision':'1'},revisioned=True)
            order=await ReadContext(2).resolve('MfgOrders',{'Name':prefix+'-ORDER'},revisioned=False)
            if q.identifier(order.get('Product'))!=product['InstanceID'] or product['Revision']!='1' or float(order['Qty'])<sum(v['qty'] for v in m['containers']):raise ValueError('Start order/product/revision/capacity mismatch.')
            lot=lot_records[item['lot']];extra={'details':{'samplingLot':{'name':lot['MfgLotName'],'instanceId':lot['MfgLotId']},'containerName':item['name'],'autoNumber':False}}
            context=dict(mfg_order_name=order['Name'],product_name=product['Name'],product_revision='1',qty=item['qty'],level_name=level['Name'],body_json=json.dumps(extra))
            chosen={}
            for field in ('Owner','StartReason'):
                selection=json.loads(await request_container_start_selection_values('Details.'+field,**context))['Details'][field]
                values=selection['SelectionValuesEx']
                if str(selection['RecordCount'])!='1' or len(values)!=1:raise ValueError('Ambiguous contextual Start '+field)
                chosen[field]=values[0]['Name']
            available=json.loads(await request_container_start_selection_values('Details.SamplingLot',**context))['Details']['SamplingLot']['SelectionValuesEx']
            if sum(v.get('Name')==lot['MfgLotName'] and q.identifier(v)==lot['MfgLotId'] for v in available)!=1:raise ValueError('Target lot unavailable in Start context.')
            body={'details':{'mfgOrder':r('ORDER'),'product':r('PRODUCT',True),'containerName':item['name'],'autoNumber':False,'qty':item['qty'],
                'level':{'name':level['Name']},'owner':{'name':chosen['Owner']},'startReason':{'name':chosen['StartReason']},'samplingLot':extra['details']['samplingLot']}}
            log('start_preflight',{'body':body,'mode':'explicit','owner':chosen['Owner'],'start_reason':chosen['StartReason']})
            await send('start-'+item['name'],'/api/Start',body)
            found=await q.sql_rows('SELECT ContainerId FROM Container WHERE ContainerName='+q.literal(item['name']),1)
            if len(found)!=1:raise ValueError('Started Container missing/ambiguous.')
            record=await loss.record(found[0]['ContainerId'])
            if record['ProductId']!=product['InstanceID'] or record['SamplingLotId']!=lot['MfgLotId'] or loss.i.numeric(record['Qty'],'qty')!=item['qty']:raise ValueError('Start readback mismatch.')
            identities[item['name']]=record;log('container_created',record)
        for suffix in ('NEG','POS'):
            view=await e.lot_view(prefix+'-'+suffix)
            expected={identities[v['name']]['ContainerId'] for v in m['containers'] if v['lot']==prefix+'-'+suffix}
            if {v['ContainerId'] for v in view['members']}!=expected or len(view['tasks'])!=1:raise ValueError('Unexpected native member/task set.')
            task=view['tasks'][0];need=task['AllowedRejects']+1
            if not 1<=need<=min(100,task['TotalSamplesRequired'],5):raise ValueError('Isolated test sampling threshold is outside the declared fixture.')
            members=view['members']
            for number in range(need):
                member=members[number%len(members)]
                await native_setup('reject-'+suffix+'-'+str(number),'lot_inspection',dict(lot_name=view['lot']['MfgLotName'],lot_instance_id=view['lot']['MfgLotId'],container_name=member['ContainerName'],container_instance_id=member['ContainerId'],
                    sample_test_name=prefix+'-TEST',sample_test_revision='1',lot_sample_data_instance_id=task['LotSampleDataId'],samples_collected=1,rejects=1,reject_reason_name=prefix+'-LOSS',samples=None,counted_groups=None,comments='Part 5 isolated test rejection'))
            failed=await e.lot_view(prefix+'-'+suffix)
            if failed['lot']['SamplingPassed'] is not False or failed['tasks'][0]['Rejects']<=failed['tasks'][0]['AllowedRejects']:raise ValueError('Native failure threshold not reached.')
            log('failed_test_lot_ready',failed)
        # Test a stale client plan AND the actual native scrap rejection on NEG.
        ready=json.loads(await scrap.prepare_sampling_lot_scrap(prefix+'-NEG',prefix+'-LOSS'))
        if 'plan_id'not in ready:raise ValueError(ready)
        log('negative_stale_plan',ready);view=await e.lot_view(prefix+'-NEG');ids=[v['ContainerId'] for v in view['members']]
        initial=await capture(view['lot']['MfgLotId'],ids);log('negative_initial',initial)
        last=view['members'][-1];target={'name':last['ContainerName'],'instanceId':last['ContainerId']}
        await send('negative-hold','/api/Hold',{'container':target,'holdReason':r('HOLD'),'comments':'MCP-L5-HOLD:'+uuid.uuid4().hex})
        release_safe=False
        try:
            held=await capture(view['lot']['MfgLotId'],ids);log('negative_held_baseline',held)
            if [v['CurrentHoldCount'] for v in held['members']]!=[0,1] or not held['member_tasks']:raise ValueError('Held member/task baseline not confirmed.')
            stopped=json.loads(await p.execute_quality_transaction(ready['plan_id'],ready['plan_digest']));log('negative_stale_blocked',stopped)
            with p.db_connect() as db:attempts=db.execute('SELECT COUNT(*) FROM attempts WHERE plan_id=?',(ready['plan_id'],)).fetchone()[0]
            if stopped.get('submitted') is not False or attempts or await capture(view['lot']['MfgLotId'],ids)!=held:raise ValueError('Stale client plan was not stopped before submission.')
            release_safe=True
            body={**ready['request_preview']['body'],'comments':'MCP-QUAL:'+uuid.uuid4().hex}
            release_safe=False  # A timeout may leave native execution running.
            response=await send('negative-native-scrap','/api/ExecuteSamplingLot',body,allow_rejection=True)
            after=await capture(view['lot']['MfgLotId'],ids);log('negative_after_rejection',after)
            roots=await q.sql_rows('SELECT TOP 2 HistoryMainlineId,TxnId FROM HistoryMainline WHERE Comments='+q.literal(body['comments']),2)
            verdict={'native_rejected':response.status_code==400 and '13500440'in response.text and last['ContainerName'] in response.text,
                'whole_scope_unchanged':after==held,'stable_second_read':await capture(view['lot']['MfgLotId'],ids)==after,'no_failed_request_history':not roots,'client_submission_attempts':attempts,
                'universal_atomicity_proven':False,'note':'Only this controlled held-member scrap rejection is verified; no timeout/crash guarantee inferred.'}
            log('negative_verdict',verdict)
            if not all(verdict[k] for k in ('native_rejected','whole_scope_unchanged','stable_second_read','no_failed_request_history')):raise ValueError('Negative result needs reconciliation; never replay.')
            release_safe=True
        finally:
            current=await loss.record(last['ContainerId'])
            if release_safe and current and current['CurrentHoldCount']==1:
                await send('negative-release','/api/Release',{'container':target,'releaseReason':r('RELEASE'),'comments':'MCP-L5-RELEASE:'+uuid.uuid4().hex})
                released=await capture(view['lot']['MfgLotId'],ids);log('negative_released',released)
                if any(v['CurrentHoldCount'] for v in released['members']):raise ValueError('Test hold release not confirmed.')
            elif not release_safe:
                log('negative_release_deferred',{'container_id':last['ContainerId'],'reason':'No verified terminal rejection; do not mutate while the native outcome may be unknown.'})
        print('Native scrap rejection: original quantities, states, tasks and history unchanged; hold released.',flush=True)
        # Separate POS lot: a real immutable plan, exactly one native transaction,
        # then verified parent/child history plus all original member effects.
        ready=json.loads(await scrap.prepare_sampling_lot_scrap(prefix+'-POS',prefix+'-LOSS'));log('positive_plan',ready)
        if 'plan_id'not in ready:raise ValueError(ready)
        args={k:ready[k] for k in ('plan_id','plan_digest')}
        log('positive_execute',json.loads(await p.execute_quality_transaction(**args)))
        proof=json.loads(await history.reconcile_quality_execution(**args));log('positive_history',proof)
        if not proof.get('recorded_business_effects_verified'):raise ValueError('Positive attempt is unknown; never replay.')
        resolved=json.loads(await p.review_quality_transaction(**args,action='resolve_history',conclusion='committed',evidence_reference=proof['candidate_txn_id'],review_note='Part 5 isolated native parent and complete member loss history plus current quantities, status and task cleanup verified'))
        log('positive_resolved',resolved)
        if not resolved.get('verified_success'):raise ValueError('Positive current effects not verified; do not replay.')
        log('complete',{'passed':True,'created_root_objects':17,'positive_native_txn':proof['candidate_txn_id']})
        print('Part 5 positive and negative acceptance verified:',proof['candidate_txn_id'],flush=True)


async def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--prefix');ap.add_argument('--output',type=Path,default=Path('tmp/lot-scrap-new'));ap.add_argument('--apply',type=Path);args=ap.parse_args()
    try:
        if args.apply:await execute(args.apply)
        elif args.prefix:await plan(args.prefix,args.output)
        else:ap.error('--prefix or --apply required')
    finally:await close_client()


if __name__=='__main__':asyncio.run(main())
