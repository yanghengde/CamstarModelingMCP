"""Part 3 isolated resource acceptance; plan first, single attempts, preserve evidence.

python scripts/resource_acceptance.py --prefix CDX-R3-YYYYMMDD-UNIQUE --output tmp/r3-new
python scripts/resource_acceptance.py --apply tmp/r3-new/manifest.json
"""
import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools import resource_operations as r,resource_execution as p,collection_modeling as cm
from core.http_client import get_client,get_headers,close_client
from core.transfer_attempt_lock import attempt_lock
from config import CAMSTAR_BASE_URL,SAFE_CREATE_THRESHOLD


def fixture_objects(prefix):
    def obj(entity,suffix,**body): return {'entity':entity,'body':{'name':prefix+'-'+suffix,**body}}
    return [obj('Setups','SETUP',revision='1',status=1),
        obj('Products','PRODUCT',revision='1',status=1,productType={'name':'COMPONENT'}),
        obj('MaintenanceReasons','REASON'),
        obj('DateReqs','REQ',revision='1',status=1,maintenanceReason={'name':prefix+'-REASON'},
            scheduleDate=(datetime.now(timezone.utc)+timedelta(days=365)).isoformat()),
        obj('ResourceStatusCodes','DOWN',availability=2,resourceState=3),
        obj('ResourceStatusCodes','UP',availability=1,resourceState=5),
        obj('ResStatusCodeGroups','INITIAL',entries=[{'name':prefix+'-DOWN'}]),
        obj('ResourceStatusModels','MODEL',initStatusCodes={'name':prefix+'-INITIAL'},details=[
            {'status':{'name':prefix+'-DOWN'},'toStatus':{'name':prefix+'-UP'}},
            {'status':{'name':prefix+'-UP'},'toStatus':{'name':prefix+'-DOWN'}}])]


async def make_manifest(prefix,output):
    if not re.fullmatch(r'CDX-R3-[A-Z0-9-]{1,50}',prefix): raise ValueError('Use a unique CDX-R3- prefix, uppercase letters/numbers/hyphens.')
    if SAFE_CREATE_THRESHOLD<10: raise ValueError('Ten new top-level objects exceed the configured create threshold.')
    objects=fixture_objects(prefix)
    for obj in objects:
        if await r.lookup(obj['entity'],obj['body']['name']): raise ValueError('Fixture already exists: '+obj['body']['name'])
    for entity,suffix in [('Resources','DEVICE'),('UserDataCollectionDefs','USER')]:
        if await r.lookup(entity,prefix+'-'+suffix): raise ValueError('Fixture name exists: '+suffix)
    ctx=r.ReadContext(6)
    for entity,n in [('UOMs','EACH'),('UOMs','UNIT'),('ProductTypes','COMPONENT')]:
        await ctx.resolve(entity,{'Name':n},revisioned=False)
    payload={'prefix':prefix,'objects':objects,'object_count':10,'created':time.time(),'environments':p.environments(),
        'definition':{'description':'Part 3 explicit resource values','points':[
            {'name':'Temperature','data_type':'decimal','required':True,'lower_limit':'0','upper_limit':'100','unit':'EACH'},
            {'name':'Ready','data_type':'boolean','required':True},{'name':'Note','data_type':'string','required':True}]},
        'scope':'master create/update; native maintenance activation/deactivation; model-based Down/Up/Down; dual-unit throughput; user resource collection; persistent history; grouped execution blocked'}
    output.mkdir(parents=True,exist_ok=True);path=output/'manifest.json'
    with path.open('x',encoding='utf-8') as f:json.dump({'digest':r.plans.digest(payload),'payload':payload},f,ensure_ascii=False,indent=2)
    print('Read-only manifest saved:',path)


