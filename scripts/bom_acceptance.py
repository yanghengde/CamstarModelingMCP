"""Isolated Part 2 acceptance. Plan first, journal before writes, never replay.

python scripts/bom_acceptance.py --prefix CDX-BOM-YYYYMMDD-UNIQUE --output tmp/bom-run
python scripts/bom_acceptance.py --apply tmp/bom-run/manifest.json
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import CAMSTAR_BASE_URL, SAFE_CREATE_THRESHOLD
from core.http_client import get_client, get_headers, close_client
from tools import bom_modeling as bm, boms


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def make_manifest(prefix, output):
    if not re.fullmatch(r'CDX-BOM-[A-Z0-9-]{1,60}', prefix): raise ValueError('Use a unique CDX-BOM- prefix (uppercase letters, numbers and hyphens).')
    if SAFE_CREATE_THRESHOLD < 6: raise ValueError('Acceptance requires six new objects, exceeding the configured create threshold.')
    ctx = bm.ReadContext(max_requests=4)
    unit = await ctx.resolve('UOMs', {'Name': 'EACH'}, revisioned=False)
    product_type = await ctx.resolve('ProductTypes', {'Name': 'COMPONENT'}, revisioned=False)
    for entity, suffix in [('Products','A'),('Products','B'),('Products','ASSEMBLY'),('BOMs','BOM')]:
        if await bm.lookup(entity, prefix + '-' + suffix): raise ValueError('Fixture name already exists: ' + suffix)
    payload = {'prefix': prefix, 'environment': CAMSTAR_BASE_URL.rstrip('/'), 'created': time.time(),
        'object_count': 6, 'bom_revisions': ['1','2','3'], 'unit': {'name': unit['Name'], 'instanceId': unit['InstanceID']},
        'products': [{'name': prefix + '-' + s, 'revision': '1', 'status': 1,
            'productType': {'name': product_type['Name'], 'instanceId': product_type['InstanceID']},
            'description': 'Isolated Part 2 BOM acceptance'} for s in ('A','B','ASSEMBLY')],
        'scope': 'create; line add/update/remove/rename; explicit quantity/UOM/issue control; revise preserving source; bind/rebind/unbind; compare/readback'}
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'manifest.json'
    with path.open('x', encoding='utf-8') as f: json.dump({'digest': fingerprint(payload), 'payload': payload}, f, ensure_ascii=False, indent=2)
    print('Read-only plan saved:', path)


async def execute_manifest(path):
    manifest = json.loads(path.read_text(encoding='utf-8')); payload = manifest['payload']
    if manifest['digest'] != fingerprint(payload) or payload['environment'] != CAMSTAR_BASE_URL.rstrip('/'):
        raise ValueError('Manifest content or target environment changed.')
    if time.time() - payload['created'] > 3600: raise ValueError('Manifest expired; prepare a new unique plan.')
    p = payload['prefix']; bom = p + '-BOM'; assembly = p + '-ASSEMBLY'
    if not re.fullmatch(r'CDX-BOM-[A-Z0-9-]{1,60}', p): raise ValueError('Invalid fixture prefix.')
    for body in payload['products']:
        if body['name'] not in {p+'-A',p+'-B',assembly} or body['revision'] != '1': raise ValueError('Unexpected fixture identity.')
        if await bm.lookup('Products', body['name']): raise ValueError('Fixture exists; never adopt/replay a previous run.')
    if await bm.lookup('BOMs', bom): raise ValueError('BOM fixture exists; never replay.')
    # Exclusive creation is the permanent per-manifest attempt fence, including crash cases.
    with path.with_suffix('.journal.jsonl').open('x', encoding='utf-8') as journal:
        def record(stage, data):
            journal.write(json.dumps({'stage':stage,'at':time.time(),'data':data},ensure_ascii=False)+'\n'); journal.flush()
        async def apply(label, preview):
            plan = json.loads(await preview); record(label + '_plan', plan)
            if 'plan_id' not in plan: raise ValueError(plan)
            args = {k: plan[k] for k in ('plan_id','plan_digest')}
            result = json.loads(await bm.execute_bom_change(**args)); record(label + '_attempt', result)
            check = json.loads(await bm.reconcile_bom_change(**args)); record(label + '_verification', check)
            if not check.get('verified_success'): raise ValueError('Stopped without retry: ' + json.dumps(check))
            print(label, 'verified', flush=True)
        for body in payload['products']:
            record('product_attempt', body)
            response = await get_client().post(CAMSTAR_BASE_URL.rstrip('/')+'/api/Products', headers=get_headers(), json=body, follow_redirects=False)
            record('product_response', {'status':response.status_code, 'body':response.text})
            if not 200 <= response.status_code < 300: raise ValueError('Fixture response requires inspection; no retry.')
            actual = await bm.read_product(body['name'],'1'); record('product_readback', actual)
            expected = {'Name':body['name'],'Revision':'1','Description':body['description'],'Status':1,'ProductType':{'Name':body['productType']['name']}}
            if not bm.matches(expected,bm.stable(actual)): raise ValueError('Fixture readback mismatch; stopped.')
        def line(n, product, quantity):
            return {'name':n,'op':'add','product':{'name':p+'-'+product,'revision':'1'},'quantity':quantity,'unit':payload['unit']['name'],'issue_control':5}
        await apply('create', bm.prepare_bom_change('create',bom,'1',{'description':'BOM acceptance V1','lines':[line('A','A',2),line('B','B',1)]}))
        await apply('update', bm.prepare_bom_change('update',bom,'1',{'notes':'Updated','lines':[{'name':'A','quantity':3},{'name':'B','op':'remove'},line('C','B',4)]}))
        await apply('revise', bm.prepare_bom_change('revise',bom,'2',{'description':'BOM acceptance V2','lines':[{'name':'A','quantity':5}]},source_revision='1'))
        await apply('bind', bm.prepare_product_bom_binding(assembly,'1',bom,'2'))
        await apply('rebind', bm.prepare_product_bom_binding(assembly,'1',bom,'1'))
        await apply('unbind', bm.prepare_product_bom_binding(assembly,'1',action='unbind'))
        await apply('rename', bm.prepare_bom_change('update',bom,'2',{'eco':'ACCEPTANCE','lines':[{'name':'C','new_name':'D','quantity2':2,'unit2':'EACH','issue_control':6,'scrap_percent':2,'setup_quantity':3,'reference_designator':'R1','assembly_sequence':1}]}))
        await apply('copy_revision', bm.prepare_bom_change('revise',bom,'3',{},source_revision='2'))
        await apply('final_bind', bm.prepare_product_bom_binding(assembly,'1',bom,'3'))
        difference = json.loads(await boms.compare_bom_versions(bom,'2',bom,'3')); record('comparison', difference)
        requirements = json.loads(await boms.get_product_material_requirements(assembly,'1')); record('requirements', requirements)
        if not requirements.get('complete') or not difference.get('complete') or any(difference[k] for k in ('added','removed','changed')):
            raise ValueError('Read/compare verification incomplete.')
        record('complete', {'passed':True,'prefix':p}); print('Part 2 acceptance passed.')


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix'); parser.add_argument('--output',type=Path,default=Path('tmp/bom-acceptance-new'))
    parser.add_argument('--apply',type=Path); args=parser.parse_args()
    try:
        if args.apply: await execute_manifest(args.apply)
        elif args.prefix: await make_manifest(args.prefix,args.output)
        else: parser.error('--prefix to prepare or --apply to execute is required')
    finally: await close_client()


if __name__ == '__main__': asyncio.run(main())
