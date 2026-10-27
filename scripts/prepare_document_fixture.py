"""Explicit isolated T09 fixture creation using documented API endpoints.

--apply creates one text Document via CreateDocuments, one DocumentSet and one
Spec. It never edits production objects, follows external locators, executes
file contents, retries mutations or deletes the created test evidence.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import quote

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from config import CAMSTAR_BASE_URL,SAFE_CREATE_THRESHOLD
from core.http_client import request,get_headers,close_client
from tools.modeling_details import raw_json,_has_next_link
from tools.process_context import ReadContext
from tools.specs import create_spec
from tools.spec_documents import get_spec_documents
from tools.document_files import download_document_file


async def prepare(prefix,output,apply):
    if not re.fullmatch('CDX-T09-[A-Z0-9-]{1,45}',prefix):raise ValueError('Use an isolated CDX-T09-... prefix.')
    output.mkdir(parents=True,exist_ok=True)
    path=output/(prefix+'.json')
    if apply and path.exists():raise ValueError('Existing journal: inspect state, never blindly replay fixture writes.')
    ctx=ReadContext(max_requests=10)
    source=await ctx.resolve('Specs',{'Name':'Incoming Inspection','Revision':'1'})
    operation=await ctx.resolve('Operations',source['Operation'],revisioned=False)
    for entity in ('Documents','DocumentSets','Specs'):
        found=await ctx.get(entity,params={'$filter':f"startswith(Name,'{prefix}')",'$top':2})
        if not isinstance(found.get('value'),list) or found['value'] or _has_next_link(found):
            raise ValueError('Fixture prefix exists or absence cannot be verified: '+entity)
    filename=prefix+'.txt';ds_name=prefix+'-SET';spec_name=prefix+'-SPEC'
    content=b'TEST DATA ONLY\nT09 document upload, association, version and download hash verification.\n'
    local=output/filename;local.write_bytes(content)
    report={'created_at_utc':datetime.now(timezone.utc).isoformat(),'environment':CAMSTAR_BASE_URL.rstrip('/'),
            'authorization':'User delegated independent test data creation.','count':3,'filename':filename,
            'document_set':ds_name,'spec':spec_name,'operation':operation['Name'],
            'expected_sha256':hashlib.sha256(content).hexdigest(),'attempts':[],'state':'planned'}
    if not apply:
        (output/(prefix+'-plan.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report));return
    if 3>SAFE_CREATE_THRESHOLD:raise ValueError('Fixture count exceeds configured batch confirmation threshold.')
    def save():path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    headers={k:v for k,v in get_headers().items() if k.casefold()!='content-type'}
    async with httpx.AsyncClient(headers=headers,verify=False,timeout=30,follow_redirects=False) as client:
        try:
            entry={'kind':'Document','name':filename,'state':'attempting'};report['attempts'].append(entry);save()
            response=await client.post(CAMSTAR_BASE_URL.rstrip('/')+'/api/CreateDocuments',files={'Documents':(filename,content,'text/plain')})
            entry['http_status']=response.status_code
            try:entry['response']=response.json()
            except ValueError:entry['response']=response.text
            save()
            if response.status_code!=200:raise ValueError('Document creation did not return unambiguous HTTP200.')
            found=await ReadContext().get('Documents',params={'$filter':f"startswith(Name,'{prefix}')",'$top':2})
            rows=found.get('value')
            if not isinstance(rows,list) or len(rows)!=1 or _has_next_link(found):raise ValueError('Uploaded document is not uniquely visible.')
            doc=await ReadContext().resolve('Documents',{'InstanceId':rows[0]['InstanceID']})
            if doc.get('Revision')!='1' or not doc.get('Name', '').startswith(prefix):raise ValueError('Uploaded document version/name mismatch.')
            entry.update(state='created_and_read_verified',document={k:doc.get(k) for k in ('Name','Revision','InstanceID','Identifier')});save()
            ds_body={'Name':ds_name,'Description':'Isolated Codex T09 fixture.',
                     'DocumentEntries':[{'Name':'VALIDATION','Document':{'name':doc['Name'],'revision':doc['Revision']}}]}
            entry={'kind':'DocumentSet','name':ds_name,'request':ds_body,'state':'attempting'};report['attempts'].append(entry);save()
            entry['response']=await request('POST','/api/DocumentSets',body=ds_body,response_formatter=raw_json);save()
            ds=await ReadContext().resolve('DocumentSets',{'Name':ds_name},revisioned=False)
            entry.update(state='created_and_read_verified',instance_id=ds['InstanceID']);save()
            entry={'kind':'Spec','name':spec_name,'state':'attempting'};report['attempts'].append(entry);save()
            entry['response']=await create_spec(spec_name,'1',operation['Name'],description='Isolated Codex T09 fixture.',
                body_json=json.dumps({'DocumentSet':{'name':ds_name}}));save()
            spec=await ReadContext().resolve('Specs',{'Name':spec_name,'Revision':'1'})
            if (spec.get('DocumentSet') or {}).get('InstanceId')!=ds['InstanceID']:raise ValueError('Spec document set reference mismatch.')
            entry.update(state='created_and_read_verified',instance_id=spec['InstanceID']);save()
            report['association_result']=json.loads(await get_spec_documents(spec['InstanceID'],documents_required=True))
            if report['association_result'].get('complete') is not True:raise ValueError('Nonempty document association is incomplete.')
            report['download_result']=json.loads(await download_document_file(doc['Name'],doc['Revision']));save()
            if report['download_result'].get('sha256')!=report['expected_sha256'] or report['download_result'].get('complete') is not True:
                raise ValueError('Document download/hash verification did not pass.')
            async with httpx.AsyncClient(verify=False,timeout=15,follow_redirects=False) as anonymous:
                denied=await anonymous.get(CAMSTAR_BASE_URL.rstrip('/')+'/api/Download/'+quote(doc['Name'],safe='')+'/'+quote(doc['Revision'],safe=''))
                report['unauthenticated_http_status']=denied.status_code
            report['state']='created_association_and_download_verified';save()
        except BaseException as error:
            if report['attempts'] and report['attempts'][-1].get('state')=='attempting':
                last=report['attempts'][-1]
                last['state']='rejected_by_server' if last.get('http_status',0)>=400 else 'unknown_or_failed'
            report.update(state='stopped_without_retry',error=type(error).__name__+': '+str(error));save();raise
        finally:await close_client()
    print(json.dumps({'state':report['state'],'created_verified':sum(a['state']=='created_and_read_verified' for a in report['attempts']),
                      'sha256_matches':True,'unauthenticated_status':report.get('unauthenticated_http_status')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'docs'/'t09-fixtures')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();asyncio.run(prepare(args.prefix,args.output,args.apply))