async def execute_manifest(path):
    manifest=json.loads(path.read_text(encoding='utf-8'));m=manifest['payload'];prefix=m['prefix']
    if r.plans.digest(m)!=manifest['digest'] or m['environments']!=p.environments(): raise ValueError('Manifest content/environment changed.')
    if time.time()-m['created']>3600: raise ValueError('Manifest expired; prepare a new unique run.')
    if not re.fullmatch(r'CDX-R3-[A-Z0-9-]{1,50}',prefix) or m['object_count']!=10 or SAFE_CREATE_THRESHOLD<10: raise ValueError('Invalid fixture scope/threshold.')
    allowed={'Setups','Products','MaintenanceReasons','DateReqs','ResourceStatusCodes','ResStatusCodeGroups','ResourceStatusModels'}
    for obj in m['objects']:
        if obj['entity'] not in allowed or not obj['body']['name'].startswith(prefix+'-'): raise ValueError('Fixture scope mismatch.')
        if await r.lookup(obj['entity'],obj['body']['name']): raise ValueError('Existing fixture; never adopt or replay.')
    for entity,suffix in [('Resources','DEVICE'),('UserDataCollectionDefs','USER')]:
        if await r.lookup(entity,prefix+'-'+suffix): raise ValueError('Fixture exists; never replay.')
    with attempt_lock(p.PLAN_DB,'acceptance:'+prefix),path.with_suffix('.journal.jsonl').open('x',encoding='utf-8') as journal:
        def record(stage,data):
            journal.write(json.dumps({'at':time.time(),'stage':stage,'data':data},ensure_ascii=False)+'\n');journal.flush()
        async def apply(label,preview):
            plan=json.loads(await preview);record(label+'_plan',plan)
            if not plan.get('plan_id'): raise ValueError(plan)
            args={k:plan[k] for k in ('plan_id','plan_digest')}
            result=json.loads(await p.execute_resource_change(**args));record(label+'_execute',result)
            checked=json.loads(await p.reconcile_resource_change(**args));record(label+'_verify',checked)
            if not checked.get('verified_success'): raise ValueError('Stopped without retry: '+json.dumps(checked))
            print(label,'verified',flush=True)
        for obj in m['objects']:
            record('fixture_attempt',obj)
            response=await get_client().post(CAMSTAR_BASE_URL.rstrip('/')+'/api/'+obj['entity'],headers=get_headers(),json=obj['body'],follow_redirects=False)
            record('fixture_response',{'status':response.status_code,'response':response.text})
            if not 200<=response.status_code<300: raise ValueError('Fixture requires inspection; no retry.')
            body=obj['body'];ref={'Name':body['name'],**({'Revision':body['revision']} if 'revision'in body else {})}
            actual=await r.ReadContext(2).resolve(obj['entity'],ref,revisioned='revision'in body);record('fixture_readback',actual)
        definition=json.loads(await cm.prepare_data_collection_definition('user','create',prefix+'-USER','1',m['definition']));record('definition_plan',definition)
        if not definition.get('plan_id'): raise ValueError(definition)
        args={k:definition[k] for k in ('plan_id','plan_digest')}
        record('definition_execute',json.loads(await cm.execute_data_collection_change(**args)))
        checked=json.loads(await cm.reconcile_data_collection_change(**args));record('definition_verify',checked)
        if not checked.get('verified_success'): raise ValueError('Definition not verified; stop, never replay.')
        device=prefix+'-DEVICE'
        await apply('create',r.prepare_resource_change('create',device,{'description':'Isolated Part 3 resource','status_model':prefix+'-MODEL','vendor_model':'R3-V1','vendor_serial_number':'R3-ACCEPTANCE'}))
        await apply('update',r.prepare_resource_change('update',device,{'description':'Verified resource maintenance','notes':'Part 3 acceptance','vendor_model':'R3-V2'}))
        await apply('initial_down',r.prepare_resource_setup(device,2,status_code=prefix+'-DOWN',transition=True))
        await apply('transition_up',r.prepare_resource_setup(device,1,status_code=prefix+'-UP',transition=True,
            setup_name=prefix+'-SETUP',setup_revision='1',product_name=prefix+'-PRODUCT',product_revision='1',run='R3-ACCEPTANCE'))
        await apply('activate',r.prepare_resource_activation(device,prefix+'-REQ','1',True))
        await apply('deactivate',r.prepare_resource_activation(device,prefix+'-REQ','1',False))
        await apply('throughput',r.prepare_resource_throughput(device,7,'EACH',quantity2=3,unit2_name='UNIT'))
        await apply('collection',r.prepare_resource_collection(device,prefix+'-USER','1',{'Temperature':25,'Ready':False,'Note':'Acceptance complete'}))
        await apply('transition_down',r.prepare_resource_setup(device,2,status_code=prefix+'-DOWN',transition=True))
        state=json.loads(await r.get_resource_operational_state(device));record('final_state',state)
        throughput=json.loads(await r.get_resource_throughput_history(device));record('throughput_history',throughput)
        collection=json.loads(await r.get_resource_collection_history(device));record('collection_history',collection)
        if not state.get('complete') or state['state']['Availability']!='2' or state['maintenance'] or state['state']['TotalThruputQty']!='7' or state['state']['TotalThruputQty2']!='3': raise ValueError('Final state mismatch.')
        if not throughput.get('complete') or len(throughput['rows'])!=1 or not collection.get('complete') or len(collection['rows'])!=3: raise ValueError('Persistent history incomplete.')
        blocked=json.loads(await r.prepare_resource_throughput(device,1,'EACH'));record('down_throughput_blocked',blocked)
        if 'error' not in blocked: raise ValueError('Down device was not blocked.')
        record('complete',{'passed':True,'resource':device});print('Part 3 resource acceptance passed.')


async def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--prefix');parser.add_argument('--output',type=Path,default=Path('tmp/resource-acceptance-new'));parser.add_argument('--apply',type=Path);args=parser.parse_args()
    try:
        if args.apply: await execute_manifest(args.apply)
        elif args.prefix: await make_manifest(args.prefix,args.output)
        else: parser.error('--prefix to prepare or --apply to execute is required')
    finally: await close_client()


if __name__=='__main__':asyncio.run(main())
