"""Part 6 explicit Event follow-up on the isolated, audited reason-loss case.

--apply validates the prepared event-manifest.json. --authorized-root-count 22
records the user's explicit approval for the concrete 22-root combined scope.
Every native request is journalled before submission and never retried.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import inspection_combinations_acceptance as a
from tools import quality_inspection_event as event,quality_history as history
from tools.process_context import ReadContext
from core.http_client import close_client


def event_models(prefix):
    if len(prefix)>20:raise ValueError('Event fixture prefix must be <=20 characters so automatic Event names remain <=30.')
    return [
        {'entity':'FailureModeGroups','body':{'name':prefix+'-FAILGROUP','description':'Part 6 isolated event association, empty failure group','entries':[]}},
        {'entity':'PriorityLevels','body':{'name':prefix+'-PRIORITY','description':'Part 6 isolated event priority'}},
        {'entity':'NumberingRules','body':{'name':prefix+'-EVENTRULE','description':'Part 6 isolated event numbering','numberingRuleType':1,'sequenceLength':4,
            'lastAssignedSequence':0,'lastAssignedSequenceHex':'0','maximumValue':9999,'prefix':'"'+prefix+'-E-"','isRollover':False}},
        {'entity':'Organizations','body':{'name':prefix+'-ORG','description':'Part 6 isolated event organization; no employee settings changed',
            'eventClassificationSpecMaps':[{'classification':{'name':'Product'},'subclassification':{'name':'FieldEvent'},'failureModeGroup':{'name':prefix+'-FAILGROUP'},
                'owner':{'name':'CamstarAdmin'},'role':{'name':'Default Quality'},'peDefault':True}],
            'numberingRuleMap':[{'category':3,'numberingRule':{'name':prefix+'-EVENTRULE'}}]}}
    ]


async def plan(original):
    m=a.load(original);prefix=m['prefix'];path=original.with_name('event-manifest.json')
    if path.exists():raise ValueError('Event manifest exists; retain original evidence.')
    for item in event_models(prefix):
        if await a.lookup(item['entity'],item['body']['name']):raise ValueError('Existing event fixture; no adoption.')
    payload=dict(prefix=prefix,environment=a.p.environment(),already_created_roots=17,configuration_roots=event_models(prefix),
        reserved_native_event_roots=1,max_root_objects=22,employee_default_changes=False,production_configuration_changes=False,outbound_notifications=False)
    with path.open('x',encoding='utf8')as f:json.dump({'payload':payload,'digest':a.p.digest(payload)},f,ensure_ascii=False,indent=2)
    print('Read-only Event fixture scope:',path,'combined maximum roots22; requires explicit authorization.',flush=True)


async def apply(path,root_budget):
    envelope=json.loads(path.read_text(encoding='utf8'));m=envelope['payload'];original=path.with_name('manifest.json');prefix=m['prefix']
    if (envelope['digest']!=a.p.digest(m)or m['environment']!=a.p.environment()or root_budget!=22 or m['max_root_objects']!=22
        or m['already_created_roots']!=17 or m['configuration_roots']!=event_models(prefix)or m['reserved_native_event_roots']!=1):raise ValueError('Explicit canonical reviewed scope22 required; old attempted manifests remain immutable.')
    if a.load(original)['prefix']!=prefix or not any(v['stage']=='fixtures_complete'for v in a.saved(original)):raise ValueError('Original isolated fixtures incomplete.')
    if a.saved(path):raise ValueError('Event configuration already attempted; reconcile original journal without replay.')
    # Resolve existing identities, never grant roles or change employee defaults.
    for entity,name in [('Classifications','Product'),('SubClassifications','FieldEvent'),('Employees','CamstarAdmin'),('Roles','Default Quality')]:
        ref=await ReadContext(2).resolve(entity,{'Name':name},revisioned=False)
        if ref['IsFrozen']:raise ValueError('Reference frozen.')
    expected=['FailureModeGroups','PriorityLevels','NumberingRules','Organizations']
    for item,entity in zip(m['configuration_roots'],expected):
        if item['entity']!=entity or not item['body']['name'].startswith(prefix+'-'):raise ValueError('Unexpected configuration scope.')
        if await a.lookup(entity,item['body']['name']):raise ValueError('Existing configuration; no adoption.')
        doc=json.loads(Path('tmp/inspection-combinations-contracts/'+entity+'.json').read_text(encoding='utf8'))
        import jsonschema
        target=doc['paths']['/api/'+entity]['post']['requestBody']['content']['application/json']['schema']['$ref']
        jsonschema.Draft7Validator({'components':doc['components'],'$ref':target}).validate(item['body'])
    a.log(path,'authorized_scope',{'authorized_root_count':root_budget,'manifest_digest':envelope['digest'],'account_changes':False})
    for item in m['configuration_roots']:
        entity,body=item['entity'],item['body'];await a.send(path,'create-'+entity,'/api/'+entity,body,True)
        current=await ReadContext(2).resolve(entity,{'Name':body['name']},revisioned=False)
        a.log(path,'configuration_verified',{'entity':entity,'object':current});print('Created',entity,current['Name'],flush=True)
    a.log(path,'event_configuration_complete',{'configuration_roots_created':4,'combined_roots_created':21,'reserved_event_roots':1})


async def prepare(path):
    original=path.with_name('manifest.json');m=a.load(original);prefix=m['prefix'];journal=a.saved(path)
    if not any(v['stage']=='event_configuration_complete'for v in journal):raise ValueError('Event configuration incomplete.')
    prior=[v['data']for v in journal if v['stage']=='event_plan'and v['data'].get('plan_id')]
    for plan in prior:
        with a.p.db_connect()as db:
            state=db.execute('SELECT state,outcome FROM plans WHERE id=?',(plan['plan_id'],)).fetchone()
            attempts=db.execute('SELECT plan_id FROM attempts WHERE plan_id=?',(plan['plan_id'],)).fetchall()
        rejected=state and state[0]in ('native_configuration_rejected','native_authorization_rejected')and json.loads(state[1]).get('known_native_rejection')is True
        if not state or not rejected and (state[0]!='ready'or attempts):raise ValueError('An unresolved Event attempt exists; reconcile, never recreate.')
    if prior:a.log(path,'superseded_plans',{'plan_ids':[v['plan_id']for v in prior],'policy':'Only never-submitted ready plans or exact evidence-resolved native configuration rejections; never replay an old plan.'})
    source=next(v['data']for v in a.saved(original)if v['stage']=='reason_groups_plan'and v['data'].get('plan_id'))
    args=dict(inspection_plan_id=source['plan_id'],inspection_plan_digest=source['plan_digest'],organization_name=prefix+'-ORG',
        classification_name='Product',subclassification_name='FieldEvent',priority_level_name=prefix+'-PRIORITY',brief_description='Part 6 counted inspection rejects',
        reporter_name='CamstarAdmin',initiator_name='CamstarAdmin',reported_date=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        description='Isolated Part 6: one verified failed inspection, two reject reasons, one separate linked quality event.')
    ready=json.loads(await event.prepare_inspection_quality_event(**args));a.log(path,'event_plan',ready)
    print('Event preparation',json.dumps({k:ready[k]for k in ('plan_id','plan_digest','state','error')if k in ready}),flush=True)
    if 'plan_id'not in ready:raise ValueError(ready.get('error','No event plan'))


async def execute(path):
    journal=a.saved(path)
    ready=[v['data']for v in journal if v['stage']=='event_plan'and v['data'].get('plan_id')][-1];args={k:ready[k]for k in ('plan_id','plan_digest')}
    if any(v['stage']=='event_execute_attempt'and v['data']['plan_id']==ready['plan_id']for v in journal):raise ValueError('Event plan already attempted; reconcile, never replay.')
    a.log(path,'event_execute_attempt',args)
    reply=json.loads(await a.p.execute_quality_transaction(**args));a.log(path,'event_execute',reply)
    print('Event submitted once',json.dumps({k:reply[k]for k in ('state','http_status','error')if k in reply}),flush=True)
    if not reply.get('submission_attempted'):raise ValueError(reply)


async def reconcile(path):
    ready=[v['data']for v in a.saved(path)if v['stage']=='event_plan'and v['data'].get('plan_id')][-1];args={k:ready[k]for k in ('plan_id','plan_digest')}
    proof=json.loads(await history.reconcile_quality_execution(**args));a.log(path,'event_history',proof)
    print('Event history',proof.get('checks'),proof.get('error'),flush=True)
    if not proof.get('recorded_business_effects_verified'):raise ValueError('Original Event not reconciled; no retry or fabricated review.')
    done=json.loads(await a.p.review_quality_transaction(**args,action='resolve_history',evidence_reference=proof['candidate_txn_id'],conclusion='committed',
        review_note='Separate native CreateProductionEvent exact original inspection source, EventData, EventLot and container cross reference verified; original inspection preserved.'))
    a.log(path,'event_resolved',done);print('Event final',done.get('state'),done.get('error'),flush=True)
    if not done.get('verified_success'):raise ValueError('Original Event final evidence incomplete; remains occupied.')


async def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('action',choices=['plan','apply','prepare','execute','reconcile']);ap.add_argument('manifest',type=Path)
    ap.add_argument('--authorized-root-count',type=int);args=ap.parse_args()
    try:
        if args.action=='apply':await apply(args.manifest,args.authorized_root_count)
        else:await globals()[args.action](args.manifest)
    finally:await close_client()


if __name__=='__main__':asyncio.run(main())
