"""T00-T09 bounded acceptance probes. No Shopfloor POST is permitted.

Run with the project venv. Writes only local evidence. It does not create MES
fixtures or infer stage acceptance from an empty/incomplete response.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from config import CAMSTAR_BASE_URL, CAMSTAR_QUERY_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import request, close_client
from tools import mcp
from core.tool_catalog import TOOL_POLICIES
from tools.modeling_details import raw_json
from tools.process_context import ReadContext
from tools.resources import list_resources, get_spec_resource_context
from tools.boms import get_product_material_requirements
from tools.data_collection import get_data_collection_details, get_container_collection_requirements
from tools.production_readiness import check_mfgorder_readiness
from tools.component_issue import get_component_requirement, get_component_issue_history
from tools.container_inquiry import get_container_details
from tools.collection_history import get_container_collection_history
from tools.container_transfers import preview_container_split
from tools.mfgorder_tasks import get_mfgorder_tasks
from tools.mfgorder_task_history import get_mfgorder_task_history
from tools.spec_documents import get_spec_documents
from scripts.audit_surrounding_contracts import GROUPS, sanitize


class ReadOnlyGate:
    def __init__(self, modeling, query, shopfloor):
        self.modeling, self.query, self.shopfloor = [httpx.URL(v.rstrip('/')) for v in (modeling, query, shopfloor)]
        self.requests = []

    def same_origin(self, url, base):
        return (url.scheme, url.host, url.port) == (base.scheme, base.host, base.port)

    def validate(self, req):
        url = req.url
        if req.method == 'GET':
            if self.same_origin(url, self.modeling) and url.path.startswith(self.modeling.path + '/api/'):
                return
            for base in (self.modeling, self.shopfloor):
                if self.same_origin(url, base):
                    suffix = url.path.removeprefix(base.path + '/swagger/')
                    if url.path.startswith(base.path + '/swagger/') and re.fullmatch(r'[A-Za-z]+/swagger\.json', suffix):
                        return
        if req.method == 'POST' and self.same_origin(url, self.query):
            service = url.path.removeprefix(self.query.path + '/api/')
            if url.path.startswith(self.query.path + '/api/') and service in {
                    'ViewContainerStatus','ContainerInfoInquiry','ContainerHistoryInquiry','ComponentIssueInquiry','MfgOrderInquiry','AdHoc'}:
                body = json.loads(req.content)
                if service != 'AdHoc':
                    return
                sql = body.get('queryText', '')
                stripped = re.sub(r"'(?:''|[^'])*'", "''", sql)
                if (re.match(r'^\s*SELECT\b', sql, re.I) and ';' not in stripped
                        and not any(x in stripped for x in ('--','/*','*/'))
                        and not re.search(r'\b(INSERT|UPDATE|DELETE|MERGE|EXEC|EXECUTE|ALTER|DROP|TRUNCATE|CREATE|INTO|OPENROWSET|OPENQUERY)\b', stripped, re.I)
                        and "'" not in stripped.replace("''", '')):
                    return
        raise RuntimeError('Readonly acceptance blocked this request method/path/body.')

    def install(self):
        original = httpx.AsyncHTTPTransport.handle_async_request
        async def checked(transport, req):
            entry = {'method':req.method,'path':req.url.path}
            self.requests.append(entry)
            try:
                self.validate(req)
            except Exception:
                entry['blocked'] = True
                raise
            response = await original(transport, req)
            entry['http_status'] = response.status_code
            return response
        httpx.AsyncHTTPTransport.handle_async_request = checked
        return original


async def audit(output, container):
    output.mkdir(parents=True, exist_ok=True)
    gate = ReadOnlyGate(CAMSTAR_BASE_URL, CAMSTAR_QUERY_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL)
    original = gate.install()
    report = {'checked_at_utc':datetime.now(timezone.utc).isoformat(),'scope':'bounded readonly acceptance, not transaction acceptance',
              'business_write_performed':False,'contracts':[],'cases':[],'samples':{},'requests':gate.requests}

    async def case(stage, name, fn, **args):
        entry = {'stage':stage,'name':name,'arguments':args}
        report['cases'].append(entry)
        try:
            result = await asyncio.wait_for(fn(**args), timeout=65)
            data = json.loads(result) if isinstance(result,str) else result
            entry['result'] = data
            entry['observation'] = ('returned_error' if isinstance(data,dict) and data.get('error') else
                                    'returned_complete' if isinstance(data,dict) and data.get('complete') is True else 'returned_incomplete_or_unclassified')
            return data
        except Exception as error:
            entry.update(observation='exception',error=type(error).__name__+': '+str(error))
            return {}

    try:
        # Local catalog inspection cannot execute the listed business tools.
        registered = {tool.name for tool in await mcp.list_tools()}
        report['catalog'] = {'registered_count':len(registered), 'matches_policies':registered == set(TOOL_POLICIES),
                             'material_write_tools':[n for n,p in TOOL_POLICIES.items() if p.group=='material' and p.effect!='read'],
                             'always_confirm_writes':[n for n,p in TOOL_POLICIES.items() if p.always_confirm]}
        semaphore = asyncio.Semaphore(3)
        async def contract(service, name):
            base = CAMSTAR_BASE_URL if service=='Modeling' else CAMSTAR_SHOPFLOOR_BASE_URL
            entry = {'service':service,'name':name}
            async with semaphore:
                try:
                    raw = await request('GET',f'/swagger/{name}/swagger.json',base_url=base,response_formatter=raw_json)
                    data = json.loads(raw)
                    if not isinstance(data,dict) or not data.get('paths'):
                        raise ValueError('No OpenAPI paths returned')
                    actual = sanitize(data)
                    path = ROOT/'Swagger'/'extensions'/f'{service}-{name}.json'
                    expected = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else None
                    canonical = lambda value: json.dumps(value,sort_keys=True,ensure_ascii=False).encode()
                    entry.update(status='matches_archive' if expected==actual else 'drift_or_no_archive',
                                 live_sha256=hashlib.sha256(canonical(actual)).hexdigest(),
                                 archive_sha256=hashlib.sha256(canonical(expected)).hexdigest() if expected else None)
                except Exception as error:
                    entry.update(status='failed',error=str(error))
            return entry
        report['contracts'] = await asyncio.gather(*(contract(service,name) for service,names in GROUPS.items() for name in names))
        context = ReadContext(max_requests=6)
        async def sample(entity):
            result = await case('T00',entity+'_sample',context.get,entity=entity,params={'$top':1})
            values = result.get('value') if isinstance(result,dict) else None
            selected = values[0] if isinstance(values,list) and values else None
            # Persist only identity/reference data needed by the probes.
            report['samples'][entity] = {k:selected.get(k) for k in ('Name','Revision','InstanceID','Product')} if isinstance(selected,dict) else None
            return selected
        spec, order = await sample('Specs'), await sample('MfgOrders')
        user_definition, product = await sample('UserDataCollectionDefs'), await sample('Products')
        await case('T01','resource_list',list_resources,limit=2)
        if spec:
            await case('T01','spec_resource_context',get_spec_resource_context,instance_id=spec['InstanceID'],max_groups=5,max_resources=10,max_depth=3)
            await case('T09','spec_documents',get_spec_documents,spec_id=spec['InstanceID'])
        if product:
            await case('T02','product_materials',get_product_material_requirements,instance_id=product['InstanceID'],
                       mfgorder_name=order['Name'] if order else '',limit=10)
        if user_definition:
            await case('T03','user_definition',get_data_collection_details,instance_id=user_definition['InstanceID'],kind='user',limit=10)
        else:
            report['cases'].append({'stage':'T03','name':'user_definition','observation':'blocked_no_visible_sample'})
        if order:
            await case('T04','order_readiness',check_mfgorder_readiness,mfgorder_name=order['Name'],max_specs=2,max_depth=1,max_definitions=2,point_limit=10)
            await case('T08','task_inventory',get_mfgorder_tasks,mfgorder_name=order['Name'],limit=10)
            await case('T08','executed_records',get_mfgorder_task_history,mfgorder_name=order['Name'])
        status = await case('T00','container_identity',get_container_details,container_name=container,view='status')
        await case('T03','container_collection_requirements',get_container_collection_requirements,container_name=container,max_definitions=2)
        product_ref = status.get('data',{}).get('ContainerStatusDetails',{}).get('Product') if isinstance(status,dict) else None
        if isinstance(product_ref,dict) and product_ref.get('Name'):
            await case('T05','component_requirement',get_component_requirement,container_name=container,object_name=product_ref['Name'])
        await case('T05','component_history',get_component_issue_history,container_name=container,limit=5)
        await case('T06','collection_history',get_container_collection_history,container_name=container,limit=5)
        await case('T07','split_intent_only',preview_container_split,container_name=container,new_container_name='ACCEPTANCE-INTENT-ONLY',qty=0.25)
    finally:
        await close_client()
        httpx.AsyncHTTPTransport.handle_async_request = original
        report['blocked_requests'] = sum(bool(r.get('blocked')) for r in gate.requests)
        report['shopfloor_posts'] = sum(r['method']=='POST' and r['path'].startswith(gate.shopfloor.path+'/') for r in gate.requests)
        (output/'readonly.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'contracts':len(report['contracts']),'cases':len(report['cases']),'requests':len(gate.requests),
                      'blocked':report['blocked_requests'],'shopfloor_posts':report['shopfloor_posts']},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'docs'/'acceptance-2026-10-24')
    parser.add_argument('--container',default='SN0000001')
    args=parser.parse_args()
    asyncio.run(audit(args.output,args.container))
