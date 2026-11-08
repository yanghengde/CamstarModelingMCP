"""Isolated Part 1 acceptance. Plan first; journal each single native submission.

Never edits source production master data, retries a mutation, or deletes evidence.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL, SAFE_CREATE_THRESHOLD
from core.http_client import request, close_client
from tools.modeling_details import raw_json, _has_next_link
from tools.process_context import ReadContext
from tools.production_reporting import query_rows
from tools.container_start import container_start, request_container_start_selection_values
from tools.container_inquiry import get_container_details

PREFIX = 'CDX-P1-20261108'
OUT = ROOT / 'docs' / 'production-acceptance'

def ref(suffix, rev=False):
    return {'name': PREFIX+'-'+suffix, **({'revision':'1'} if rev else {})}

def objects():
    def obj(entity, suffix, **fields):
        return {'entity':entity, 'body':dict(name=PREFIX+'-'+suffix,
            description='Isolated production reporting acceptance 2026-11-08', **fields)}
    return [obj('ContainerLevels','LEVEL',allowMove=True,isNameUnique=True),
        obj('LossReasonGroups','LOSS',entries=[{'name':'SCRAP'}]),
        obj('ContainerDefectReasons','DEFECT'),
        obj('ContDefectReasonGroups','DG',entries=[ref('DEFECT')]),
        *[obj('Operations',s,thruputReportingLevel=ref('LEVEL'),useQueue=True,
              lossReasons=ref('LOSS'),containerDefectReasons=ref('DG')) for s in ('OP1','OP2')],
        *[obj('Specs',s,revision='1',status=1,operation=ref(op)) for s,op in [('S1','OP1'),('S2','OP2')]],
        obj('Workflows','WF',revision='1',status=1,steps=[
            {'@odata.type':'#modeling.SpecStepChanges','name':'First','stepType':1,'sequence':1,'spec':ref('S1',True)},
            {'@odata.type':'#modeling.SpecStepChanges','name':'Final','stepType':1,'sequence':2,'spec':ref('S2',True),'isLastStep':True}]),
        obj('Products','P',revision='1',status=1,productType={'name':'BOX'},
            workflow=ref('WF',True),stdStartUOM={'name':'EACH'}),
        obj('MfgOrders','REPORT',product=ref('P',True),qty=6,uom={'name':'EACH'}),
        obj('MfgOrders','COMPLETE',product=ref('P',True),qty=2,uom={'name':'EACH'})]

class Runner:
    def __init__(self):
        self.path=OUT/'journal.json'
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'prefix':PREFIX,'environment':CAMSTAR_BASE_URL.rstrip('/'), 'attempts':[], 'objects':{},
            'authorization':'Implementation and necessary isolated acceptance under user Part 1 request'}
        if self.state['environment']!=CAMSTAR_BASE_URL.rstrip('/'):
            raise ValueError('Journal environment changed')
    def save(self):
        OUT.mkdir(parents=True,exist_ok=True)
        self.path.write_text(json.dumps(self.state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    async def find(self,entity,name):
        d=await ReadContext(3).get(entity,params={'$filter':"Name eq '"+name+"'",'$top':2})
        rows=d.get('value')
        if not isinstance(rows,list) or len(rows)>1 or _has_next_link(d):raise ValueError('Ambiguous identity '+name)
        return await ReadContext(3).get(entity,rows[0]['InstanceID']) if rows else None
    async def plan(self):
        for entity,name in [('LossReasons','SCRAP'),('UOMs','EACH'),('ProductTypes','BOX')]:
            await ReadContext(3).resolve(entity,{'Name':name},revisioned=False)
        for o in objects():
            if await self.find(o['entity'],o['body']['name']):raise ValueError('Fixture name exists '+o['body']['name'])
        names=[PREFIX+'-C0'+str(i) for i in range(1,4)]
        for n in names:
            if await query_rows("SELECT TOP 2 ContainerId FROM Container WHERE ContainerName=N'"+n+"'"):
                raise ValueError('Container name exists '+n)
        start={'mfg_order_name':'MO-100076435','product_name':'WM-900A','product_revision':'A','qty':1,'level_name':'BOX'}
        choices={}
        for field in ('Owner','StartReason'):
            d=json.loads(await request_container_start_selection_values('Details.'+field,**start))
            t=d.get('Details',{}).get(field,{})
            rows=t.get('SelectionValuesEx')
            if not isinstance(rows,list) or len(rows)!=1 or str(t.get('RecordCount'))!='1' or _has_next_link(d):
                raise ValueError('Start '+field+' requires explicit selection')
            choices[field]=rows[0]['Name']
        plan={'prefix':PREFIX,'created_at':datetime.now(timezone.utc).isoformat(),
            'modeling_objects':objects(), 'containers':[
                {'name':names[0],'order':PREFIX+'-REPORT','qty':6},
                *[{'name':n,'order':PREFIX+'-COMPLETE','qty':1} for n in names[1:]]],
            'level':PREFIX+'-LEVEL','product':PREFIX+'-P','revision':'1','selections':choices,
            'object_count':15,'reserve_native_labor_objects':2,
            'scenarios':['partial throughput','defect without quantity loss','scrap quantity loss',
                         'queue MoveIn and MoveOut','second operation','native order production completion',
                         'manual labor and automatic elapsed time','immutable report sources and replay prevention'],
            'scope':'New prefixed objects only; no source production edits or automatic cleanup'}
        if plan['object_count']+plan['reserve_native_labor_objects']>SAFE_CREATE_THRESHOLD:
            raise ValueError('Fixture exceeds configured explicit-confirmation threshold')
        OUT.mkdir(parents=True,exist_ok=True)
        (OUT/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(raw_json({k:v for k,v in plan.items() if k!='modeling_objects'}))
    async def submit(self,key,path,body,*,base_url=CAMSTAR_BASE_URL,method='POST',params=None):
        if any(a['key']==key for a in self.state['attempts']):raise ValueError('Previously attempted; read journal '+key)
        a={'key':key,'path':path,'method':method,'body':body,'state':'in_flight'}
        self.state['attempts'].append(a);self.save()
        a['response']=await request(method,path,body=body,base_url=base_url,params=params,response_formatter=raw_json)
        a['state']='submitted_needs_readback';self.save()
        if a['response'].startswith('❌'):raise ValueError(a['response'])
        return a
    async def modeling(self):
        plan=json.loads((OUT/'plan.json').read_text(encoding='utf-8'))
        if plan['modeling_objects']!=objects():raise ValueError('Plan differs from current payloads')
        for o in objects():
            e,b=o['entity'],o['body'];key=e+'/'+b['name']
            old=await self.find(e,b['name'])
            if key in self.state['objects']:
                if not old or old['InstanceID']!=self.state['objects'][key]['InstanceID']:raise ValueError('Fixture identity changed')
                continue
            if old:raise ValueError('Unjournaled existing object '+key)
            a=await self.submit(key,'/api/'+e,b)
            row=await self.find(e,b['name'])
            if not row:raise ValueError('Created object not read back '+key)
            a['state']='verified_created';self.state['objects'][key]=row;self.save()
            print('CREATED',e,b['name'],flush=True)
        await self.graph()
    async def graph(self):
        if self.state.get('graph_verified'):return
        w=await self.find('Workflows',PREFIX+'-WF');wid=w['InstanceID']
        leaf=await ReadContext(3).get('Workflows',wid,{'$expand':'Steps'})
        steps={s['Name']:s['InstanceID'] for s in leaf['Steps']}
        if set(steps)!={'First','Final'}:raise ValueError('Unexpected steps')
        def sr(n):return {'name':n,'instanceId':steps[n],'parent':{'instanceId':wid}}
        body={'firstStep':sr('First'),'steps':[{'@odata.type':'#modeling.SpecStepChanges','name':'First',
            'objectToChange':sr('First'),'paths-Expanded':[{'listItemAction':'add','value':{
                '@odata.type':'#modeling.MovePathChanges','name':'Next','toStep':sr('Final')}}]},
            {'@odata.type':'#modeling.SpecStepChanges','name':'Final','objectToChange':sr('Final')}]}
        a=await self.submit('workflow_graph','/api/Workflows/'+wid,body,method='PUT')
        leaf=await ReadContext(3).get('Workflows',wid,{'$expand':'Steps($expand=Paths)'})
        first=next(s for s in leaf['Steps'] if s['Name']=='First')
        if len(first.get('Paths',[]))!=1 or first['Paths'][0]['Name']!='Next':raise ValueError('Graph not verified')
        a.update(state='verified',readback=leaf);self.state['graph_verified']=True;self.save()
    async def starts(self):
        if not self.state.get('graph_verified'):raise ValueError('Graph must be readback verified first')
        plan=json.loads((OUT/'plan.json').read_text(encoding='utf-8'))
        for c in plan['containers']:
            key='Start/'+c['name']
            if key in self.state['objects']:continue
            context=dict(mfg_order_name=c['order'],product_name=plan['product'],product_revision='1',
                qty=c['qty'],level_name=plan['level'])
            order=await ReadContext(3).resolve('MfgOrders',{'Name':c['order']},revisioned=False)
            if order['Product']['Name']!=plan['product'] or order['Product']['Revision']!='1':raise ValueError('Order product mismatch')
            for field in ('Owner','StartReason'):
                d=json.loads(await request_container_start_selection_values('Details.'+field,**context))
                rows=d.get('Details',{}).get(field,{}).get('SelectionValuesEx',[])
                if len(rows)!=1 or rows[0]['Name']!=plan['selections'][field]:raise ValueError('New Start context selection changed')
            body={'details':dict(mfgOrder={'name':c['order']},product=ref('P',True),qty=c['qty'],
                level=ref('LEVEL'),owner={'name':plan['selections']['Owner']},
                startReason={'name':plan['selections']['StartReason']},containerName=c['name'],autoNumber=False)}
            a=await self.submit(key,'/api/Start',body,base_url=CAMSTAR_SHOPFLOOR_BASE_URL)
            row=json.loads(await get_container_details(c['name'],view='status'))
            status=row.get('data',{}).get('ContainerStatusDetails',{})
            if row.get('complete') is not True or status.get('Container',{}).get('Name')!=c['name']:raise ValueError('Start result not verified')
            self.state['objects'][key]=status;a.update(state='verified_created',readback=status);self.save()
            print('STARTED',c['name'],status['Container'],flush=True)

    async def moves(self):
        from tools.container_moves import _movement_context
        name=PREFIX+'-C02'
        cid=self.state['objects']['Start/'+name]['Container']['InstanceId']
        for service in ('MoveIn','MoveOut'):
            key=service+'/C02/First'
            if any(a['key']==key for a in self.state['attempts']):
                a=next(a for a in self.state['attempts'] if a['key']==key)
                if a['state']=='verified':continue
                raise ValueError('Existing attempt needs reconciliation '+key)
            await _movement_context(name,cid,PREFIX+'-LEVEL',service)
            body={'container':{'name':name,'instanceId':cid},'comments':'MCP-P1-ACCEPT:'+key}
            if service=='MoveOut':body.update(moveAllQty=True,thruputAllQty=True,close=False)
            a=await self.submit(key,'/api/'+service,body,base_url=CAMSTAR_SHOPFLOOR_BASE_URL)
            a['readback']=json.loads(await get_container_details(name,cid,view='status'))
            status=a['readback'].get('data',{}).get('ContainerStatusDetails',{})
            if service=='MoveIn' and status.get('InProcess') is not True:raise ValueError('MoveIn not observed')
            if service=='MoveOut' and status.get('StepName')!='Final':raise ValueError('MoveOut final step not observed')
            a['state']='verified';self.save();print('VERIFIED',key,flush=True)

    async def phased(self):
        from tools import production_station as s,production_reporting as p,production_verification as v
        name=PREFIX+'-C03'
        cid=self.state['objects']['Start/'+name]['Container']['InstanceId']
        for step in ('First',):
            for action in ('enter','throughput','exit'):
                key='phased/'+step+'/'+action
                old=self.state.get('phased',{}).get(key)
                if old:
                    if old.get('state')=='history_committed':continue
                    if old['state']!='prepared' or old['prepared'].get('executable') is not False:
                        raise ValueError('Reconcile prior phased attempt '+key)
                    self.state.setdefault('preparation_history',[]).append({'key':key,**old})
                if action=='throughput':
                    prepared=json.loads(await p.prepare_container_throughput(name,1,container_instance_id=cid))
                    execute=p.execute_container_throughput
                else:
                    prepared=json.loads(await s.prepare_production_station_transition(name,action,
                        container_instance_id=cid,path_name='Next' if action=='exit' else None))
                    execute=s.execute_production_station_transition
                self.state.setdefault('phased',{})[key]=record={'prepared':prepared,'state':'prepared'};self.save()
                if prepared.get('state')!='ready':raise ValueError(raw_json(prepared))
                args={k:prepared[k] for k in ('plan_id','plan_digest')}
                record['submitted']=json.loads(await execute(**args));record['state']='attempted';self.save()
                if record['submitted'].get('submitted') is not True:raise ValueError(raw_json(record['submitted']))
                record['verification']=json.loads(await v.verify_production_transaction(**args));self.save()
                if record['verification'].get('transaction_verified') is not True:raise ValueError(raw_json(record['verification']))
                record['state']='history_committed';self.save();print('VERIFIED',key,flush=True)
        self.state['complete_order_after_c03']=await p.native_order_quantities(PREFIX+'-COMPLETE');self.save()

    async def complete(self):
        from tools import production_station as s,production_reporting as p,production_verification as v,production_records as r
        # C02 remains stopped after its failed MoveOut; do not switch its native
        # service in this request. A separately planned one-unit order tests the
        # explicit phased workflow through native whole-order completion.
        order_name=PREFIX+'-END2';name=PREFIX+'-C04'
        extension={'order':order_name,'container':name,'qty':1,'new_object_count':2,
            'total_including_reserved_labor':19,'reason':'Independent complete-order acceptance; failed C02 remains stopped'}
        (OUT/'completion-extension-plan.json').write_text(json.dumps(extension,indent=2),encoding='utf-8')
        if 19>SAFE_CREATE_THRESHOLD:raise ValueError('Extension exceeds threshold')
        key='MfgOrders/'+order_name
        if key not in self.state['objects']:
            if await self.find('MfgOrders',order_name):raise ValueError('Unjournaled order exists')
            a=await self.submit(key,'/api/MfgOrders',{'name':order_name,'product':ref('P',True),'qty':1,'uom':{'name':'EACH'}})
            row=await self.find('MfgOrders',order_name)
            if not row:raise ValueError('Order not verified')
            self.state['objects'][key]=row;a['state']='verified';self.save()
        key='Start/'+name
        if key not in self.state['objects']:
            if await query_rows("SELECT TOP 2 ContainerId FROM Container WHERE ContainerName=N'"+name+"'"):
                raise ValueError('Container name occupied')
            plan=json.loads((OUT/'plan.json').read_text(encoding='utf-8'))
            context=dict(mfg_order_name=order_name,product_name=PREFIX+'-P',product_revision='1',qty=1,level_name=PREFIX+'-LEVEL')
            for f in ('Owner','StartReason'):
                d=json.loads(await request_container_start_selection_values('Details.'+f,**context))
                rows=d.get('Details',{}).get(f,{}).get('SelectionValuesEx',[])
                if len(rows)!=1 or rows[0]['Name']!=plan['selections'][f]:raise ValueError('Start selection changed')
            body={'details':dict(mfgOrder={'name':order_name},product=ref('P',True),qty=1,level=ref('LEVEL'),
                owner={'name':plan['selections']['Owner']},startReason={'name':plan['selections']['StartReason']},
                containerName=name,autoNumber=False)}
            a=await self.submit(key,'/api/Start',body,base_url=CAMSTAR_SHOPFLOOR_BASE_URL)
            data=json.loads(await get_container_details(name,view='status'))
            if data.get('complete') is not True:raise ValueError('Start readback failed')
            self.state['objects'][key]=data['data']['ContainerStatusDetails'];a['state']='verified';self.save()
        cid=self.state['objects']['Start/'+name]['Container']['InstanceId']
        for action in ('enter','throughput','complete'):
            key='complete/'+action
            old=self.state.get('completion_steps',{}).get(key)
            if old:
                if old['state']=='history_committed':continue
                raise ValueError('Reconcile attempted completion step '+key)
            prepared=json.loads(await (p.prepare_container_throughput(name,1,container_instance_id=cid) if action=='throughput'
                else s.prepare_production_station_transition(name,action,container_instance_id=cid,
                    path_name='Next' if action=='complete' else None)))
            self.state.setdefault('completion_steps',{})[key]=record={'prepared':prepared,'state':'prepared'};self.save()
            if prepared.get('state')!='ready':raise ValueError(raw_json(prepared))
            args={k:prepared[k] for k in ('plan_id','plan_digest')}
            execute=p.execute_container_throughput if action=='throughput' else s.execute_production_station_transition
            record['submitted']=json.loads(await execute(**args));record['state']='attempted';self.save()
            if record['submitted'].get('submitted') is not True:raise ValueError(raw_json(record['submitted']))
            record['verification']=json.loads(await v.verify_production_transaction(**args));self.save()
            if record['verification'].get('transaction_verified') is not True:raise ValueError(raw_json(record['verification']))
            record['state']='history_committed';self.save();print('VERIFIED',key,flush=True)
        self.state['native_complete_summary']=json.loads(await p.get_mfgorder_production_summary(order_name))
        self.state['native_complete_readiness']=json.loads(await r.get_mfgorder_completion_readiness(order_name))
        self.save()
        if self.state['native_complete_readiness'].get('ready_to_record_local_completion') is not True:
            raise ValueError('Native order completion missing '+raw_json(self.state['native_complete_readiness']))
        self.state['completion_record']=json.loads(await r.record_mfgorder_completion(PREFIX+'-END2-CLOSE',order_name))
        t=self.state['completion_steps']['complete/throughput']['prepared']
        self.state['complete_yield']=json.loads(await r.record_production_report(PREFIX+'-END2-YIELD','1','0','none',t['plan_id'],t['plan_digest']))
        self.save();print('NATIVE ORDER COMPLETE',order_name,flush=True)

    async def labor(self):
        from config import CAMSTAR_USERNAME
        from tools import collection_modeling as cm, production_records as r,production_labor as l
        unit=PREFIX+'-MIN';definition=PREFIX+'-LABOR'
        plan={'new_objects':[unit,definition],'actual_minutes_unit':'minute','count_total_all_stages':19,
            'binding':PREFIX+'-S1/1 CollectData optional','points':l.POINT_TYPES}
        (OUT/'labor-plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
        if 19>SAFE_CREATE_THRESHOLD:raise ValueError('Labor extension exceeds threshold')
        if 'UOMs/'+unit not in self.state['objects']:
            if await self.find('UOMs',unit):raise ValueError('Unjournaled UOM exists')
            a=await self.submit('UOMs/'+unit,'/api/UOMs',{'name':unit,'description':'One minute of operator declared actual labor'})
            self.state['objects']['UOMs/'+unit]=await self.find('UOMs',unit);a['state']='verified';self.save()
        async def change(key,prepare):
            old=self.state.get('labor_config',{}).get(key)
            if old:
                if old.get('state')=='verified_applied':return
                raise ValueError('Inspect previous modeling attempt '+key)
            prepared=json.loads(await prepare())
            self.state.setdefault('labor_config',{})[key]=record={'prepared':prepared,'state':'prepared'};self.save()
            if not prepared.get('plan_id'):raise ValueError(raw_json(prepared))
            args={k:prepared[k] for k in ('plan_id','plan_digest')}
            record['submitted']=json.loads(await cm.execute_data_collection_change(**args));record['state']='attempted';self.save()
            record['verification']=json.loads(await cm.reconcile_data_collection_change(**args));self.save()
            if record['verification'].get('state')!='verified_applied':raise ValueError(raw_json(record['verification']))
            record['state']='verified_applied';self.save();print('VERIFIED',key,flush=True)
        points=[{'name':n,'data_type':t,'required':True,**({'unit':unit,'decimal_scale':6} if n=='ActualMinutes' else {})}
            for n,t in l.POINT_TYPES.items()]
        await change('labor_definition',lambda:cm.prepare_data_collection_definition('user','create',definition,'1',
            {'status':1,'description':'Production actual labor in explicit minutes; separate from elapsed time','points':points}))
        await change('labor_binding',lambda:cm.prepare_data_collection_binding(PREFIX+'-S1','1','user',definition,'1',required=False))
        name=PREFIX+'-C01';cid=self.state['objects']['Start/'+name]['Container']['InstanceId']
        if 'manual_labor' not in self.state:
            self.state['manual_labor']=json.loads(await r.record_production_labor(PREFIX+'-LABOR-1',PREFIX+'-REPORT',
                CAMSTAR_USERNAME,'2026-11-08T08:00:00+08:00','2026-11-08T08:01:00+08:00','1',
                container_name=name,container_instance_id=cid,comments='Isolated acceptance declared test labor'))
            self.save()
        record_id=self.state['manual_labor'].get('record_id')
        if not record_id:raise ValueError(raw_json(self.state['manual_labor']))
        if 'labor_prepared' not in self.state or (self.state['labor_prepared'].get('executable') is False
                and 'labor_submitted' not in self.state):
            if 'labor_prepared' in self.state:self.state.setdefault('labor_preparation_history',[]).append(self.state['labor_prepared'])
            self.state['labor_prepared']=json.loads(await l.prepare_production_labor_submission(record_id,definition,'1',unit));self.save()
        if self.state['labor_prepared'].get('state')!='ready':raise ValueError(raw_json(self.state['labor_prepared']))
        if 'labor_submitted' not in self.state:
            self.state['labor_submitted']={'state':'in_flight'};self.save()
            self.state['labor_submitted']=json.loads(await l.execute_production_labor_submission(record_id));self.save()
        self.state['labor_verification']=json.loads(await l.verify_production_labor_submission(record_id));self.save()
        if self.state['labor_verification'].get('mes_persisted') is not True:raise ValueError(raw_json(self.state['labor_verification']))
        print('VERIFIED native actual labor minutes',flush=True)

    async def reporting(self):
        from tools import production_station as s,production_reporting as p,production_verification as v,production_records as r
        from tools import quality_plans as q
        name=PREFIX+'-C01';cid=self.state['objects']['Start/'+name]['Container']['InstanceId']
        async def step(key,prepare,execute,*,quality=False):
            old=self.state.get('report_steps',{}).get(key)
            if old:
                if old.get('state')=='history_committed':return old['prepared']
                raise ValueError('Inspect previous report attempt '+key)
            prepared=json.loads(await prepare())
            self.state.setdefault('report_steps',{})[key]=record={'prepared':prepared,'state':'prepared'};self.save()
            if prepared.get('state')!='ready':raise ValueError(raw_json(prepared))
            args={k:prepared[k] for k in ('plan_id','plan_digest')}
            record['submitted']={'state':'in_flight'};self.save()
            record['submitted']=json.loads(await execute(**args));record['state']='attempted';self.save()
            if quality:
                with q.db_connect() as db:
                    snap=json.loads(db.execute('SELECT payload FROM plans WHERE id=?',(args['plan_id'],)).fetchone()[0])
                comment=snap['body']['comments'].replace("'","''")
                rows=await p.query_rows("SELECT TOP 3 TxnId FROM HistoryMainline WHERE ContainerId='"+cid+"' AND Comments=N'"+comment+"'")
                if len(rows)!=1:raise ValueError('Scrap unique transaction unavailable')
                record['verification']=json.loads(await q.review_quality_transaction(**args,action='resolve_history',
                    evidence_reference=rows[0]['TxnId'],review_note='Isolated Part 1 receipt, native loss details and actual quantity verified',conclusion='committed'))
            else:
                if record['submitted'].get('submitted') is not True:raise ValueError(raw_json(record['submitted']))
                record['verification']=json.loads(await v.verify_production_transaction(**args))
            self.save()
            if record['verification'].get('state')!='history_committed':raise ValueError(raw_json(record['verification']))
            record['state']='history_committed';self.save();print('VERIFIED',key,flush=True)
            return prepared
        await step('enter_C01',lambda:s.prepare_production_station_transition(name,'enter',container_instance_id=cid),s.execute_production_station_transition)
        t1=await step('partial_3_record',lambda:p.prepare_container_throughput(name,3,container_instance_id=cid),p.execute_container_throughput)
        d1=await step('defect_1',lambda:r.prepare_production_reject(name,'1','record',PREFIX+'-DEFECT',
            container_instance_id=cid,inspected_qty='3',defect_count=2),r.execute_production_reject)
        self.state['report_record_mode']=json.loads(await r.record_production_report(PREFIX+'-RECORD','2','1','record',
            t1['plan_id'],t1['plan_digest'],d1['plan_id'],d1['plan_digest']));self.save()
        if self.state['report_record_mode'].get('saved') is not True:raise ValueError(raw_json(self.state['report_record_mode']))
        t2=await step('partial_3_scrap',lambda:p.prepare_container_throughput(name,3,container_instance_id=cid),p.execute_container_throughput)
        d2=await step('scrap_1',lambda:r.prepare_production_reject(name,'1','scrap','SCRAP',
            container_instance_id=cid,close_when_zero=False,adjust_throughput=True),q.execute_quality_transaction,quality=True)
        self.state['report_scrap_mode']=json.loads(await r.record_production_report(PREFIX+'-SCRAP','2','1','scrap',
            t2['plan_id'],t2['plan_digest'],d2['plan_id'],d2['plan_digest']));self.save()
        if self.state['report_scrap_mode'].get('saved') is not True:raise ValueError(raw_json(self.state['report_scrap_mode']))
        self.state['report_totals']=json.loads(await r.get_production_reports(PREFIX+'-REPORT'))
        self.state['report_worktime']=json.loads(await r.get_production_worktime(PREFIX+'-REPORT'))
        self.state['native_elapsed_completed_order']=json.loads(await r.get_production_worktime(PREFIX+'-END2'))
        self.state['report_final_state']=json.loads(await get_container_details(name,cid,view='status'));self.save()
        print('VERIFIED both rejection modes and declared yield',flush=True)

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['plan','modeling','starts','moves','phased','complete','labor','reporting'])
    args=parser.parse_args()
    try:await getattr(Runner(),args.stage)()
    finally:await close_client()

if __name__=='__main__':asyncio.run(main())
